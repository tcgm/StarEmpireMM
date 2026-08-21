from __future__ import annotations

import hashlib
import json
import marshal
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from installer.hook_recipe import parse_hook_recipe
from installer.manager_core import ProcessProbeResult
from installer.release_profiles import (BINDING_SCHEMA, FULL_UI_POLICY,
                                        MOD_LOADER_POLICY,
                                        ReleaseProfileError,
                                        TARGET_VITALS_ALPHA_POLICY,
                                        TARGET_VITALS_ALPHA_POLICY_ID,
                                        parse_release_binding,
                                        release_policy)
from tools.build_version_binding import (
    VersionBindingError,
    build_version_binding,
    locate_policy_offsets,
    locate_release_offsets,
    locate_target_vitals_offsets,
    verify_clean_client_archive,
)


def _client_source(newline: str = "\n", prefix: str = "") -> bytes:
    text = prefix + """\
class RenderMixin:
    def _draw_panel(self):
        pass

class SolarSystemWindow(RenderMixin):
    def _apply_display_mode(self, value):
        return value

    def _handle_turret_gui_event(self, event):
        return False

    def run(self):
        self._refresh_desktop_size()
        screen = self._apply_display_mode(self._fullscreen)
        logger.info("ready")
        while self._running:
            for event in events:
                if (event.type != pygame.QUIT
                        and self._handle_turret_gui_event(event)):
                    continue
                if event.type == pygame.QUIT:
                    self._running = False
            self._profiler.mark("warp_flash")
            self._draw_warp_flash(frame)
            _frame = RenderFrame(screen, width, height)
            self._draw_background(_frame)
            self._profiler.mark("turret_layout")
            self._draw_turret_layout(frame)

def _show_use_launcher_message():
    pass

if __name__ == "__main__":
    start()
"""
    return text.replace("\n", newline).encode("utf-8")


def _render_source(newline: str = "\n") -> bytes:
    text = """\
class RenderMixin:
    def _draw_station_overlay(self):
        if self._station_overlay:
            if self._ps_tab == "Storage":
                _STOR_SEARCH_H = self._s(24)
                _iy = 0
                _iy += _STOR_SEARCH_H + self._s(8)
                _ov_sep()
"""
    return text.replace("\n", newline).encode("utf-8")


class _FakePyz:
    def __init__(self, code):
        self.toc = {"Client": (0, 0, 0)}
        self._code = code

    def extract(self, name):
        if name != "Client":
            raise KeyError(name)
        return self._code


class _FakeArchive:
    def __init__(self, outer, embedded=None):
        self.toc = {"Client": object(), "PYZ.pyz": object()}
        self._outer = outer
        self._embedded = embedded or outer

    def extract(self, name):
        if name != "Client":
            raise KeyError(name)
        return marshal.dumps(self._outer)

    def open_embedded_archive(self, name):
        if name != "PYZ.pyz":
            raise KeyError(name)
        return _FakePyz(self._embedded)


