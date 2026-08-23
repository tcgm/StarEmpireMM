from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch

from installer.candidate_builder import BuiltCandidate, CandidateBuildError
from installer.manager_core import (CompatibilityPack, InstallState,
                                    InstallStatus, ManagerDataError,
                                    save_install_state, sha256_file)
from installer.mod_package import PackageError
from installer.manager_ui import (ManagerApp, acquire_signed_update,
                                   action_availability,
                                   compatibility_test_available,
                                   detect_game_build_identity,
                                   discover_local_loader,
                                   load_mod_display_details,
                                   load_selected_game,
                                   manager_internal_loader_root,
                                   mod_compatibility_view,
                                   MOD_REGISTRY_POLL_MS,
                                   MOD_README_MAX_BYTES,
                                   save_selected_game,
                                   selected_game_matches_inspection)
from installer.trusted_keys import (BUILTIN_TRUSTED_KEYS,
                                    DEFAULT_UPDATE_FEED_URL,
                                    load_trust_configuration,
                                    load_trusted_keys)
from installer.update_feed import FeedError, UpdateFeed, UpdateFeedIndex
from installer.mod_manifest import ModCompatibility


class ManagerUiStateTests(unittest.TestCase):
    def test_mod_compatibility_status_is_clear_and_force_is_visible(self):
        compatibility = ModCompatibility(game_versions=("0.4.62",))

        compatible = mod_compatibility_view(
            compatibility, False, "0.4.62", "A" * 64)
        blocked = mod_compatibility_view(
            compatibility, False, "0.4.63", "B" * 64)
        forced = mod_compatibility_view(
            compatibility, True, "0.4.63", "B" * 64)

        self.assertEqual("COMPATIBLE", compatible.status)
        self.assertEqual("INCOMPATIBLE", blocked.status)
        self.assertIn("will not load", blocked.detail)
        self.assertEqual("FORCED", forced.status)
        self.assertIn("may crash", forced.detail)
        self.assertEqual("0.4.62", forced.supported)

    def test_force_load_checkbox_confirms_and_saves_selected_mod(self):
        app = object.__new__(ManagerApp)
        app._selected_mod_id = lambda: "example.mod"
        app.force_load_mod = SimpleNamespace(get=lambda: True, set=lambda _value: None)
        app._mods = SimpleNamespace(
            set_force_load=unittest.mock.Mock(return_value=SimpleNamespace(
                mod_id="example.mod")))
        app._prepare_game_for_mod_install = unittest.mock.Mock(return_value=True)
        app.status_text = unittest.mock.Mock()
        app.summary_text = unittest.mock.Mock()
        app._refresh_mods = unittest.mock.Mock()

        with patch(
                "installer.manager_ui.messagebox.askyesno",
                return_value=True):
            app._set_selected_mod_force_load()

        app._prepare_game_for_mod_install.assert_not_called()
        app._mods.set_force_load.assert_called_once_with("example.mod", True)
        app._refresh_mods.assert_called_once()

    def test_force_load_is_not_saved_when_confirmation_is_cancelled(self):
        app = object.__new__(ManagerApp)
        app._selected_mod_id = lambda: "example.mod"
        app.force_load_mod = SimpleNamespace(
            get=lambda: True, set=unittest.mock.Mock())
        app._mods = SimpleNamespace(set_force_load=unittest.mock.Mock())
        with patch(
                "installer.manager_ui.messagebox.askyesno",
                return_value=False):
            app._set_selected_mod_force_load()

        app._mods.set_force_load.assert_not_called()
        app.force_load_mod.set.assert_called_once_with(False)

    def test_selected_game_round_trip_is_manager_owned_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            selection = root / "manager-state" / "selected-game.txt"

            self.assertIsNone(load_selected_game(selection))
            save_selected_game(selection, game)

            self.assertEqual(game.resolve(), load_selected_game(selection))
            self.assertFalse(tuple(selection.parent.glob("*.partial")))

    def test_selected_game_must_match_the_inspected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            other = root / "other"
            game.mkdir()
            other.mkdir()
            inspection = SimpleNamespace(game_root=game.resolve())

            self.assertTrue(selected_game_matches_inspection(
                str(game), inspection))
            self.assertFalse(selected_game_matches_inspection(
                str(other), inspection))
            self.assertFalse(selected_game_matches_inspection("", inspection))
            self.assertFalse(selected_game_matches_inspection(str(game), None))

    def test_interrupted_operation_disables_every_action_except_repair(self) -> None:
        inspection = SimpleNamespace(
            can_install=True, can_update=False, can_restore=False, pack=object())
        state = action_availability(inspection, None, True)
        self.assertTrue(state.repair)
        self.assertFalse(state.build)
        self.assertFalse(state.install)

    def test_build_is_available_before_install_candidate_exists(self) -> None:
        inspection = SimpleNamespace(
            can_install=True, can_update=False, can_restore=False, pack=object())
        state = action_availability(inspection, None, False)
        self.assertTrue(state.build)
        self.assertFalse(state.install)
        self.assertFalse(state.update)

    def test_compatibility_conflict_enables_only_warning_gated_build(self) -> None:
        pack = CompatibilityPack(
            "pack", "1.0", "0.4.45", "A" * 64, "B" * 64,
            Path("."), "C" * 64, "key")
        inspection = SimpleNamespace(
            game_root=Path(".").resolve(),
            status=InstallStatus.UNSUPPORTED_VANILLA,
            current_sha256="D" * 64, version="0.4.46", pack=pack,
            can_install=False, can_update=False, can_restore=False)
        package = SimpleNamespace(
            manifest={"schema": 2}, release_profile=object(),
            release_binding=SimpleNamespace(ui_source_sha256={"theme.py": "E" * 64}))

        self.assertTrue(compatibility_test_available(inspection, package))
        availability = action_availability(
            inspection, None, False, compatibility_test=True)
        self.assertTrue(availability.build)
        self.assertFalse(availability.install)
        self.assertFalse(availability.update)

        for changed in (
                SimpleNamespace(**{**inspection.__dict__, "current_sha256": None}),
                SimpleNamespace(**{**inspection.__dict__,
                                   "status": InstallStatus.PROCESS_CHECK_FAILED}),
                SimpleNamespace(**{**inspection.__dict__, "version": None})):
            self.assertFalse(compatibility_test_available(changed, package))

    def test_audited_compatibility_candidate_enables_install_not_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "Client.exe"
            candidate_path.write_bytes(b"compatibility candidate")
            pack = CompatibilityPack(
                "pack", "1.0", "0.4.45", "A" * 64, "B" * 64,
                root, "C" * 64, "key")
            inspection = SimpleNamespace(
                status=InstallStatus.UNSUPPORTED_VANILLA,
                current_sha256="D" * 64, version="0.4.46", pack=pack,
                can_install=False, can_update=False, can_restore=False)
            candidate = BuiltCandidate(
                candidate_path, sha256_file(candidate_path), "D" * 64,
                "pack", "C" * 64, "key", ("Client",), root / "audit.json",
                True, "0.4.46")

            availability = action_availability(
                inspection, candidate, False, compatibility_test=True)
            self.assertTrue(availability.build)
            self.assertTrue(availability.install)
            self.assertFalse(availability.update)

    def test_declining_either_compatibility_warning_makes_no_change(self) -> None:
        package = SimpleNamespace(
            manifest={"schema": 2}, release_profile=object(),
            release_binding=SimpleNamespace(ui_source_sha256={"x.py": "A" * 64}))
        inspection = SimpleNamespace(
            game_root=Path(".").resolve(),
            status=InstallStatus.UNSUPPORTED_VANILLA,
            current_sha256="B" * 64, version="0.4.46", pack=object(),
            can_install=False, can_update=False, can_restore=False)
        app = object.__new__(ManagerApp)
        app._inspection = inspection
        app._package = package
        app.game_path = SimpleNamespace(get=lambda: str(inspection.game_root))
        with patch("installer.manager_ui.messagebox.askyesno", return_value=False), \
                patch("installer.manager_ui.build_candidate") as build:
            app._build_candidate()
        build.assert_not_called()

        app._candidate = SimpleNamespace(compatibility_test=True)
        app._transaction = SimpleNamespace(install_or_update=unittest.mock.Mock())
        with patch("installer.manager_ui.messagebox.askyesno", return_value=False):
            app._install_or_update("install")
        app._transaction.install_or_update.assert_not_called()

    def test_changed_path_blocks_build_and_install_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inspected = root / "inspected"
            selected = root / "selected"
            inspected.mkdir()
            selected.mkdir()
            package = SimpleNamespace(
                manifest={"schema": 2}, release_profile=object(),
                release_binding=SimpleNamespace(
                    ui_source_sha256={"x.py": "A" * 64}))
            inspection = SimpleNamespace(
                game_root=inspected.resolve(),
                status=InstallStatus.UNSUPPORTED_VANILLA,
                current_sha256="B" * 64, version="0.4.47", pack=object(),
                can_install=False, can_update=False, can_restore=False)
            app = object.__new__(ManagerApp)
            app._inspection = inspection
            app._package = package
            app._candidate = SimpleNamespace(compatibility_test=True)
            app.game_path = SimpleNamespace(get=lambda: str(selected))
            app._set_actions = unittest.mock.Mock()
            app._transaction = SimpleNamespace(
                install_or_update=unittest.mock.Mock())

            with patch("installer.manager_ui.messagebox.showerror"), patch(
                    "installer.manager_ui.build_candidate") as build:
                app._build_candidate()
                app._candidate = SimpleNamespace(compatibility_test=True)
                app._install_or_update("install")

            build.assert_not_called()
            app._transaction.install_or_update.assert_not_called()
            self.assertIsNone(app._candidate)

    def test_manual_package_selection_invalidates_pending_feed_result(self) -> None:
        app = object.__new__(ManagerApp)
        app._update_generation = 7
        app._update_check_active = True
        button = unittest.mock.Mock()
        app.update_check_button = button

        app._cancel_pending_update_check()

        self.assertEqual(8, app._update_generation)
        self.assertFalse(app._update_check_active)
        button.configure.assert_called_once_with(state="normal")

    def test_stale_feed_result_does_not_strand_newer_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary).resolve()
            package = SimpleNamespace(path=game / "new.seuimod")
            resolution = SimpleNamespace(package=package)
            app = object.__new__(ManagerApp)
            app._update_results = SimpleQueue()
            app._update_results.put((7, True, str(game), None, RuntimeError("old")))
            app._update_results.put((8, True, str(game), resolution, None))
            app._update_generation = 8
            app._update_check_active = True
            app.root = SimpleNamespace(after=unittest.mock.Mock())
            app.update_check_button = unittest.mock.Mock()
            app.game_path = SimpleNamespace(get=lambda: str(game))
            app.pack_path = SimpleNamespace(set=unittest.mock.Mock())
            app.status_text = SimpleNamespace(set=unittest.mock.Mock())
            app.summary_text = SimpleNamespace(set=unittest.mock.Mock())
            app._package = None
            app._candidate = object()
            app.refresh = unittest.mock.Mock()

            app._poll_update_result()
            self.assertTrue(app._update_check_active)
            app.root.after.assert_called_once_with(100, app._poll_update_result)

            app._poll_update_result()
            self.assertFalse(app._update_check_active)
            self.assertIs(package, app._package)
            self.assertIsNone(app._candidate)
            app.pack_path.set.assert_called_once_with(str(package.path))
            app.refresh.assert_called_once_with()

    def test_external_trust_file_accepts_only_strict_public_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "keys.json"
            path.write_text(json.dumps({
                "schema": 1,
                "keys": {"test": base64.b64encode(b"K" * 32).decode("ascii")},
                "update_feed_url": "https://example.test/stable.json",
            }), encoding="utf-8")
            trust = load_trust_configuration(path)
            self.assertEqual(b"K" * 32, trust.keys["test"])
            self.assertEqual(
                bytes.fromhex(
                    "42F13FDF90D978F7FC45FE7BA90742AD"
                    "0F8B966682CB0B049F5B04709D0AA80F"),
                trust.keys["star-empire-ui-alpha-2026-08"],
            )
            self.assertEqual("https://example.test/stable.json",
                             trust.update_feed_url)
            self.assertEqual(trust.keys, load_trusted_keys(path))

            path.write_text(json.dumps({
                "schema": 1, "keys": {"bad": "not-base64"},
            }), encoding="utf-8")
            with self.assertRaises(ManagerDataError):
                load_trusted_keys(path)

    def test_default_trust_configuration_has_the_signed_release_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            trust = load_trust_configuration(missing)
            self.assertEqual(DEFAULT_UPDATE_FEED_URL, trust.update_feed_url)
            self.assertTrue(trust.update_feed_url.startswith("https://"))

    def test_build_identity_uses_verified_vanilla_backup_when_mod_is_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            (game / "version.txt").write_text("0.4.45\n", encoding="utf-8")
            client = game / "Client.exe"
            client.write_bytes(b"modded client")
            backup = root / "backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official client")
            state_path = root / "state.json"
            state = InstallState.new(
                game, backup, sha256_file(backup), sha256_file(client), "pack")
            save_install_state(state_path, state)

            identity = detect_game_build_identity(game, state_path)
            self.assertEqual("0.4.45", identity.game_version)
            self.assertEqual(sha256_file(backup), identity.official_client_sha256)
            self.assertEqual(sha256_file(client), identity.current_client_sha256)

    def test_companion_loader_is_discovered_by_exact_build_without_picker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "StarEmpireModLoader-current.seloader"
            legacy_duplicate = root / "duplicate.seuimod"
            wrong = root / "wrong.seloader"
            for path in (current, legacy_duplicate, wrong):
                path.write_bytes(path.name.encode())
            identity = SimpleNamespace(
                game_version="0.4.47", official_client_sha256="A" * 64)
            match = SimpleNamespace(
                path=current, manifest_sha256="B" * 64,
                compatibility=SimpleNamespace(
                    game_version="0.4.47",
                    official_client_sha256="A" * 64))
            duplicate = SimpleNamespace(
                path=legacy_duplicate, manifest_sha256="B" * 64,
                compatibility=match.compatibility)
            mismatch = SimpleNamespace(
                path=wrong, manifest_sha256="C" * 64,
                compatibility=SimpleNamespace(
                    game_version="0.4.46",
                    official_client_sha256="D" * 64))

            def verified(path, _keys):
                return {current: match, legacy_duplicate: duplicate,
                        wrong: mismatch}[path]

            with patch("installer.manager_ui.detect_game_build_identity",
                       return_value=identity), patch(
                    "installer.manager_ui.verify_mod_package",
                    side_effect=verified):
                selected = discover_local_loader(
                    root / "game", root / "state.json", root, {"key": b"K"})
            self.assertIs(match, selected)

    def test_single_internal_binding_can_drive_guarded_compatibility_test(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_path = root / "internal.seloader"
            package_path.write_bytes(b"package")
            identity = SimpleNamespace(
                game_version="0.4.48", official_client_sha256="A" * 64)
            package = SimpleNamespace(
                path=package_path, manifest_sha256="B" * 64,
                compatibility=SimpleNamespace(
                    game_version="0.4.47",
                    official_client_sha256="C" * 64))
            with patch("installer.manager_ui.detect_game_build_identity",
                       return_value=identity), patch(
                    "installer.manager_ui.verify_mod_package",
                    return_value=package):
                selected = discover_local_loader(
                    root / "game", root / "state.json", root, {"key": b"K"},
                    allow_compatibility_fallback=True)
            self.assertIs(package, selected)

    def test_internal_fallback_rejects_multiple_distinct_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.seloader"
            second = root / "second.seloader"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            identity = SimpleNamespace(
                game_version="0.4.48", official_client_sha256="A" * 64)

            def verified(path, _keys):
                return SimpleNamespace(
                    path=path, manifest_sha256=("B" if path == first else "C") * 64,
                    compatibility=SimpleNamespace(
                        game_version="0.4.47",
                        official_client_sha256="D" * 64))

            with patch("installer.manager_ui.detect_game_build_identity",
                       return_value=identity), patch(
                    "installer.manager_ui.verify_mod_package",
                    side_effect=verified):
                with self.assertRaisesRegex(PackageError, "multiple internal"):
                    discover_local_loader(
                        root / "game", root / "state.json", root,
                        {"key": b"K"}, allow_compatibility_fallback=True)

    def test_invalid_bundled_template_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "internal.seloader"
            package.write_bytes(b"broken")
            identity = SimpleNamespace(
                game_version="0.4.67", official_client_sha256="A" * 64)

            with patch("installer.manager_ui.detect_game_build_identity",
                       return_value=identity), patch(
                    "installer.manager_ui.verify_mod_package",
                    side_effect=PackageError("requires a newer Manager")):
                with self.assertRaisesRegex(
                        PackageError, "bundled compatibility template"):
                    discover_local_loader(
                        root / "game", root / "state.json", root,
                        {"key": b"K"}, allow_compatibility_fallback=True,
                        strict_internal_inventory=True)

    def test_frozen_internal_loader_root_is_private_extraction_folder(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
                __import__("installer.manager_ui", fromlist=["sys"]).sys,
                "_MEIPASS", temporary, create=True):
            self.assertEqual(
                Path(temporary).resolve() / "embedded_loaders",
                manager_internal_loader_root())

    def test_game_selection_uses_companion_loader_before_network_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary).resolve()
            app = object.__new__(ManagerApp)
            app._cancel_pending_update_check = unittest.mock.Mock()
            app.game_path = SimpleNamespace(set=unittest.mock.Mock())
            app._selected_game_path = selected.parent / "selected-game.txt"
            app.pack_path = SimpleNamespace(set=unittest.mock.Mock())
            app._package = object()
            app._candidate = object()
            app._auto_select_local_loader = unittest.mock.Mock(
                return_value=True)
            app.refresh = unittest.mock.Mock()
            app._check_updates = unittest.mock.Mock()

            with patch("installer.manager_ui.filedialog.askdirectory",
                       return_value=str(selected)), patch(
                    "installer.manager_ui.save_selected_game") as remember:
                app._choose_game()

            app._cancel_pending_update_check.assert_called_once_with()
            app.game_path.set.assert_called_once_with(str(selected))
            remember.assert_called_once_with(
                app._selected_game_path, selected)
            app.pack_path.set.assert_called_once_with("")
            app._auto_select_local_loader.assert_called_once_with(selected)
            app.refresh.assert_called_once_with()
            app._check_updates.assert_not_called()

    def test_update_acquisition_selects_exact_build_and_rechecks_game(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            (game / "version.txt").write_text("0.4.45\n", encoding="utf-8")
            client = game / "Client.exe"
            client.write_bytes(b"official client")
            digest = sha256_file(client)
            release = UpdateFeed(
                "alpha", "package", "pack-045", "0.2.0", "0.4.45",
                "0.2.0", SimpleNamespace(),
                "https://example.test/mod.seuimod", 10, "A" * 64, digest)
            feed = UpdateFeedIndex(
                "alpha", "feed", 1, SimpleNamespace(), SimpleNamespace(),
                "0.2.0", (release,), "B" * 64)
            compatibility = SimpleNamespace(
                pack_id=release.pack_id, mod_version=release.mod_version,
                game_version=release.game_version,
                official_client_sha256=digest, key_id=release.key_id)
            package = SimpleNamespace(
                path=root / "download.seuimod", compatibility=compatibility)

            with patch("installer.manager_ui.fetch_update_feed",
                       return_value=feed), patch(
                    "installer.manager_ui.accept_update_feed_sequence"), patch(
                    "installer.manager_ui.download_update_package",
                    return_value=package):
                result = acquire_signed_update(
                    game, root / "state.json", "https://example.test/feed",
                    {"feed": b"F" * 32, "package": b"P" * 32},
                    root / "downloads")
            self.assertIs(package, result.package)
            self.assertEqual(digest, result.identity.official_client_sha256)

            changed = SimpleNamespace(
                game_root=game.resolve(), game_version="0.4.45",
                official_client_sha256="C" * 64,
                current_client_sha256="C" * 64)
            original = detect_game_build_identity(game, root / "state.json")
            with patch("installer.manager_ui.detect_game_build_identity",
                       side_effect=[original, changed]), patch(
                    "installer.manager_ui.fetch_update_feed",
                    return_value=feed), patch(
                    "installer.manager_ui.accept_update_feed_sequence"), patch(
                    "installer.manager_ui.download_update_package",
                    return_value=package):
                with self.assertRaisesRegex(Exception, "changed"):
                    acquire_signed_update(
                        game, root / "state.json",
                        "https://example.test/feed",
                        {"feed": b"F" * 32, "package": b"P" * 32},
                        root / "downloads-2")

    def test_automatic_path_rejects_replayable_legacy_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            (game / "version.txt").write_text("0.4.45\n", encoding="utf-8")
            (game / "Client.exe").write_bytes(b"official")
            legacy = UpdateFeed(
                "alpha", "package", "old-pack", "0.1.0", "0.4.45",
                "0.2.0", SimpleNamespace(),
                "https://example.test/old.seuimod", 10, "A" * 64)
            with patch("installer.manager_ui.fetch_update_feed",
                       return_value=legacy):
                with self.assertRaisesRegex(FeedError, "legacy"):
                    acquire_signed_update(
                        game, root / "state.json",
                        "https://example.test/feed",
                        {"package": b"P" * 32}, root / "downloads")

    def test_builtin_alpha_key_is_fixed_and_exactly_ed25519_sized(self) -> None:
        self.assertEqual(
            {"star-empire-ui-alpha-2026-08"},
            set(BUILTIN_TRUSTED_KEYS),
        )
        self.assertEqual(
            32, len(BUILTIN_TRUSTED_KEYS["star-empire-ui-alpha-2026-08"]))

    def test_ui_source_has_no_arbitrary_client_executable_picker(self) -> None:
        source = Path("installer/manager_ui.py").read_text(encoding="utf-8")
        self.assertIn("discover_local_loader(", source)
        self.assertIn("manager_internal_loader_root()", source)
        self.assertNotIn("Locate a signed Star Empire loader package", source)
        self.assertNotIn("Loader infrastructure", source)
        self.assertNotIn("Locate manually…", source)
        self.assertIn("build_candidate(", source)
        self.assertIn("self._transaction.recover()", source)
        self.assertIn("fetch_update_feed(", source)
        self.assertIn("download_update_package(", source)
        self.assertIn("Thread(target=worker", source)
        self.assertIn("select_update_release(", source)
        self.assertIn('"COMPATIBILITY CONFLICT"', source)
        self.assertIn('"Try Compatibility Test…"', source)
        self.assertIn('state="readonly"', source)
        self.assertIn("selected_game_matches_inspection(", source)
        self.assertNotIn("host is already modified", source)
        self.assertNotIn("Client executable", source)
        self.assertNotIn("CompatibilityPack.load", source)

    def test_ui_exposes_general_loader_and_external_mod_pages(self) -> None:
        source = Path("installer/manager_ui.py").read_text(encoding="utf-8")
        self.assertIn("STAR EMPIRE MOD MANAGER", source)
        self.assertIn("automatic game compatibility", source)
        self.assertNotIn("Locate a signed Star Empire loader package", source)
        self.assertNotIn("Verified UI Mod", source)
        self.assertIn('notebook.add(mods, text="Mods")', source)
        self.assertNotIn('notebook.add(dashboard, text="Advanced")', source)
        self.assertIn('notebook.add(logs, text="Logs")', source)
        self.assertIn('notebook.add(settings, text="Settings")', source)
        self.assertIn('"MOD SUPPORT ENABLED"', source)
        self.assertIn('"MOD SUPPORT DISABLED"', source)
        self.assertIn("command=self._toggle_manager_enabled", source)
        self.assertIn("self._recover_interrupted_automatically()", source)
        self.assertIn('filetypes=(("Star Empire Mod", "*.semod"),)', source)
        self.assertIn('title="Install a Star Empire mod"', source)
        self.assertIn("self._mods = ModManagerController()", source)
        self.assertIn("verify_semod_package(selected_path)", source)
        self.assertNotIn("verify_semod_package(selected_path, keys)", source)
        self.assertIn('text="Install Mod…"', source)
        self.assertIn('self.mods_tree.bind("<Button-1>"', source)
        self.assertIn('self.mods_tree.bind("<space>"', source)
        self.assertIn("mods.rowconfigure(3, weight=1)", source)
        self.assertIn(
            'mod_browser.grid(row=3, column=0, sticky="nsew")', source)
        self.assertIn('text="MOD INFORMATION"', source)
        self.assertIn('text="No preview image"', source)
        self.assertIn('self.mod_readme_text = Text(', source)
        self.assertIn(
            'mod_actions.grid(row=4, column=0, sticky="ew"', source)
        self.assertNotIn("self.mods_tree.pack(", source)
        self.assertNotIn("mod_actions.pack(", source)
        self.assertIn("_prepare_game_for_mod_install", source)
        self.assertIn("ModManagerController", source)
        self.assertIn("self._mods.set_enabled", source)
        self.assertIn("self._mods.uninstall", source)
        self.assertIn("Export Diagnostics ZIP…", source)
        self.assertIn("export_diagnostics(", source)
        self.assertIn("Enable verbose mod debug logging", source)
        self.assertIn("save_manager_settings(", source)
        self.assertIn("Check Selected for Update", source)
        self.assertIn("self._mods.check_update", source)
        self.assertIn("self._mods.apply_update", source)
        self.assertIn("self.log_text = Text(", source)
        self.assertIn('orient="vertical"', source)
        self.assertIn('orient="horizontal"', source)
        self.assertIn('text="Refresh Logs"', source)
        self.assertIn("class SetupProgressDialog", source)
        self.assertIn("Toplevel(root)", source)
        self.assertIn("AUTOMATIC GAME SETUP", source)

    def test_mod_details_load_declared_readme_and_optional_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            namespace = install / "mod" / "example" / "mod"
            namespace.mkdir(parents=True)
            (namespace / "README.md").write_text(
                "# Example Mod\n\nUseful details.", encoding="utf-8")
            preview = b"\x89PNG\r\n\x1a\npreview"
            (namespace / "preview.png").write_bytes(preview)
            installed = SimpleNamespace(
                install_path=install,
                manifest=SimpleNamespace(
                    mod_id="example.mod", name="Example Mod", version="1.2.3",
                    entrypoint="example_mod.entry:load",
                    description="Short description",
                    files=("mod/example/mod/README.md",
                           "mod/example/mod/preview.png")))

            details = load_mod_display_details(installed)

            self.assertEqual("Example Mod  1.2.3", details.title)
            self.assertIn("Useful details.", details.readme)
            self.assertEqual(preview, details.preview_png)

    def test_mod_details_only_read_files_declared_inside_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary) / "installed"
            namespace = install / "mod" / "example" / "mod"
            namespace.mkdir(parents=True)
            (namespace / "README.md").write_text(
                "undeclared text", encoding="utf-8")
            installed = SimpleNamespace(
                install_path=install,
                manifest=SimpleNamespace(
                    mod_id="example.mod", name="Example", version="1.0.0",
                    entrypoint="example_mod.entry:load",
                    description="Safe fallback", files=()))

            details = load_mod_display_details(installed)

            self.assertIn("Safe fallback", details.readme)
            self.assertNotIn("undeclared text", details.readme)
            self.assertIsNone(details.preview_png)

    def test_mod_details_bound_oversized_readme(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            namespace = install / "mod" / "example" / "mod"
            namespace.mkdir(parents=True)
            (namespace / "README.md").write_bytes(
                b"x" * (MOD_README_MAX_BYTES + 20))
            installed = SimpleNamespace(
                install_path=install,
                manifest=SimpleNamespace(
                    mod_id="example.mod", name="Example", version="1.0.0",
                    entrypoint="example_mod.entry:load", description="",
                    files=("mod/example/mod/README.md",)))

            details = load_mod_display_details(installed)

            self.assertTrue(details.readme.endswith(
                "[README truncated by the Mod Manager.]"))
            self.assertLess(len(details.readme), MOD_README_MAX_BYTES + 100)

    def test_global_switch_is_green_when_enabled_and_red_when_disabled(self):
        app = object.__new__(ManagerApp)
        app.manager_switch = unittest.mock.Mock()
        app.manager_hint = unittest.mock.Mock()
        app._transaction = SimpleNamespace(
            has_interrupted_operation=lambda: False)

        app._inspection = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY)
        app._update_manager_switch()
        enabled = app.manager_switch.configure.call_args.kwargs
        self.assertEqual("MOD SUPPORT ENABLED", enabled["text"])
        self.assertEqual("#198754", enabled["background"])

        app._inspection = SimpleNamespace(
            status=InstallStatus.READY_TO_INSTALL)
        app._update_manager_switch()
        disabled = app.manager_switch.configure.call_args.kwargs
        self.assertEqual("MOD SUPPORT DISABLED", disabled["text"])
        self.assertEqual("#b4232f", disabled["background"])

    def test_global_switch_disables_with_one_verified_restore(self) -> None:
        app = object.__new__(ManagerApp)
        inspection = SimpleNamespace(status=InstallStatus.INSTALLED_HEALTHY)
        app.game_path = SimpleNamespace(get=lambda: "C:/Star Empire")
        app._inspection = inspection
        app._candidate = object()
        transaction = SimpleNamespace(
            has_interrupted_operation=lambda: False,
            restore=unittest.mock.Mock())
        app._transaction = transaction
        app.refresh = unittest.mock.Mock()
        app._recover_interrupted_automatically = unittest.mock.Mock(
            return_value=True)
        app._prepare_game_for_mod_install = unittest.mock.Mock()

        app._toggle_manager_enabled()

        transaction.restore.assert_called_once_with(inspection)
        self.assertIsNone(app._candidate)
        app._prepare_game_for_mod_install.assert_not_called()

    def test_global_switch_enables_with_automatic_preparation(self) -> None:
        app = object.__new__(ManagerApp)
        app.game_path = SimpleNamespace(get=lambda: "C:/Star Empire")
        app._inspection = SimpleNamespace(status=InstallStatus.READY_TO_INSTALL)
        app._transaction = SimpleNamespace(
            has_interrupted_operation=lambda: False)
        app.refresh = unittest.mock.Mock()
        app._recover_interrupted_automatically = unittest.mock.Mock(
            return_value=True)
        app._prepare_game_for_mod_install = unittest.mock.Mock(return_value=True)

        app._toggle_manager_enabled()

        app._prepare_game_for_mod_install.assert_called_once_with()

    def test_automatic_preparation_recovers_interrupted_swap_first(self) -> None:
        app = object.__new__(ManagerApp)
        app.game_path = SimpleNamespace(get=lambda: "C:/Star Empire")
        app._package = object()
        app._inspection = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY, can_update=False)
        app.refresh = unittest.mock.Mock()
        app._recover_interrupted_automatically = unittest.mock.Mock(
            return_value=False)

        self.assertFalse(app._prepare_game_for_mod_install())
        app._recover_interrupted_automatically.assert_called_once_with()

    def test_clicking_mod_on_column_toggles_it_immediately(self) -> None:
        app = object.__new__(ManagerApp)
        app.mods_tree = unittest.mock.Mock()
        app.mods_tree.identify_column.return_value = "#1"
        app.mods_tree.identify_row.return_value = "example.mod"
        app._toggle_external_mod = unittest.mock.Mock()

        result = app._toggle_mod_from_click(SimpleNamespace(x=12, y=24))

        self.assertEqual("break", result)
        app.mods_tree.selection_set.assert_called_once_with("example.mod")
        app._toggle_external_mod.assert_called_once_with()

    def test_clicking_other_mod_columns_only_selects_normally(self) -> None:
        app = object.__new__(ManagerApp)
        app.mods_tree = unittest.mock.Mock()
        app.mods_tree.identify_column.return_value = "#2"
        app._toggle_external_mod = unittest.mock.Mock()

        result = app._toggle_mod_from_click(SimpleNamespace(x=80, y=24))

        self.assertIsNone(result)
        app._toggle_external_mod.assert_not_called()

    def test_install_stores_mod_disabled_without_preparing_game(self) -> None:
        app = object.__new__(ManagerApp)
        selected = Path("C:/Mods/example.semod").resolve()
        result = SimpleNamespace(mod_id="example.mod", version="1.0.0")
        app._mods = SimpleNamespace(
            install=unittest.mock.Mock(return_value=result))
        app._prepare_game_for_mod_install = unittest.mock.Mock()
        app.status_text = unittest.mock.Mock()
        app.summary_text = unittest.mock.Mock()
        app._refresh_mods = unittest.mock.Mock()

        with patch("installer.manager_ui.verify_semod_package") as verify:
            app._install_external_mod_paths((selected,))

        verify.assert_called_once_with(selected)
        app._mods.install.assert_called_once_with(selected, enable=False)
        app._prepare_game_for_mod_install.assert_not_called()
        app.status_text.set.assert_called_once_with(
            "example.mod 1.0.0 installed disabled")
        app._refresh_mods.assert_called_once_with()

    def test_enabling_mod_prepares_support_once_then_queues_next_launch(self) -> None:
        app = object.__new__(ManagerApp)
        app._selected_mod_id = lambda: "example.mod"
        app._mods = SimpleNamespace(
            installed=unittest.mock.Mock(return_value=SimpleNamespace(
                enabled=False)),
            set_enabled=unittest.mock.Mock(return_value=SimpleNamespace(
                mod_id="example.mod")))
        app._prepare_game_for_mod_install = unittest.mock.Mock(return_value=True)
        app.status_text = unittest.mock.Mock()
        app.summary_text = unittest.mock.Mock()
        app._refresh_mods = unittest.mock.Mock()

        app._toggle_external_mod()

        app._prepare_game_for_mod_install.assert_called_once_with()
        app._mods.set_enabled.assert_called_once_with("example.mod", True)
        app.status_text.set.assert_called_once_with(
            "example.mod will load on the next Star Empire launch")

    def test_disabling_mod_only_queues_disabled_next_launch(self) -> None:
        app = object.__new__(ManagerApp)
        app._selected_mod_id = lambda: "example.mod"
        app._mods = SimpleNamespace(
            installed=unittest.mock.Mock(return_value=SimpleNamespace(
                enabled=True)),
            set_enabled=unittest.mock.Mock(return_value=SimpleNamespace(
                mod_id="example.mod")))
        app._prepare_game_for_mod_install = unittest.mock.Mock()
        app.status_text = unittest.mock.Mock()
        app.summary_text = unittest.mock.Mock()
        app._refresh_mods = unittest.mock.Mock()

        app._toggle_external_mod()

        app._prepare_game_for_mod_install.assert_not_called()
        app._mods.set_enabled.assert_called_once_with("example.mod", False)
        app.status_text.set.assert_called_once_with(
            "example.mod is disabled for the next launch")

    def test_healthy_game_needs_no_manual_compatibility_steps_for_mod_install(self) -> None:
        app = object.__new__(ManagerApp)
        app.game_path = SimpleNamespace(get=lambda: "C:/Star Empire")
        app._package = object()
        app._inspection = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY, can_update=False)
        app.refresh = unittest.mock.Mock()
        app._transaction = SimpleNamespace(
            has_interrupted_operation=unittest.mock.Mock(return_value=False))

        self.assertTrue(app._prepare_game_for_mod_install())
        app._transaction.has_interrupted_operation.assert_called_once_with()

    def test_mod_install_automatically_updates_internal_loader(self) -> None:
        app = object.__new__(ManagerApp)
        game = Path("C:/Star Empire")
        baseline = Path("C:/Manager/backups/Client.exe")
        inspection = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY,
            can_install=False, can_update=True,
            game_root=game,
            version="0.4.61",
            state=SimpleNamespace(original_client=baseline),
            message="loader update ready")
        final = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY, can_update=False)
        app.game_path = SimpleNamespace(get=lambda: str(game))
        app._package = object()
        app._inspection = inspection
        app._work_root = Path("C:/Manager/work")
        app.root = object()
        transaction = unittest.mock.Mock()
        transaction.has_interrupted_operation.return_value = False
        app._transaction = transaction

        def refresh():
            if transaction.install_or_update.called:
                app._inspection = final

        app.refresh = refresh
        candidate = object()
        progress = unittest.mock.Mock()
        with patch(
                "installer.manager_ui.SetupProgressDialog",
                return_value=progress), patch(
                "installer.manager_ui.build_candidate",
                return_value=candidate) as build:
            self.assertTrue(app._prepare_game_for_mod_install())

        build.assert_called_once_with(
            app._package, game, app._work_root,
            baseline_client=baseline, compatibility_test=False)
        transaction.install_or_update.assert_called_once_with(
            inspection, candidate)
        messages = [call.args[0] for call in progress.step.call_args_list]
        self.assertEqual([
            "Checking the selected Star Empire build",
            "Verifying internal compatibility support",
            "The isolated loader build passed",
            "Backing up vanilla and enabling mod support",
            "Removing temporary build files",
            "Verifying the installed loader and mod list",
        ], messages)
        progress.complete.assert_called_once_with(
            "Mod support is enabled and ready for Star Empire.")
        progress.fail.assert_not_called()

    def test_game_update_automatically_rebases_bundled_loader(self) -> None:
        app = object.__new__(ManagerApp)
        game = Path("C:/Star Empire")
        package = SimpleNamespace(
            manifest={"schema": 2},
            release_profile=object(),
            release_binding=SimpleNamespace(
                ui_source_sha256={"host_client.py": "A" * 64}))
        inspection = SimpleNamespace(
            status=InstallStatus.UNSUPPORTED_VANILLA,
            can_install=False, can_update=False,
            game_root=game, version="0.4.67", current_sha256="B" * 64,
            pack=object(), state=SimpleNamespace(original_client=Path("old")),
            message="game update requires structural verification")
        final = SimpleNamespace(
            status=InstallStatus.INSTALLED_HEALTHY, can_update=False)
        app.root = object()
        app.game_path = SimpleNamespace(get=lambda: str(game))
        app._package = None
        app._inspection = None
        app._work_root = Path("C:/Manager/work")
        app._recover_interrupted_automatically = unittest.mock.Mock(
            return_value=True)
        transaction = unittest.mock.Mock()
        app._transaction = transaction

        def select_loader(selected_game):
            self.assertEqual(game, selected_game)
            app._package = package
            return True

        app._auto_select_local_loader = unittest.mock.Mock(
            side_effect=select_loader)

        def refresh():
            app._inspection = (
                final if transaction.install_or_update.called else inspection)

        app.refresh = refresh
        candidate = object()
        progress = unittest.mock.Mock()
        with patch(
                "installer.manager_ui.SetupProgressDialog",
                return_value=progress), patch(
                "installer.manager_ui.build_candidate",
                return_value=candidate) as build:
            self.assertTrue(app._prepare_game_for_mod_install())

        app._auto_select_local_loader.assert_called_once_with(game)
        build.assert_called_once_with(
            package, game, app._work_root,
            baseline_client=None, compatibility_test=True)
        transaction.install_or_update.assert_called_once_with(
            inspection, candidate)
        progress.complete.assert_called_once()

    def test_automatic_setup_progress_explains_safe_failure(self) -> None:
        app = object.__new__(ManagerApp)
        game = Path("C:/Star Empire")
        app.root = object()
        app.game_path = SimpleNamespace(get=lambda: str(game))
        app._package = object()
        app._inspection = SimpleNamespace(
            status=InstallStatus.READY_TO_INSTALL,
            can_install=True, can_update=False, game_root=game,
            state=None, version="0.4.62", message="ready")
        app._work_root = Path("C:/Manager/work")
        app.refresh = unittest.mock.Mock()
        app._recover_interrupted_automatically = unittest.mock.Mock(
            return_value=True)
        app._transaction = SimpleNamespace(
            has_interrupted_operation=lambda: False)
        progress = unittest.mock.Mock()

        with patch(
                "installer.manager_ui.SetupProgressDialog",
                return_value=progress), patch(
                "installer.manager_ui.build_candidate",
                side_effect=CandidateBuildError("required hook is missing")), patch(
                "installer.manager_ui.messagebox.showerror"):
            self.assertFalse(app._prepare_game_for_mod_install())

        progress.fail.assert_called_once_with("required hook is missing")
        progress.complete.assert_not_called()

    def test_diagnostics_refreshes_embedded_read_only_log_view(self):
        app = object.__new__(ManagerApp)
        app.game_path = SimpleNamespace(get=lambda: "C:/Star Empire")
        app._mods = SimpleNamespace(state_root=Path("state"))
        app._inspection = None
        app._candidate = None
        app._transaction = SimpleNamespace(journal_path=Path("journal.json"))
        app.log_text = unittest.mock.Mock()
        app.notebook = unittest.mock.Mock()
        app.logs_tab = object()

        with patch(
                "installer.manager_ui.current_diagnostics_text",
                return_value="CURRENT LOG TAILS\nlatest log line\n") as render:
            app._diagnostics(select_tab=False)

        render.assert_called_once_with(
            game_root=Path("C:/Star Empire"), mod_state_root=Path("state"))
        app.log_text.delete.assert_called_once_with("1.0", "end")
        inserted = app.log_text.insert.call_args.args[1]
        self.assertIn("Manager status: Not checked yet", inserted)
        self.assertIn("Compatibility package: Not selected", inserted)
        self.assertIn("Interrupted operation: None", inserted)
        self.assertIn("latest log line", inserted)
        self.assertNotIn("pack_digest=", inserted)
        self.assertEqual(
            [unittest.mock.call(state="normal"),
             unittest.mock.call(state="disabled")],
            app.log_text.configure.call_args_list)
        app.log_text.see.assert_called_once_with("end")
        app.notebook.select.assert_not_called()

        with patch(
                "installer.manager_ui.current_diagnostics_text",
                return_value="refreshed\n"):
            app._diagnostics()
        app.notebook.select.assert_called_once_with(app.logs_tab)

    def test_mod_update_result_requires_confirmation_before_apply(self):
        update = SimpleNamespace(
            package=SimpleNamespace(manifest=SimpleNamespace(
                name="Example", version="2.0.0")),
            repository="owner/repo")
        app = object.__new__(ManagerApp)
        app._mod_update_results = SimpleQueue()
        app._mod_update_results.put(("example.mod", update, None))
        app._mod_update_active = True
        app._set_mod_actions = unittest.mock.Mock()
        app._refresh_mods = unittest.mock.Mock()
        app._mods = SimpleNamespace(apply_update=unittest.mock.Mock())
        with patch("installer.manager_ui.messagebox.askyesno", return_value=False):
            app._poll_selected_mod_update()
        app._mods.apply_update.assert_not_called()
        self.assertFalse(app._mod_update_active)

        result = SimpleNamespace(mod_id="example.mod", version="2.0.0")
        app._mod_update_results.put(("example.mod", update, None))
        app._mod_update_active = True
        app._mods.apply_update.return_value = result
        with patch(
                "installer.manager_ui.messagebox.askyesno",
                return_value=True), patch(
                "installer.manager_ui.messagebox.showinfo"):
            app._poll_selected_mod_update()
        app._mods.apply_update.assert_called_once_with(update)
        app._refresh_mods.assert_called_once()

    def test_diagnostics_export_uses_selected_game_and_shared_mod_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            state = root / "state"
            destination = root / "diagnostics.zip"
            app = object.__new__(ManagerApp)
            app.game_path = SimpleNamespace(get=lambda: str(game))
            app._mods = SimpleNamespace(state_root=state)

            with patch(
                    "installer.manager_ui.filedialog.asksaveasfilename",
                    return_value=str(destination)), patch(
                    "installer.manager_ui.export_diagnostics",
                    return_value=destination) as export, patch(
                    "installer.manager_ui.messagebox.showinfo") as info:
                app._export_diagnostics()

            export.assert_called_once_with(
                destination, game_root=game, mod_state_root=state)
            info.assert_called_once()

    def test_diagnostics_export_refuses_overwrite_error(self):
        app = object.__new__(ManagerApp)
        app.game_path = SimpleNamespace(get=lambda: "")
        app._mods = SimpleNamespace(state_root=Path("state"))
        with patch(
                "installer.manager_ui.filedialog.asksaveasfilename",
                return_value="diagnostics.zip"), patch(
                "installer.manager_ui.export_diagnostics",
                side_effect=FileExistsError("already exists")), patch(
                "installer.manager_ui.messagebox.showerror") as error:
            app._export_diagnostics()
        error.assert_called_once()

    def test_debug_setting_saves_and_reverts_on_failure(self):
        app = object.__new__(ManagerApp)
        app._settings_path = Path("manager-settings.json")
        app._saved_debug_logging = False
        app.debug_logging = SimpleNamespace(
            get=lambda: True, set=unittest.mock.Mock())
        with patch("installer.manager_ui.save_manager_settings") as save:
            app._save_manager_settings()
        self.assertTrue(save.call_args.args[1].debug_logging)
        self.assertTrue(app._saved_debug_logging)

        app._saved_debug_logging = False
        with patch(
                "installer.manager_ui.save_manager_settings",
                side_effect=OSError("blocked")), patch(
                "installer.manager_ui.messagebox.showerror") as error:
            app._save_manager_settings()
        app.debug_logging.set.assert_called_once_with(False)
        error.assert_called_once()

    def test_registry_change_refreshes_visible_mod_list(self):
        app = object.__new__(ManagerApp)
        app._mod_registry_stamp = (10, 20)
        app._mod_registry_file_stamp = unittest.mock.Mock(
            return_value=(11, 21))
        app._refresh_mods = unittest.mock.Mock()

        app._refresh_mods_if_registry_changed()

        app._refresh_mods.assert_called_once_with()

    def test_unchanged_registry_does_not_rebuild_visible_mod_list(self):
        app = object.__new__(ManagerApp)
        app._mod_registry_stamp = (10, 20)
        app._mod_registry_file_stamp = unittest.mock.Mock(
            return_value=(10, 20))
        app._refresh_mods = unittest.mock.Mock()

        app._refresh_mods_if_registry_changed()

        app._refresh_mods.assert_not_called()

    def test_registry_poll_reschedules_after_check(self):
        app = object.__new__(ManagerApp)
        app.root = unittest.mock.Mock()
        app._mod_registry_watch_id = "previous"
        app._refresh_mods_if_registry_changed = unittest.mock.Mock()

        app._poll_mod_registry()

        app._refresh_mods_if_registry_changed.assert_called_once_with()
        app.root.after.assert_called_once_with(
            MOD_REGISTRY_POLL_MS, app._poll_mod_registry)

    def test_close_cancels_registry_watch(self):
        app = object.__new__(ManagerApp)
        app.root = unittest.mock.Mock()
        app._mod_registry_watch_id = "watch-1"
        app._candidate = None
        app._cleanup_candidate = unittest.mock.Mock(return_value=None)

        app._close_manager()

        app.root.after_cancel.assert_called_once_with("watch-1")
        app.root.destroy.assert_called_once_with()
        self.assertIsNone(app._mod_registry_watch_id)


if __name__ == "__main__":
    unittest.main()
