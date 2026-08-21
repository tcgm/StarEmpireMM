from __future__ import annotations

from pathlib import Path
import logging
import tempfile
import unittest
from unittest.mock import patch

from mod_loader.host_client import (
    ModLoaderInstallError,
    STATE_ROOT_ENV,
    begin_mod_loader_frame,
    configure_mod_logging,
    default_registry_path,
    draw_mod_loader_overlay,
    draw_mod_loader_region,
    handle_mod_loader_event,
    initialize_mod_loader,
    install_mod_loader_bridge,
    shutdown_mod_loader,
)
from installer.authored_fragments import (
    ACTIVE_AUTHORED_FRAGMENT_IDS,
    CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
    CLIENT_MOD_LOADER_BOOTSTRAP_V1,
    CLIENT_MOD_LOADER_EVENT_V1,
    CLIENT_MOD_LOADER_INSTALL_V1,
    CLIENT_MOD_LOADER_OVERLAY_V1,
    resolve_authored_fragment,
)


class _Host:
    pass


class _Pygame:
    QUIT = 1
    KEYDOWN = 2
    KEYUP = 3
    K_ESCAPE = 27


class _Event:
    def __init__(self, event_type, key=None):
        self.type = event_type
        self.key = key


class _Loader:
    def __init__(self, path, *, load_error=None):
        self.path = Path(path)
        self.load_error = load_error
        self.events = []
        self.results = ()
        self.shutdown_count = 0

    def load_enabled(self):
        if self.load_error is not None:
            raise self.load_error
        return ()

    def emit(self, event_name, **kwargs):
        self.events.append((event_name, kwargs))
        return self.results

    def shutdown(self):
        self.shutdown_count += 1


