from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from installer.manager_mods import (
    ModManagerController,
    default_mod_state_root,
)
from installer.manager_core import ProcessProbeResult
from installer.mod_manifest import GithubUpdateSource
from installer.mod_service import ModService
from installer.mod_updates import AcquiredModUpdate
from installer.semod_package import SemodPackageError
from mod_loader.host_client import STATE_ROOT_ENV

from tests.test_mod_service import _package


class ManagerModsTests(unittest.TestCase):
    def test_default_root_exactly_matches_loader_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict("os.environ", {STATE_ROOT_ENV: temporary}):
                self.assertEqual(
                    Path(temporary).resolve(), default_mod_state_root())

    def test_install_list_toggle_update_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            first = _package(root / "packages", "example.mod")
            second = _package(
                root / "packages", "example.mod", "1.1.0", token=b"two")
            controller = ModManagerController(root / "state", service=service)

            with patch(
                    "installer.manager_mods.verify_semod_package",
                    side_effect=(first, second)):
                installed = controller.install(root / "first.semod")
                updated = controller.install(
                    root / "second.semod", source="github:owner/repo")

            self.assertEqual("install", installed.action)
            self.assertEqual("update", updated.action)
            self.assertEqual(1, len(controller.summaries()))
            summary = controller.summaries()[0]
            self.assertEqual("example.mod", summary.mod_id)
            self.assertEqual("1.1.0", summary.version)
            self.assertFalse(summary.enabled)
            self.assertFalse(summary.force_load)
            self.assertEqual("github:owner/repo", summary.source)

            controller.set_force_load("example.mod", True)
            self.assertTrue(controller.summaries()[0].force_load)

            controller.toggle("example.mod")
            self.assertTrue(controller.summaries()[0].enabled)
            removed = controller.uninstall("example.mod")
            self.assertEqual("uninstall", removed.action)
            self.assertEqual((), controller.summaries())

    def test_invalid_package_and_running_game_fail_before_mod_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = ModManagerController(root / "state")
            with patch(
                    "installer.manager_mods.verify_semod_package",
                    side_effect=SemodPackageError("invalid package")):
                with self.assertRaisesRegex(SemodPackageError, "invalid"):
                    controller.install(root / "test.semod")

            package = _package(root / "packages", "example.mod")
            service = ModService(
                root / "running",
                process_probe=lambda: ProcessProbeResult(("Client.exe",)))
            controller = ModManagerController(
                root / "running", service=service)
            with patch(
                    "installer.manager_mods.verify_semod_package",
                    return_value=package):
                with self.assertRaisesRegex(Exception, "close Star Empire"):
                    controller.install(root / "test.semod")
            self.assertEqual((), controller.summaries())

    def test_github_update_is_authenticated_and_stale_install_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            first = _package(root / "packages", "example.mod")
            update = _package(
                root / "packages", "example.mod", "2.0.0", token=b"two")
            source = GithubUpdateSource("owner/repo", "example-*.semod")
            first = replace(first, manifest=replace(first.manifest, update=source))
            update = replace(update, manifest=replace(update.manifest, update=source))
            controller = ModManagerController(root / "state", service=service)
            service.install(first, source="local:first.semod")
            previous = controller.installed("example.mod")
            acquired = AcquiredModUpdate(
                update, root / "update.semod", "owner/repo",
                previous.package_sha256)

            with patch("installer.manager_mods.acquire_github_update",
                       return_value=acquired) as check:
                self.assertIs(acquired, controller.check_update("example.mod"))
            check.assert_called_once()
            result = controller.apply_update(acquired)
            self.assertEqual("update", result.action)
            self.assertEqual("github:owner/repo",
                             controller.installed("example.mod").source)

            stale = AcquiredModUpdate(
                first, root / "old.semod", "owner/repo",
                previous.package_sha256)
            with self.assertRaisesRegex(Exception, "changed while"):
                controller.apply_update(stale)


if __name__ == "__main__":
    unittest.main()
