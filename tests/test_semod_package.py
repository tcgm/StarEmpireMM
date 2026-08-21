from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from installer.semod_package import (SEMOD_FORMAT, SEMOD_SCHEMA,
                                     SemodPackageError,
                                     verify_semod_package)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


class SemodPackageTests(unittest.TestCase):
    def _package(self, root: Path, *, mutate_package=None, extras=None,
                 module_payload: bytes = b"def register(api):\n    return None\n"):
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": 1,
            "format": "star-empire-mod",
            "mod_id": "example.mod",
            "name": "Example Mod",
            "version": "1.0.0",
            "author": "Test Author",
            "description": "Test-only mod",
            "loader_api": 1,
            "entrypoint": "example.mod:register",
            "files": ["mod/example/mod.py"],
            "dependencies": [],
            "conflicts": [],
            "load_after": [],
            "permissions": ["ui.render"],
            "update": {
                "type": "github-release",
                "repository": "dezgard/example-mod",
                "asset": "example-*.semod",
            },
        }
        manifest_bytes = (json.dumps(
            manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        payload_data = {
            "manifest.json": manifest_bytes,
            "mod/example/mod.py": module_payload,
        }
        payloads = [{
            "path": name, "size": len(payload), "sha256": _sha(payload),
        } for name, payload in sorted(payload_data.items())]
        package = {
            "schema": SEMOD_SCHEMA,
            "format": SEMOD_FORMAT,
            "manifest_sha256": _sha(manifest_bytes),
            "payloads": payloads,
        }
        if mutate_package is not None:
            mutate_package(package, payload_data)
        package_bytes = (json.dumps(
            package, sort_keys=True, separators=(",", ":")) + "\n").encode()
        output = root / "example.semod"
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("package.json", package_bytes)
            for name, payload in payload_data.items():
                archive.writestr(name, payload)
            for name, payload in extras or ():
                archive.writestr(name, payload)
        return output

    def test_keyless_mod_verifies_and_extracts_exact_declared_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root)
            verified = verify_semod_package(package)
            extracted = verified.extract(root / "installed" / "example.mod")

            self.assertEqual("example.mod", verified.manifest.mod_id)
            self.assertEqual("1.0.0", verified.manifest.version)
            self.assertEqual(_sha(package.read_bytes()), verified.package_sha256)
            self.assertEqual(
                b"def register(api):\n    return None\n",
                (extracted / "mod" / "example" / "mod.py").read_bytes())
            self.assertTrue((extracted / "manifest.json").is_file())

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_extracted_directory_is_not_owner_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verified = verify_semod_package(self._package(root))
            previous_umask = os.umask(0o022)
            try:
                extracted = verified.extract(root / "installed" / "example.mod")
            finally:
                os.umask(previous_umask)

            mode = stat.S_IMODE(extracted.stat().st_mode)
            self.assertEqual(0o755, mode)

    def test_tampered_payload_and_inventory_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root / "payload")
            with zipfile.ZipFile(package, "a") as archive:
                archive.writestr("mod/example/mod.py", b"changed")
            with self.assertRaises(SemodPackageError):
                verify_semod_package(package)

            package = self._package(
                root / "extra", extras=(("mod/undeclared.py", b"pass\n"),))
            with self.assertRaisesRegex(SemodPackageError, "inventory"):
                verify_semod_package(package)

    def test_unsafe_game_host_binary_and_manifest_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def host_name(package, payloads):
                payload = payloads.pop("mod/example/mod.py")
                payloads["mod/Client.py"] = payload
                package["payloads"] = [{
                    "path": name, "size": len(data), "sha256": _sha(data),
                } for name, data in sorted(payloads.items())]

            package = self._package(root / "host", mutate_package=host_name)
            with self.assertRaisesRegex(SemodPackageError, "game-owned"):
                verify_semod_package(package)

            package = self._package(
                root / "binary", module_payload=b"MZ" + b"\0" * 20)
            with self.assertRaisesRegex(SemodPackageError, "executable"):
                verify_semod_package(package)

            def missing_declared_file(package, _payloads):
                package["payloads"] = package["payloads"][:-1]

            package = self._package(
                root / "missing", mutate_package=missing_declared_file)
            with self.assertRaises(SemodPackageError):
                verify_semod_package(package)

    def test_wrong_suffix_and_overwrite_are_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root)
            wrong = root / "example.zip"
            wrong.write_bytes(package.read_bytes())
            with self.assertRaisesRegex(SemodPackageError, r"\.semod"):
                verify_semod_package(wrong)
            verified = verify_semod_package(package)
            destination = root / "installed"
            destination.mkdir()
            with self.assertRaisesRegex(SemodPackageError, "overwrite"):
                verified.extract(destination)


if __name__ == "__main__":
    unittest.main()
