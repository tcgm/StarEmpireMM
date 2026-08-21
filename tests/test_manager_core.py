"""Tests for the fail-closed installer prototype status model."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from installer.manager_core import (CompatibilityPack, InstallState,
                                    InstallStatus, ProcessProbeResult,
                                    _windows_process_names,
                                    inspect_installation,
                                    running_game_processes,
                                    save_install_state)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


class ManagerCoreTests(unittest.TestCase):
    def test_default_state_uses_general_mod_manager_root(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": "C:/LocalData"}):
            from installer.manager_core import default_state_path
            self.assertEqual(
                Path("C:/LocalData/StarEmpireModManager/install-state.json"),
                default_state_path())

    def _game(self, root: Path, payload: bytes = b"official",
              version: str = "0.4.39") -> Path:
        game = root / "Star Empire"
        game.mkdir()
        (game / "Client.exe").write_bytes(payload)
        (game / "StarEmpireLauncher.exe").write_bytes(b"launcher")
        (game / "version.txt").write_text(version + "\n", encoding="utf-8")
        return game

    def _pack(self, root: Path, official: bytes = b"official",
              installed: bytes = b"modded", pack_id: str = "ui-0.1.56",
              game_version: str = "0.4.39",
              pack_digest: str = "") -> CompatibilityPack:
        return CompatibilityPack.from_mapping({
            "schema": 1,
            "pack_id": pack_id,
            "mod_version": "0.1.56",
            "game_version": game_version,
            "official_client_sha256": _sha(official),
            "expected_client_sha256": _sha(installed),
            "pack_digest": pack_digest,
        }, root / "pack.json")

    def test_exact_official_build_is_ready_only_with_a_selected_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root)
            no_pack = inspect_installation(game, None, root / "state.json")
            ready = inspect_installation(game, self._pack(root), root / "state.json")

        self.assertEqual(InstallStatus.NO_UPDATE_PACK, no_pack.status)
        self.assertEqual(InstallStatus.READY_TO_INSTALL, ready.status)
        self.assertTrue(ready.can_install)

    def test_running_game_blocks_every_mutating_action_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = inspect_installation(
                self._game(root), self._pack(root), root / "state.json",
                running_processes=("Client.exe",))

        self.assertEqual(InstallStatus.GAME_RUNNING, result.status)
        self.assertIsNone(result.current_sha256)

    def test_native_process_probe_filters_game_processes_case_insensitively(self) -> None:
        process_names = (
            "explorer.exe", "Client.exe", "CLIENT.EXE",
            "StarEmpireLauncher.exe",
        )
        with patch("installer.manager_core.os.name", "nt"), patch(
                "installer.manager_core._windows_process_names",
                return_value=process_names):
            result = running_game_processes()

        self.assertTrue(result.verified)
        self.assertEqual(
            ("client.exe", "starempirelauncher.exe"), result.names)

    def test_native_process_probe_failure_remains_fail_closed(self) -> None:
        with patch("installer.manager_core.os.name", "nt"), patch(
                "installer.manager_core._windows_process_names",
                side_effect=OSError("snapshot denied")):
            result = running_game_processes()

        self.assertFalse(result.verified)
        self.assertEqual((), result.names)
        self.assertIn("snapshot denied", result.error or "")

    @unittest.skipUnless(os.name == "nt", "Windows Tool Help API only")
    def test_windows_native_process_enumeration_succeeds(self) -> None:
        process_names = _windows_process_names()

        self.assertTrue(process_names)
        self.assertTrue(all(isinstance(name, str) and name
                            for name in process_names))

    def test_unknown_process_state_blocks_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = inspect_installation(
                self._game(root), self._pack(root), root / "state.json",
                running_processes=ProcessProbeResult.unknown("tasklist failed"))

        self.assertEqual(InstallStatus.PROCESS_CHECK_FAILED, result.status)
        self.assertIsNone(result.current_sha256)
        self.assertFalse(result.can_install)
        self.assertIn("tasklist failed", result.message)

    def test_verified_install_state_allows_restore_but_never_reinstalls_blindly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"modded")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official"), _sha(b"modded"), "ui-0.1.55"))
            result = inspect_installation(game, self._pack(root), state_path)

        self.assertEqual(InstallStatus.INSTALLED_HEALTHY, result.status)
        self.assertTrue(result.can_restore)
        self.assertTrue(result.can_update)
        self.assertFalse(result.can_install)

    def test_installed_client_rejects_update_pack_for_another_official_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"modded")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official"), _sha(b"modded"), "ui-0.1.55"))
            wrong_pack = self._pack(
                root, official=b"new official", installed=b"new modded",
                pack_id="ui-0.1.57", game_version="0.4.40")
            result = inspect_installation(game, wrong_pack, state_path)

        self.assertEqual(InstallStatus.INSTALLED_HEALTHY, result.status)
        self.assertTrue(result.can_restore)
        self.assertFalse(result.can_update)
        self.assertIn("different official game build", result.message)

    def test_supported_launcher_update_is_recognised_as_a_new_vanilla_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"new official", version="0.4.40")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official"), _sha(b"modded"), "ui-0.1.55"))
            new_pack = self._pack(
                root, official=b"new official", installed=b"new modded",
                pack_id="ui-0.1.57", game_version="0.4.40")
            result = inspect_installation(game, new_pack, state_path)

        self.assertEqual(InstallStatus.READY_TO_INSTALL, result.status)
        self.assertTrue(result.can_install)
        self.assertFalse(result.can_restore)
        self.assertIn("new version-specific vanilla backup", result.message)

    def test_unknown_hash_after_reported_version_change_enters_structural_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"new unbound official", version="0.4.62")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official 0.4.61")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official 0.4.61"),
                _sha(b"modded 0.4.61"), "loader-0.4.61",
                game_version="0.4.61"))
            old_pack = self._pack(
                root, official=b"official 0.4.61",
                installed=b"modded 0.4.61", pack_id="loader-0.4.61",
                game_version="0.4.61")

            result = inspect_installation(game, old_pack, state_path)

        self.assertEqual(InstallStatus.UNSUPPORTED_VANILLA, result.status)
        self.assertEqual(_sha(b"new unbound official"), result.current_sha256)
        self.assertIsNotNone(result.state)
        self.assertIn("structurally verify", result.message)
        self.assertFalse(result.can_install)
        self.assertFalse(result.can_restore)

    def test_same_version_unknown_hash_remains_blocked_as_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"unknown same-version client",
                              version="0.4.61")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official 0.4.61")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official 0.4.61"),
                _sha(b"modded 0.4.61"), "loader-0.4.61",
                game_version="0.4.61"))

            result = inspect_installation(
                game, self._pack(root, official=b"official 0.4.61",
                                 installed=b"modded 0.4.61",
                                 pack_id="loader-0.4.61",
                                 game_version="0.4.61"), state_path)

        self.assertEqual(
            InstallStatus.MODIFIED_OUTSIDE_MANAGER, result.status)
        self.assertIn("no overwrite is safe", result.message)

    def test_new_version_without_loader_package_requests_one_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"official 0.4.62", version="0.4.62")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official 0.4.61")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official 0.4.61"),
                _sha(b"modded 0.4.61"), "loader-0.4.61",
                game_version="0.4.61"))

            result = inspect_installation(game, None, state_path)

        self.assertEqual(InstallStatus.NO_UPDATE_PACK, result.status)
        self.assertIn("new game version", result.message)

    def test_matching_hash_with_mismatched_reported_version_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, version="0.4.40")
            result = inspect_installation(
                game, self._pack(root, game_version="0.4.39"),
                root / "state.json")

        self.assertEqual(InstallStatus.UNSUPPORTED_VANILLA, result.status)
        self.assertFalse(result.can_install)

    def test_official_json_string_version_is_normalised_before_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, version=json.dumps("0.4.39"))
            result = inspect_installation(
                game, self._pack(root, game_version="0.4.39"),
                root / "state.json")

        self.assertEqual("0.4.39", result.version)
        self.assertEqual(InstallStatus.READY_TO_INSTALL, result.status)

    def test_malformed_or_non_string_json_version_fails_closed(self) -> None:
        for payload in ('"0.4.39', '{"version":"0.4.39"}'):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    game = self._game(root, version=payload)
                    result = inspect_installation(
                        game, self._pack(root), root / "state.json")
                self.assertEqual(
                    InstallStatus.UNSUPPORTED_VANILLA, result.status)
                self.assertIsNone(result.current_sha256)

    def test_invalid_utf8_version_fails_closed_before_client_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root)
            (game / "version.txt").write_bytes(b"\xff\xfe\x80")

            result = inspect_installation(
                game, self._pack(root), root / "state.json")

        self.assertEqual(InstallStatus.UNSUPPORTED_VANILLA, result.status)
        self.assertIsNone(result.current_sha256)
        self.assertIn("cannot read game version file", result.message)

    def test_changed_client_or_backup_blocks_restore_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"unknown")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"wrong backup")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official"), _sha(b"modded"), "ui-0.1.55"))
            result = inspect_installation(game, self._pack(root), state_path)

        self.assertEqual(InstallStatus.BACKUP_INVALID, result.status)
        self.assertFalse(result.can_restore)

    def test_unknown_game_never_becomes_supported_from_an_old_install_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"new official update")
            result = inspect_installation(game, self._pack(root), root / "state.json")

        self.assertEqual(InstallStatus.UNSUPPORTED_VANILLA, result.status)
        self.assertFalse(result.can_install)

    def test_state_schema_one_migrates_without_fabricating_signature_evidence(self) -> None:
        legacy = InstallState.from_mapping({
            "schema": 1,
            "game_root": "C:/Game",
            "original_client": "C:/Backup/Client.exe",
            "original_sha256": "A" * 64,
            "installed_sha256": "B" * 64,
            "pack_id": "legacy",
            "created_at": "yesterday",
        })
        self.assertEqual("", legacy.pack_digest)
        self.assertEqual("", legacy.compatibility_source_game_version)
        self.assertEqual(3, legacy.to_mapping()["schema"])

    def test_state_schema_two_migrates_without_fabricating_version_bridge(self) -> None:
        previous = InstallState.from_mapping({
            "schema": 2,
            "game_root": "C:/Game",
            "original_client": "C:/Backup/Client.exe",
            "original_sha256": "A" * 64,
            "installed_sha256": "B" * 64,
            "pack_id": "loader-0.4.61",
            "created_at": "yesterday",
            "game_version": "0.4.61",
        })

        self.assertEqual("0.4.61", previous.game_version)
        self.assertEqual("", previous.compatibility_source_game_version)
        self.assertEqual(3, previous.to_mapping()["schema"])

    def test_state_schema_three_round_trips_explicit_version_bridge(self) -> None:
        state = InstallState.new(
            Path("C:/Game"), Path("C:/Backup/Client.exe"),
            "A" * 64, "B" * 64, "loader-0.4.61",
            game_version="0.4.62",
            compatibility_source_game_version="0.4.61")

        reloaded = InstallState.from_mapping(state.to_mapping())

        self.assertEqual("0.4.62", reloaded.game_version)
        self.assertEqual(
            "0.4.61", reloaded.compatibility_source_game_version)

    def test_state_schema_three_rejects_missing_or_invalid_version_bridge(self) -> None:
        base = InstallState.new(
            Path("C:/Game"), Path("C:/Backup/Client.exe"),
            "A" * 64, "B" * 64, "loader").to_mapping()
        missing = dict(base)
        missing.pop("compatibility_source_game_version")
        invalid = dict(base)
        invalid["compatibility_source_game_version"] = "0.4.61\nunsafe"

        with self.assertRaisesRegex(
                Exception, "compatibility_source_game_version"):
            InstallState.from_mapping(missing)
        with self.assertRaisesRegex(Exception, "invalid characters"):
            InstallState.from_mapping(invalid)

    def test_reused_pack_id_with_different_digest_is_never_an_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root, b"modded")
            backup = root / "manager-backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes(b"official")
            state_path = root / "state.json"
            save_install_state(state_path, InstallState.new(
                game, backup, _sha(b"official"), _sha(b"modded"),
                "ui-0.1.56", pack_digest="A" * 64))
            changed = self._pack(
                root, pack_id="ui-0.1.56", pack_digest="B" * 64)
            result = inspect_installation(game, changed, state_path)

        self.assertEqual(InstallStatus.INSTALLED_HEALTHY, result.status)
        self.assertFalse(result.can_update)
        self.assertIn("reuses an installed pack ID", result.message)


if __name__ == "__main__":
    unittest.main()
