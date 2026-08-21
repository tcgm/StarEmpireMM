from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from installer.semod_package import verify_semod_package
from tools.build_semod_package import SemodBuildError, build_semod_package


class BuildSemodPackageTests(unittest.TestCase):
    def _fixture(self, root: Path):
        source = root / "source"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        (source / "entry.py").write_text(
            "def register(api):\n    return None\n", encoding="utf-8")
        (source / "README.md").write_text(
            "# Example Mod\n\nTest package.\n", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema": 1,
            "format": "star-empire-mod",
            "mod_id": "example.mod",
            "name": "Example Mod",
            "version": "1.0.0",
            "author": "Test",
            "loader_api": 1,
            "entrypoint": "example.mod.entry:register",
            "files": [
                "mod/example/mod/README.md",
                "mod/example/mod/__init__.py",
                "mod/example/mod/entry.py",
            ],
            "dependencies": [], "conflicts": [], "load_after": [],
            "permissions": [],
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return source, manifest

    def test_build_is_deterministic_verified_and_extractable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self._fixture(root)
            first = build_semod_package(
                source_directory=source, manifest_path=manifest,
                output=root / "first.semod")
            second = build_semod_package(
                source_directory=source, manifest_path=manifest,
                output=root / "second.semod")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            verified = verify_semod_package(first)
            self.assertEqual("example.mod", verified.manifest.mod_id)
            extracted = verified.extract(root / "installed")
            self.assertTrue((extracted / "mod/example/mod/entry.py").is_file())
            self.assertTrue((extracted / "mod/example/mod/README.md").is_file())

    def test_readme_is_required_for_new_mod_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self._fixture(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"].remove("mod/example/mod/README.md")
            manifest.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(SemodBuildError, "README.md"):
                build_semod_package(
                    source_directory=source, manifest_path=manifest,
                    output=root / "missing-readme.semod")

    def test_extra_source_wrong_extension_and_overwrite_are_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self._fixture(root)
            (source / "undeclared.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(SemodBuildError, "inventory"):
                build_semod_package(
                    source_directory=source, manifest_path=manifest,
                    output=root / "bad.semod")
            (source / "undeclared.py").unlink()
            with self.assertRaisesRegex(SemodBuildError, r"\.semod extension"):
                build_semod_package(
                    source_directory=source, manifest_path=manifest,
                    output=root / "wrong.zip")
            existing = root / "existing.semod"
            existing.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                build_semod_package(
                    source_directory=source, manifest_path=manifest,
                    output=existing)
            self.assertEqual(b"keep", existing.read_bytes())

    def test_files_outside_mod_namespace_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self._fixture(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"] = ["mod/other/entry.py"]
            data["entrypoint"] = "other.entry:register"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SemodBuildError):
                build_semod_package(
                    source_directory=source, manifest_path=manifest,
                    output=root / "bad.semod")


if __name__ == "__main__":
    unittest.main()
