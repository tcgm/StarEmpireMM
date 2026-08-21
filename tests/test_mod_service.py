from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from installer.manager_core import ProcessProbeResult
from installer.mod_manifest import parse_mod_manifest
from installer.mod_registry import ModRegistryError
from installer.mod_service import ModService, ModServiceError
from installer.semod_package import SemodPayload, VerifiedSemodPackage


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _package(
        root: Path, mod_id: str, version: str = "1.0.0", *,
        dependencies=(), conflicts=(), token: bytes = b"v1"):
    module = mod_id.replace("-", "_")
    module_path = module.replace(".", "/")
    manifest_data = {
        "schema": 1,
        "format": "star-empire-mod",
        "mod_id": mod_id,
        "name": mod_id.title(),
        "version": version,
        "author": "Test Author",
        "loader_api": 1,
        "entrypoint": f"{module}:register",
        "files": [f"mod/{module_path}.py"],
        "dependencies": [
            {"mod_id": item[0], "version_min": item[1]}
            for item in dependencies
        ],
        "conflicts": list(conflicts),
        "load_after": [],
        "permissions": ["ui.render"],
    }
    manifest_bytes = (json.dumps(
        manifest_data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    module_bytes = b"TOKEN = " + repr(token).encode() + b"\n"
    data = {
        "manifest.json": manifest_bytes,
        f"mod/{module_path}.py": module_bytes,
    }
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{mod_id}-{version}-{token.hex()}.semod"
    with zipfile.ZipFile(archive, "w") as zipped:
        for name, payload in data.items():
            zipped.writestr(name, payload)
    payloads = tuple(
        SemodPayload(PurePosixPath(name), len(payload), _sha(payload))
        for name, payload in sorted(data.items()))
    return VerifiedSemodPackage(
        archive, parse_mod_manifest(manifest_bytes),
        _sha(archive.read_bytes()), "F" * 64, payloads)


class ModServiceTests(unittest.TestCase):
    def test_install_enable_disable_and_recoverable_uninstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            package = _package(root / "packages", "example.mod")

            installed = service.install(package, source="local:example.semod")
            self.assertEqual("install", installed.action)
            self.assertTrue(installed.install_path.is_dir())
            self.assertTrue(service.registry().installed("example.mod").enabled)
            receipt = json.loads((
                installed.install_path / ".semod-install.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(1, receipt["schema"])
            self.assertEqual("keyless-external-mod", receipt["key_id"])
            self.assertEqual({
                "schema", "package_sha256", "package_manifest_sha256",
                "key_id", "payloads",
            }, set(receipt))

            service.set_enabled("example.mod", False)
            self.assertFalse(service.registry().installed("example.mod").enabled)
            service.set_enabled("example.mod", True)
            service.set_force_load("example.mod", True)
            self.assertTrue(service.registry().installed("example.mod").force_load)
            service.set_force_load("example.mod", False)
            self.assertFalse(service.registry().installed("example.mod").force_load)
            result = service.uninstall("example.mod")

            self.assertEqual("uninstall", result.action)
            self.assertFalse(installed.install_path.exists())
            self.assertTrue(result.removed_path.is_dir())
            self.assertEqual((), service.registry().mods)

    def test_update_switches_registry_and_archives_previous_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            first = _package(root / "packages", "example.mod", token=b"one")
            second = _package(
                root / "packages", "example.mod", "1.1.0", token=b"two")
            old = service.install(first, source="local:first")
            service.set_force_load("example.mod", True)
            updated = service.install(second, source="github:second")

            item = service.registry().installed("example.mod")
            self.assertEqual("update", updated.action)
            self.assertEqual("1.1.0", item.manifest.version)
            self.assertEqual(second.package_sha256, item.package_sha256)
            self.assertTrue(item.force_load)
            self.assertTrue(updated.install_path.is_dir())
            self.assertFalse(old.install_path.exists())
            self.assertTrue(updated.removed_path.is_dir())

    def test_reinstall_repairs_changed_same_package_and_preserves_force_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            package = _package(root / "packages", "example.mod")
            installed = service.install(package, source="local:first")
            service.set_force_load("example.mod", True)
            module = installed.install_path / "mod" / "example" / "mod.py"
            module.write_bytes(b"changed\n")

            repaired = service.install(package, source="local:repair")

            self.assertEqual("repair", repaired.action)
            self.assertTrue(repaired.removed_path.is_dir())
            self.assertEqual(b"TOKEN = b'v1'\n", module.read_bytes())
            item = service.registry().installed("example.mod")
            self.assertTrue(item.enabled)
            self.assertTrue(item.force_load)

    def test_dependencies_conflicts_and_uninstall_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            child = _package(
                root / "packages", "child.mod",
                dependencies=(("base.mod", "1.0.0"),))
            with self.assertRaisesRegex(ModRegistryError, "requires enabled"):
                service.install(child, source="local:child")
            self.assertFalse(service._install_target(child).exists())
            self.assertEqual((), service.registry().mods)

            base = _package(root / "packages", "base.mod")
            service.install(base, source="local:base")
            service.install(child, source="local:child")
            with self.assertRaisesRegex(ModRegistryError, "dependents"):
                service.uninstall("base.mod")
            service.set_enabled("child.mod", False)
            service.uninstall("base.mod")
            self.assertEqual(("child.mod",), service.registry().load_order)

    def test_uninstall_unregisters_inaccessible_legacy_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            package = _package(root / "packages", "legacy.mod")
            installed = service.install(package, source="local:legacy")
            original_replace = Path.replace

            def deny_legacy_move(path, target):
                if path == installed.install_path:
                    raise PermissionError(5, "Access is denied", str(path))
                return original_replace(path, target)

            with patch.object(Path, "replace", new=deny_legacy_move):
                result = service.uninstall("legacy.mod")

            self.assertEqual("uninstall-legacy-orphan", result.action)
            self.assertIsNone(result.removed_path)
            self.assertTrue(installed.install_path.is_dir())
            self.assertEqual((), service.registry().mods)

    def test_running_or_unknown_process_state_blocks_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _package(root / "packages", "example.mod")
            running = ModService(
                root / "running", process_probe=lambda: ("Client.exe",))
            with self.assertRaisesRegex(ModServiceError, "close Star Empire"):
                running.install(package, source="local")
            self.assertFalse(running.mods_root.exists())

            unknown = ModService(
                root / "unknown",
                process_probe=lambda: ProcessProbeResult.unknown("probe failed"))
            with self.assertRaisesRegex(ModServiceError, "cannot verify"):
                unknown.install(package, source="local")
            self.assertFalse(unknown.mods_root.exists())

    def test_registry_save_failure_rolls_back_new_install_and_uninstall_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ModService(root / "state", process_probe=lambda: ())
            package = _package(root / "packages", "example.mod")
            with (patch("installer.mod_service.save_mod_registry",
                        side_effect=OSError("disk full")),
                  self.assertRaisesRegex(OSError, "disk full")):
                service.install(package, source="local")
            self.assertFalse(service._install_target(package).exists())

            installed = service.install(package, source="local")
            with (patch("installer.mod_service.save_mod_registry",
                        side_effect=OSError("disk full")),
                  self.assertRaisesRegex(OSError, "disk full")):
                service.uninstall("example.mod")
            self.assertTrue(installed.install_path.is_dir())
            self.assertTrue(service.registry().installed("example.mod").enabled)


if __name__ == "__main__":
    unittest.main()