class ModLoaderHostTests(unittest.TestCase):
    def test_file_logging_is_rotating_utf8_and_not_duplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = configure_mod_logging(root)
            self.assertEqual(root / "logs" / "star-empire-mods.log", path)
            configure_mod_logging(root)
            logger = logging.getLogger("star_empire_mod.example")
            logger.error("test diagnostic")
            handlers = [
                handler for handler in logging.getLogger("star_empire_mod").handlers
                if getattr(handler, "_star_empire_mod_log", None) == path]
            self.assertEqual(1, len(handlers))
            self.assertIn(handlers[0], logging.getLogger("mod_loader").handlers)
            handlers[0].max_bytes = 160
            logger.error("rotation-one-%s", "x" * 120)
            logger.error("rotation-two-%s", "y" * 120)
            handlers[0].flush()
            self.assertIn("rotation-two", path.read_text(encoding="utf-8"))
            self.assertTrue(path.with_name(path.name + ".1").is_file())
            closed = set()
            for namespace in ("star_empire_mod", "mod_loader"):
                target = logging.getLogger(namespace)
                for handler in tuple(target.handlers):
                    if getattr(handler, "_star_empire_mod_log", None) == path:
                        target.removeHandler(handler)
                        if id(handler) not in closed:
                            handler.close()
                            closed.add(id(handler))

    def test_loader_logging_uses_no_unfrozen_logging_handlers_module(self):
        source = Path("mod_loader/host_client.py").read_text(encoding="utf-8")
        self.assertNotIn("logging.handlers", source)
        self.assertNotIn("RotatingFileHandler", source)
        self.assertIn("class _BoundedFileHandler(logging.FileHandler)", source)

    def test_atomic_bridge_install_and_collision_failure(self):
        class Client:
            pass

        names = install_mod_loader_bridge(Client, _Pygame)
        self.assertEqual(6, len(names))
        self.assertTrue(all(callable(getattr(Client, name)) for name in names))
        with self.assertRaisesRegex(ModLoaderInstallError, "already exists"):
            install_mod_loader_bridge(Client, _Pygame)

        class Colliding:
            _handle_star_empire_mod_event = object()

        with self.assertRaisesRegex(ModLoaderInstallError, "already exists"):
            install_mod_loader_bridge(Colliding, _Pygame)
        self.assertFalse(hasattr(
            Colliding, "_initialize_star_empire_mod_loader"))

    def test_loader_fragments_are_source_only_and_active(self):
        fragment_ids = (
            CLIENT_MOD_LOADER_BOOTSTRAP_V1,
            CLIENT_MOD_LOADER_EVENT_V1,
            CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
            CLIENT_MOD_LOADER_OVERLAY_V1,
            CLIENT_MOD_LOADER_INSTALL_V1,
        )
        for fragment_id in fragment_ids:
            fragment = resolve_authored_fragment(fragment_id)
            self.assertEqual("Client", fragment.module)
            self.assertGreater(fragment.size, 0)
            self.assertIn(fragment_id, ACTIVE_AUTHORED_FRAGMENT_IDS)
            self.assertNotIn("Client.exe", fragment.text)
        install = resolve_authored_fragment(CLIENT_MOD_LOADER_INSTALL_V1).text
        self.assertIn("star_empire_mod_loader.host_client", install)
        self.assertNotIn("star_empire_ui_mod", install)

    def test_default_registry_uses_shared_manager_root_and_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict("os.environ", {STATE_ROOT_ENV: temporary}):
                self.assertEqual(
                    Path(temporary).resolve() / "mods.json",
                    default_registry_path())

    def test_initialize_is_transactional_idempotent_and_emits_startup(self):
        host = _Host()
        screen = object()
        made = []

        def factory(path):
            loader = _Loader(path)
            made.append(loader)
            return loader

        path = Path("C:/managed/mods.json")
        self.assertTrue(initialize_mod_loader(
            host, _Pygame, screen, registry_path=path,
            loader_factory=factory))
        self.assertTrue(initialize_mod_loader(
            host, _Pygame, screen, registry_path=path,
            loader_factory=factory))
        self.assertEqual(1, len(made))
        self.assertIs(made[0], host._se_mod_loader)
        self.assertEqual("client.startup", made[0].events[0][0])
        self.assertIs(host, made[0].events[0][1]["host"])

    def test_registry_failure_leaves_vanilla_and_logs_only_once(self):
        host = _Host()
        factory = lambda path: _Loader(path, load_error=ValueError("bad registry"))
        with self.assertLogs("mod_loader.host_client", "ERROR") as captured:
            self.assertFalse(initialize_mod_loader(
                host, _Pygame, object(), loader_factory=factory))
            self.assertFalse(initialize_mod_loader(
                host, _Pygame, object(), loader_factory=factory))
        self.assertIsNone(host._se_mod_loader)
        self.assertEqual(1, sum(
            "STARTUP_FAILED" in line for line in captured.output))

    def test_events_require_explicit_true_and_escape_remains_game_owned(self):
        host = _Host()
        loader = _Loader(Path("C:/managed/mods.json"))
        host._se_mod_loader = loader
        loader.results = (1, "yes", True)
        self.assertTrue(handle_mod_loader_event(
            host, _Pygame, _Event(99), object()))
        self.assertFalse(handle_mod_loader_event(
            host, _Pygame, _Event(_Pygame.KEYDOWN, _Pygame.K_ESCAPE),
            object()))
        loader.results = (1, "yes")
        self.assertFalse(handle_mod_loader_event(
            host, _Pygame, _Event(99), object()))

    def test_begin_draw_and_quit_shutdown_are_isolated_and_idempotent(self):
        host = _Host()
        loader = _Loader(Path("C:/managed/mods.json"))
        host._se_mod_loader = loader
        host._se_mod_loader_shutdown = False
        screen = object()
        render_target = object()

        begin_mod_loader_frame(host, render_target)
        draw_mod_loader_overlay(host, _Pygame, screen, render_target)
        self.assertEqual(
            ["client.frame.begin", "client.draw"],
            [item[0] for item in loader.events])
        self.assertFalse(handle_mod_loader_event(
            host, _Pygame, _Event(_Pygame.QUIT), screen))
        shutdown_mod_loader(host)
        self.assertEqual(1, loader.shutdown_count)

    def test_inline_region_uses_largest_valid_clamped_reservation(self):
        host = _Host()
        loader = _Loader(Path("C:/managed/mods.json"))
        host._se_mod_loader = loader
        loader.results = (False, -4, 20, 120, 12.5)
        rect = type("Rect", (), {"height": 90})()

        height = draw_mod_loader_region(
            host, _Pygame, object(), "player_station.storage.filters", rect)

        self.assertEqual(90, height)
        self.assertEqual("client.region.draw", loader.events[-1][0])
        self.assertEqual(
            "player_station.storage.filters",
            loader.events[-1][1]["region"],
        )

    def test_inline_region_failure_reserves_nothing(self):
        host = _Host()
        loader = _Loader(Path("C:/managed/mods.json"))
        host._se_mod_loader = loader

        def fail(*_args, **_kwargs):
            raise RuntimeError("draw failed")

        loader.emit = fail
        rect = type("Rect", (), {"height": 90})()
        with self.assertLogs("mod_loader.host_client", "ERROR"):
            height = draw_mod_loader_region(
                host, _Pygame, object(),
                "player_station.storage.filters", rect)
        self.assertEqual(0, height)


if __name__ == "__main__":
    unittest.main()
