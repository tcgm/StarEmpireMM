from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from installer.manager_core import ProcessProbeResult
from installer.manager_entry import self_test
from tools.audit_manager_artifact import (FORBIDDEN_GAME_MODULES,
                                          ManagerArtifactError,
                                          audit_manager_artifact)


REQUIRED = {
    "installer.manager_ui", "installer.manager_core",
    "installer.manager_transaction", "installer.mod_package",
    "installer.hook_recipe", "installer.authored_fragments",
    "installer.candidate_builder",
    "installer.release_profiles", "installer.trusted_keys",
    "installer.update_feed", "installer.version",
    "installer.manager_mods", "installer.manager_settings",
    "installer.diagnostics", "installer.mod_manifest",
    "installer.mod_registry", "installer.mod_service",
    "installer.mod_updates", "installer.semod_package",
    "mod_loader", "mod_loader.host_client", "mod_loader.runtime",
    "tkinterdnd2", "tkinterdnd2.TkinterDnD",
    "tools.build_version_binding", "tools.repack_client",
}


class _Pyz:
    def __init__(self, names):
        self.toc = {name: (0, 0, 0) for name in names}


class _Archive:
    def __init__(self, names, outer=("PYZ.pyz", "manager_entry")):
        self.toc = {name: (0, 0, 0, 0, "z") for name in outer}
        self._pyz = _Pyz(names)

    def open_embedded_archive(self, name):
        if name != "PYZ.pyz":
            raise KeyError(name)
        return self._pyz


class ManagerBuildAuditTests(unittest.TestCase):
    def _manager(self, root: Path) -> Path:
        manager = root / "StarEmpireModManager.exe"
        manager.write_bytes(b"MZmanager-test")
        return manager

    def test_required_manager_inventory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            report = audit_manager_artifact(
                manager, reader_factory=lambda _path: _Archive(REQUIRED))
        self.assertEqual(len(REQUIRED), report.python_modules)

    def test_assembled_release_companions_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            (root / "README-FIRST.txt").write_text("read me", encoding="utf-8")
            (root / "RELEASE.json").write_text("{}", encoding="utf-8")
            (root / "SHA256SUMS.txt").write_text("hashes", encoding="utf-8")
            (root / "StarEmpireModLoader-0.4.47-v1.seloader").write_bytes(
                b"package")
            (root / "StarEmpireUIMod-0.1.0.semod").write_bytes(b"mod")
            report = audit_manager_artifact(
                manager, reader_factory=lambda _path: _Archive(REQUIRED))
        self.assertEqual(len(REQUIRED), report.python_modules)

    def test_unknown_or_multiple_release_companions_are_rejected(self) -> None:
        for names in (
                ("notes.txt",),
                ("one.seloader", "two.seloader"),
                ("one.seloader", "legacy.seuimod")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manager = self._manager(root)
                for name in names:
                    (root / name).write_bytes(b"unexpected")
                with self.assertRaisesRegex(ManagerArtifactError, "unexpected companions"):
                    audit_manager_artifact(
                        manager, reader_factory=lambda _path: _Archive(REQUIRED))

    def test_frozen_game_module_or_extra_dist_file_is_rejected(self) -> None:
        for forbidden in sorted(FORBIDDEN_GAME_MODULES):
            with self.subTest(forbidden=forbidden), tempfile.TemporaryDirectory() as temporary:
                manager = self._manager(Path(temporary))
                with self.assertRaisesRegex(ManagerArtifactError, "game modules"):
                    audit_manager_artifact(
                        manager,
                        reader_factory=lambda _path, name=forbidden:
                        _Archive(REQUIRED | {name}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            (root / "Client.py.patch").write_text("forbidden", encoding="utf-8")
            with self.assertRaisesRegex(ManagerArtifactError, "not one-file"):
                audit_manager_artifact(
                    manager, reader_factory=lambda _path: _Archive(REQUIRED))

    def test_spec_and_build_script_do_not_collect_repo_game_or_patchset(self) -> None:
        spec = Path("installer/StarEmpireUiModManager.spec").read_text(
            encoding="utf-8")
        script = Path("tools/build_manager.ps1").read_text(encoding="utf-8")
        self.assertIn('collect_submodules("PyInstaller.archive")', spec)
        self.assertIn('collect_submodules("mod_loader")', spec)
        self.assertIn('collect_submodules("tkinterdnd2")', spec)
        self.assertIn('collect_data_files("tkinterdnd2")', spec)
        self.assertIn('"win-x64", "win-x64-tcl9"', spec)
        self.assertIn("repository = Path(SPECPATH).parent", spec)
        self.assertIn('"STAR_EMPIRE_MANAGER_EMBEDDED_LOADERS"', spec)
        self.assertIn('"embedded_loaders"', spec)
        self.assertIn('loader_path.suffix.casefold() != ".seloader"', spec)
        self.assertIn('"tools.build_version_binding"', spec)
        self.assertIn('"tools.repack_client"', spec)
        self.assertNotIn("integration/patchsets", spec)
        self.assertNotIn("game/", spec)
        self.assertIn("audit_manager_artifact", script)
        self.assertIn("EmbeddedLoaderPackages", script)
        self.assertIn("STAR_EMPIRE_MANAGER_EMBEDDED_LOADERS", script)
        self.assertIn('Path]::PathSeparator', script)
        self.assertIn('ArgumentList "--self-test"', script)
        self.assertIn('"StarEmpireModManager.exe"', script)
        self.assertIn('manager_version = $managerVersion', script)
        self.assertIn('source_revision = $sourceRevision', script)
        self.assertIn('source_dirty = $sourceDirty', script)
        self.assertIn('Join-Path $dist "RELEASE.json"', script)
        entry = Path("installer/manager_entry.py").read_text(encoding="utf-8")
        self.assertIn("def self_test()", entry)
        self.assertIn('"--self-test" in sys.argv[1:]', entry)
        self.assertIn(
            "from tools.build_version_binding import locate_target_vitals_offsets",
            entry)
        self.assertIn(
            "from installer.semod_package import verify_semod_package", entry)
        self.assertIn("from mod_loader.runtime import ExternalModLoader", entry)

    def test_manager_self_test_requires_a_verified_process_snapshot(self) -> None:
        with patch(
                "installer.manager_core.running_game_processes",
                return_value=ProcessProbeResult()):
            self.assertEqual(0, self_test())
        with patch(
                "installer.manager_core.running_game_processes",
                return_value=ProcessProbeResult.unknown("snapshot failed")):
            self.assertEqual(3, self_test())

    def test_authored_fragment_catalog_is_required_in_frozen_manager(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            incomplete = REQUIRED - {"installer.authored_fragments"}
            with self.assertRaisesRegex(
                    ManagerArtifactError, "installer.authored_fragments"):
                audit_manager_artifact(
                    manager,
                    reader_factory=lambda _path: _Archive(incomplete))

    def test_compatibility_scanner_is_required_in_frozen_manager(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            incomplete = REQUIRED - {"tools.build_version_binding"}
            with self.assertRaisesRegex(
                    ManagerArtifactError, "tools.build_version_binding"):
                audit_manager_artifact(
                    manager,
                    reader_factory=lambda _path: _Archive(incomplete))


if __name__ == "__main__":
    unittest.main()