class VersionBindingTests(unittest.TestCase):
    def _fixture(self, root: Path, newline: str = "\n"):
        game = root / "Star Empire"
        internal = game / "_internal"
        internal.mkdir(parents=True)
        (game / "version.txt").write_text('"0.4.45"\n', encoding="utf-8")
        (game / "Client.exe").write_bytes(b"clean-client-exe")
        (internal / "Client.py").write_bytes(_client_source(newline))
        (internal / "render_mixin.py").write_bytes(_render_source(newline))
        ui_source = root / "ui_mod"
        ui_source.mkdir()
        for name in TARGET_VITALS_ALPHA_POLICY.ui_source_files:
            (ui_source / name).write_text(
                f'"""authored {name}"""\n', encoding="utf-8")
        return game, ui_source

    @staticmethod
    def _clear_processes():
        return ProcessProbeResult(())

    @staticmethod
    def _baseline_ok(_client: Path, _source: bytes):
        return None

    @staticmethod
    def _repack(_client: Path, source: Path, output: Path, module_names):
        payload = source.read_bytes()
        output.write_bytes(b"candidate\0" + payload[:64]
                           + "|".join(module_names).encode("utf-8"))

    def _build(self, *, game_root: Path, **kwargs):
        official_hash = hashlib.sha256(
            (Path(game_root) / "Client.exe").read_bytes()).hexdigest().upper()
        return build_version_binding(
            game_root=game_root,
            expected_official_client_sha256=official_hash,
            **kwargs,
        )

    def test_policy_is_version_neutral_and_exact(self):
        policy = release_policy(TARGET_VITALS_ALPHA_POLICY_ID)
        self.assertIs(TARGET_VITALS_ALPHA_POLICY, policy)
        self.assertEqual("Client", policy.host_module)
        self.assertEqual("Client.py", policy.host_path)
        self.assertEqual(4, len(policy.fragment_ids))
        self.assertEqual(28, len(policy.ui_source_files))
        self.assertEqual("star_empire_ui_mod", policy.frozen_module_names[0])

    def test_loader_uses_six_reviewed_anchors_and_its_own_source_package(self):
        sources = {
            "Client": _client_source(),
            "render_mixin": _render_source(),
        }
        offsets = locate_policy_offsets(sources, MOD_LOADER_POLICY)
        self.assertEqual(
            MOD_LOADER_POLICY.fragment_ids, tuple(offsets))
        self.assertEqual(6, len(offsets))
        self.assertEqual(5, len(locate_release_offsets(
            sources["Client"], MOD_LOADER_POLICY)))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, _ui_source = self._fixture(root)
            loader_source = root / "mod_loader"
            loader_source.mkdir()
            for name in MOD_LOADER_POLICY.ui_source_files:
                (loader_source / name).write_text(
                    f'"""loader {name}"""\n', encoding="utf-8")
            result = self._build(
                game_root=game,
                output_dir=root / "loader-binding",
                policy_id=MOD_LOADER_POLICY.policy_id,
                ui_source=loader_source,
                repack=self._repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes,
            )
            binding = json.loads(result.binding_path.read_text(encoding="utf-8"))
            self.assertEqual(MOD_LOADER_POLICY.policy_id, binding["policy_id"])
            self.assertEqual(
                list(MOD_LOADER_POLICY.ui_source_files),
                binding["ui_source_files"])

    def test_semantic_offsets_preserve_lf_and_crlf_raw_boundaries(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                source = _client_source(newline)
                offsets = locate_target_vitals_offsets(
                    source, TARGET_VITALS_ALPHA_POLICY)
                expected = (
                    source.index(b"        logger.info"),
                    source.index(b"                if event.type == pygame.QUIT:"),
                    source.index(b"            self._profiler.mark(\"turret_layout\")"),
                    source.index(b"if __name__ == \"__main__\":"),
                )
                self.assertEqual(
                    expected,
                    tuple(offsets[item]
                          for item in TARGET_VITALS_ALPHA_POLICY.fragment_ids),
                )

    def test_ast_scope_ignores_duplicate_raw_text_outside_window_run(self):
        prefix = """\
def unrelated(self):
    self._refresh_desktop_size()
    screen = self._apply_display_mode(self._fullscreen)
    return screen

"""
        source = _client_source(prefix=prefix)
        offsets = locate_target_vitals_offsets(
            source, TARGET_VITALS_ALPHA_POLICY)
        self.assertEqual(4, len(offsets))
        self.assertGreater(min(offsets.values()), len(prefix.encode("utf-8")))

    def test_full_policy_adds_one_structural_begin_frame_boundary(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                source = _client_source(newline)
                offsets = locate_release_offsets(source, FULL_UI_POLICY)
                self.assertEqual(5, len(offsets))
                expected = (
                    source.index(b"        logger.info"),
                    source.index(
                        b"                if event.type == pygame.QUIT:"),
                    source.index(
                        ("            self._profiler.mark(\"turret_layout\")"
                         ).replace("\n", newline).encode("utf-8")),
                    source.index(
                        ("            self._profiler.mark(\"turret_layout\")"
                         ).replace("\n", newline).encode("utf-8")),
                    source.index(
                        "if __name__ == \"__main__\":".encode("utf-8")),
                )
                frame_line = (
                    "            _frame = RenderFrame(screen, width, height)"
                    + newline).encode("utf-8")
                expected = (
                    expected[0], expected[1],
                    source.index(frame_line) + len(frame_line),
                    expected[3], expected[4])
                self.assertEqual(
                    expected,
                    tuple(offsets[item]
                          for item in FULL_UI_POLICY.fragment_ids))

    def test_missing_or_ambiguous_semantic_anchor_fails_closed(self):
        missing = _client_source().replace(
            b'self._profiler.mark("turret_layout")',
            b'self._profiler.mark("other")')
        with self.assertRaisesRegex(VersionBindingError, "missing or ambiguous"):
            locate_target_vitals_offsets(missing, TARGET_VITALS_ALPHA_POLICY)

        duplicate = _client_source().replace(
            b"        logger.info(\"ready\")\n",
            b"        screen = self._apply_display_mode(self._fullscreen)\n"
            b"        logger.info(\"ready\")\n",
        )
        with self.assertRaisesRegex(VersionBindingError, "missing or ambiguous"):
            locate_target_vitals_offsets(duplicate, TARGET_VITALS_ALPHA_POLICY)

    def test_nested_continue_inert_quit_and_unowned_profiler_are_not_anchors(self):
        cases = (
            _client_source().replace(
                b"                    continue\n                if event.type",
                b"                    if nested:\n"
                b"                        continue\n"
                b"                if event.type"),
            _client_source().replace(
                b"                    self._running = False",
                b"                    stop_loop()"),
            _client_source().replace(
                b'self._profiler.mark("turret_layout")',
                b'other.mark("turret_layout")'),
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaisesRegex(
                    VersionBindingError, "missing or ambiguous"):
                locate_target_vitals_offsets(
                    source, TARGET_VITALS_ALPHA_POLICY)

    def test_frozen_archive_must_match_loose_source_twice(self):
        source = b"value = 1\n"
        code = compile(source.decode(), "different-path.py", "exec",
                       optimize=0, dont_inherit=True)
        with mock.patch("tools.build_version_binding.CArchiveReader",
                        return_value=_FakeArchive(code)):
            verify_clean_client_archive(Path("unused.exe"), source)

        different = compile("value = 2\n", "Client.py", "exec",
                            optimize=0, dont_inherit=True)
        with mock.patch("tools.build_version_binding.CArchiveReader",
                        return_value=_FakeArchive(code, different)):
            with self.assertRaisesRegex(
                    VersionBindingError, "does not match both frozen"):
                verify_clean_client_archive(Path("unused.exe"), source)

    def test_build_is_deterministic_atomic_and_source_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root, "\r\n")
            output = root / "bindings" / "0.4.45"
            calls = []

            def repack(client, source, candidate, modules):
                calls.append((client, source, tuple(modules)))
                self._repack(client, source, candidate, modules)

            result = self._build(
                game_root=game,
                output_dir=output,
                ui_source=ui_source,
                repack=repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes,
            )
            official_prefix = hashlib.sha256(
                (game / "Client.exe").read_bytes()).hexdigest()[:12]
            self.assertEqual(
                f"target-vitals-alpha-0.4.45-{official_prefix}-v1",
                result.profile_id)
            self.assertEqual(2, len(calls))
            self.assertEqual(
                ("Client", *TARGET_VITALS_ALPHA_POLICY.frozen_module_names),
                calls[0][2],
            )
            files = tuple(sorted(path.name for path in output.iterdir()))
            self.assertEqual(
                (f"{result.profile_id}.binding.json",
                 f"{result.profile_id}.hook"),
                files,
            )
            recipe_bytes = result.recipe_path.read_bytes()
            recipe = parse_hook_recipe(recipe_bytes)
            self.assertEqual("0.4.45", recipe.game_version)
            self.assertEqual(1, len(recipe.modules))
            binding = json.loads(result.binding_path.read_text(encoding="utf-8"))
            self.assertEqual(TARGET_VITALS_ALPHA_POLICY_ID, binding["policy_id"])
            self.assertEqual(result.candidate_sha256,
                             binding["expected_client_sha256"])
            self.assertEqual(28, len(binding["ui_source_files"]))
            self.assertEqual(BINDING_SCHEMA, binding["schema"])
            expected_ui_hashes = {
                name: hashlib.sha256((ui_source / name).read_bytes())
                .hexdigest().upper()
                for name in TARGET_VITALS_ALPHA_POLICY.ui_source_files
            }
            self.assertEqual(expected_ui_hashes, binding["ui_source_sha256"])
            parsed = parse_release_binding(result.binding_path.read_bytes())
            self.assertEqual(
                expected_ui_hashes, dict(parsed.ui_source_sha256))
            combined = recipe_bytes + result.binding_path.read_bytes()
            self.assertNotIn(b"SolarSystemWindow", combined)
            self.assertNotIn(str(game).encode("utf-8"), combined)
            self.assertFalse(any(path.suffix == ".exe" for path in output.iterdir()))
            self.assertFalse(any(path.name == "Client.py" for path in output.iterdir()))

            repeated = self._build(
                game_root=game,
                output_dir=root / "bindings" / "repeat",
                ui_source=ui_source,
                repack=repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes,
            )
            self.assertEqual(result.candidate_sha256, repeated.candidate_sha256)
            self.assertEqual(
                result.recipe_path.read_bytes(), repeated.recipe_path.read_bytes())
            self.assertEqual(
                result.binding_path.read_bytes(), repeated.binding_path.read_bytes())

    def test_binding_pins_ui_bytes_rejects_bad_hash_inventory_and_keeps_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            first = self._build(
                game_root=game, output_dir=root / "first-binding",
                ui_source=ui_source, repack=self._repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes)
            first_value = json.loads(first.binding_path.read_text(encoding="utf-8"))
            changed_name = "theme.py"
            original_digest = first_value["ui_source_sha256"][changed_name]

            with (ui_source / changed_name).open("ab") as stream:
                stream.write(b"# exact-byte change\n")
            second = self._build(
                game_root=game, output_dir=root / "second-binding",
                ui_source=ui_source, repack=self._repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes)
            second_value = json.loads(
                second.binding_path.read_text(encoding="utf-8"))
            self.assertNotEqual(
                original_digest, second_value["ui_source_sha256"][changed_name])
            self.assertNotEqual(
                first.binding_path.read_bytes(), second.binding_path.read_bytes())

            missing = dict(second_value)
            missing["ui_source_sha256"] = dict(missing["ui_source_sha256"])
            missing["ui_source_sha256"].pop(changed_name)
            invalid = dict(second_value)
            invalid["ui_source_sha256"] = dict(invalid["ui_source_sha256"])
            invalid["ui_source_sha256"][changed_name] = "not-a-sha256"
            extra = dict(second_value)
            extra["ui_source_sha256"] = dict(extra["ui_source_sha256"])
            extra["ui_source_sha256"]["extra.py"] = "0" * 64
            for changed in (missing, invalid, extra):
                with self.subTest(changed=changed), self.assertRaises(
                        ReleaseProfileError):
                    parse_release_binding(
                        (json.dumps(changed, sort_keys=True, separators=(",", ":"))
                         + "\n").encode("utf-8"))

            legacy = dict(second_value)
            legacy["schema"] = 1
            legacy.pop("ui_source_sha256")
            parsed_legacy = parse_release_binding(
                (json.dumps(legacy, sort_keys=True, separators=(",", ":"))
                 + "\n").encode("utf-8"))
            self.assertEqual({}, dict(parsed_legacy.ui_source_sha256))

    def test_same_reported_version_with_new_executable_gets_a_new_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_game, ui_source = self._fixture(root / "first")
            second_game, _ = self._fixture(root / "second")
            (second_game / "Client.exe").write_bytes(b"new-official-hotfix")
            first = self._build(
                game_root=first_game, output_dir=root / "first-binding",
                ui_source=ui_source, repack=self._repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes)
            second = self._build(
                game_root=second_game, output_dir=root / "second-binding",
                ui_source=ui_source, repack=self._repack,
                baseline_probe=self._baseline_ok,
                process_probe=self._clear_processes)
            self.assertNotEqual(first.profile_id, second.profile_id)
            self.assertIn("0.4.45", first.profile_id)
            self.assertIn("0.4.45", second.profile_id)

    def test_untrusted_official_executable_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            with self.assertRaisesRegex(VersionBindingError, "trusted official"):
                build_version_binding(
                    game_root=game, output_dir=root / "out",
                    expected_official_client_sha256="0" * 64,
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertFalse((root / "out").exists())

    def test_modded_missing_and_running_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            client_source = game / "_internal" / "Client.py"
            client_source.write_bytes(
                client_source.read_bytes() + b"\n# star_empire_ui_mod\n")
            with self.assertRaisesRegex(VersionBindingError, "already contains"):
                self._build(
                    game_root=game, output_dir=root / "out",
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            (game / "version.txt").unlink()
            with self.assertRaisesRegex(VersionBindingError, "version file"):
                self._build(
                    game_root=game, output_dir=root / "out",
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            with self.assertRaisesRegex(VersionBindingError, "close the game"):
                self._build(
                    game_root=game, output_dir=root / "out",
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=lambda: ProcessProbeResult(("client.exe",)))

    def test_uncertain_process_probe_and_baseline_failure_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            with self.assertRaisesRegex(VersionBindingError, "cannot verify"):
                self._build(
                    game_root=game, output_dir=root / "out",
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=lambda: ProcessProbeResult.unknown("probe failed"))
            self.assertFalse((root / "out").exists())

            def fail_baseline(_client, _source):
                raise VersionBindingError("frozen mismatch")

            with self.assertRaisesRegex(VersionBindingError, "frozen mismatch"):
                self._build(
                    game_root=game, output_dir=root / "out",
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=fail_baseline,
                    process_probe=self._clear_processes)
            self.assertFalse((root / "out").exists())

    def test_compile_repack_and_determinism_failures_leave_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            with mock.patch(
                    "tools.build_version_binding._apply_policy_fragments",
                    return_value={"Client": b"not valid python ("}):
                with self.assertRaisesRegex(VersionBindingError, "does not compile"):
                    self._build(
                        game_root=game, output_dir=root / "compile-out",
                        ui_source=ui_source, repack=self._repack,
                        baseline_probe=self._baseline_ok,
                        process_probe=self._clear_processes)
            self.assertFalse((root / "compile-out").exists())

            def fail_repack(_client, _source, _output, _modules):
                raise RuntimeError("repack failed")

            with self.assertRaisesRegex(RuntimeError, "repack failed"):
                self._build(
                    game_root=game, output_dir=root / "repack-out",
                    ui_source=ui_source, repack=fail_repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertFalse((root / "repack-out").exists())

            counter = 0

            def unstable(_client, source, output, _modules):
                nonlocal counter
                counter += 1
                output.write_bytes(source.read_bytes() + bytes([counter]))

            with self.assertRaisesRegex(VersionBindingError, "not deterministic"):
                self._build(
                    game_root=game, output_dir=root / "unstable-out",
                    ui_source=ui_source, repack=unstable,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertFalse((root / "unstable-out").exists())

    def test_mid_run_game_change_and_output_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            calls = 0

            def changing(client, source, output, modules):
                nonlocal calls
                calls += 1
                self._repack(client, source, output, modules)
                if calls == 2:
                    client.write_bytes(client.read_bytes() + b"changed")

            with self.assertRaisesRegex(VersionBindingError, "updated or changed"):
                self._build(
                    game_root=game, output_dir=root / "changed-out",
                    ui_source=ui_source, repack=changing,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertFalse((root / "changed-out").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            output = root / "existing"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self._build(
                    game_root=game, output_dir=output,
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertEqual("keep", (output / "keep.txt").read_text())

    def test_output_inside_game_installation_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, ui_source = self._fixture(root)
            output = game / "generated-binding"
            with self.assertRaisesRegex(VersionBindingError, "outside"):
                self._build(
                    game_root=game, output_dir=output,
                    ui_source=ui_source, repack=self._repack,
                    baseline_probe=self._baseline_ok,
                    process_probe=self._clear_processes)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
