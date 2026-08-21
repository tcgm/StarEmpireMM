from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from installer.mod_manifest import (MOD_FORMAT, MOD_SCHEMA, ModManifestError,
                                    parse_mod_manifest)
from installer.mod_registry import (ModRegistry, ModRegistryError,
                                    load_mod_registry, save_mod_registry)


def _manifest(
        mod_id: str, *, version: str = "1.0.0", dependencies=(),
        conflicts=(), load_after=(), update=False, compatibility=None):
    module = mod_id.replace("-", "_")
    module_path = module.replace(".", "/")
    data = {
        "schema": MOD_SCHEMA,
        "format": MOD_FORMAT,
        "mod_id": mod_id,
        "name": mod_id.title(),
        "version": version,
        "author": "Test Author",
        "description": "Test-only authored mod",
        "loader_api": 1,
        "entrypoint": f"{module}:register",
        "files": [f"mod/{module_path}.py"],
        "dependencies": [
            {"mod_id": item[0], "version_min": item[1]}
            for item in dependencies
        ],
        "conflicts": list(conflicts),
        "load_after": list(load_after),
        "permissions": ["ui.render"],
    }
    if update:
        data["update"] = {
            "type": "github-release",
            "repository": "dezgard/example-mod",
            "asset": "example-*.semod",
        }
    if compatibility is not None:
        data["compatibility"] = compatibility
    return parse_mod_manifest(
        (json.dumps(data, separators=(",", ":")) + "\n").encode())


class ModManifestTests(unittest.TestCase):
    def test_manifest_is_strict_generic_and_github_update_ready(self):
        manifest = _manifest("example.mod", update=True)
        self.assertEqual("example.mod", manifest.mod_id)
        self.assertEqual("example.mod:register", manifest.entrypoint)
        self.assertEqual("dezgard/example-mod", manifest.update.repository)
        self.assertEqual(["mod/example/mod.py"], list(manifest.files))
        self.assertNotIn("Client.exe", json.dumps(manifest.to_mapping()))

    def test_exact_game_compatibility_round_trips_and_matches_both_fields(self):
        digest = "A" * 64
        manifest = _manifest("example.mod", compatibility={
            "mode": "exact-game-build",
            "game_builds": [{
                "game_version": "0.4.61",
                "client_sha256": digest.lower(),
            }],
        })
        self.assertTrue(manifest.compatibility.supports("0.4.61", digest))
        self.assertFalse(manifest.compatibility.supports("0.4.60", digest))
        self.assertFalse(manifest.compatibility.supports("0.4.61", "B" * 64))
        self.assertEqual(
            digest,
            manifest.to_mapping()["compatibility"]["game_builds"][0]
            ["client_sha256"],
        )

    def test_empty_exact_compatibility_intentionally_supports_no_build(self):
        manifest = _manifest("blocked.mod", compatibility={
            "mode": "exact-game-build", "game_builds": [],
        })
        self.assertFalse(
            manifest.compatibility.supports("0.4.61", "A" * 64))

    def test_game_version_compatibility_ignores_executable_hash(self):
        manifest = _manifest("version.mod", compatibility={
            "mode": "game-version", "game_versions": ["0.4.61"],
        })
        self.assertTrue(manifest.compatibility.supports(
            "0.4.61", "A" * 64))
        self.assertTrue(manifest.compatibility.supports(
            "0.4.61", "B" * 64))
        self.assertFalse(manifest.compatibility.supports(
            "0.4.60", "A" * 64))
        self.assertEqual(
            {"mode": "game-version", "game_versions": ["0.4.61"]},
            manifest.to_mapping()["compatibility"])

    def test_malformed_or_duplicate_compatibility_is_rejected(self):
        valid = {
            "game_version": "0.4.61", "client_sha256": "A" * 64,
        }
        invalid_values = (
            {"mode": "latest", "game_builds": []},
            {"mode": "game-version", "game_versions": []},
            {"mode": "game-version", "game_versions": ["0.4.61", "0.4.61"]},
            {"mode": "game-version", "game_versions": ["not-a-version"]},
            {"mode": "exact-game-build", "game_builds": "0.4.61"},
            {"mode": "exact-game-build", "game_builds": [
                {**valid, "client_sha256": "not-a-hash"}]},
            {"mode": "exact-game-build", "game_builds": [valid, valid]},
        )
        for compatibility in invalid_values:
            with self.subTest(compatibility=compatibility):
                with self.assertRaises(ModManifestError):
                    _manifest("blocked.mod", compatibility=compatibility)

    def test_duplicate_keys_unsafe_files_and_missing_entrypoint_are_rejected(self):
        duplicate = (
            b'{"schema":1,"schema":1,"format":"star-empire-mod"}')
        with self.assertRaisesRegex(ModManifestError, "duplicate"):
            parse_mod_manifest(duplicate)

        data = _manifest("safe.mod").to_mapping()
        data["files"] = ["../Client.py", "mod/safe_mod.py"]
        with self.assertRaisesRegex(ModManifestError, "unsafe"):
            parse_mod_manifest(json.dumps(data).encode())

        data = _manifest("safe.mod").to_mapping()
        data["files"] = ["mod/other.py"]
        with self.assertRaisesRegex(ModManifestError, "entrypoint module"):
            parse_mod_manifest(json.dumps(data).encode())

    def test_self_dependencies_conflicts_and_invalid_updates_are_rejected(self):
        for field, value in (
                ("dependencies", [{"mod_id": "self.mod", "version_min": "1.0.0"}]),
                ("conflicts", ["self.mod"]),
                ("load_after", ["self.mod"])):
            with self.subTest(field=field):
                data = _manifest("self.mod").to_mapping()
                data[field] = value
                with self.assertRaisesRegex(ModManifestError, "cannot"):
                    parse_mod_manifest(json.dumps(data).encode())

        data = _manifest("safe.mod").to_mapping()
        data["update"] = {
            "type": "github-release", "repository": "bad",
            "asset": "../bad.zip",
        }
        with self.assertRaises(ModManifestError):
            parse_mod_manifest(json.dumps(data).encode())


class ModRegistryTests(unittest.TestCase):
    def test_multiple_mods_round_trip_atomically_with_stable_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = ModRegistry.empty()
            registry = registry.with_installed(
                _manifest("core.mod"), root / "mods" / "core.mod",
                "A" * 64, source="local:core.semod")
            registry = registry.with_installed(
                _manifest("ui.mod", dependencies=(("core.mod", "1.0.0"),)),
                root / "mods" / "ui.mod", "B" * 64,
                source="github:dezgard/ui")
            path = save_mod_registry(root / "state" / "mods.json", registry)
            loaded = load_mod_registry(path)

            self.assertEqual(("core.mod", "ui.mod"), loaded.load_order)
            self.assertEqual(("core.mod", "ui.mod"), loaded.resolved_load_order())
            self.assertEqual("github:dezgard/ui", loaded.installed("ui.mod").source)
            self.assertFalse(loaded.installed("ui.mod").force_load)
            self.assertFalse(path.with_name(path.name + ".pending").exists())

            forced = loaded.with_force_load("ui.mod", True)
            save_mod_registry(path, forced)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["schema"])
            self.assertTrue(load_mod_registry(path).installed("ui.mod").force_load)

    def test_schema_one_registry_migrates_with_force_load_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = ModRegistry.empty().with_installed(
                _manifest("legacy.mod"), root / "mods" / "legacy.mod",
                "A" * 64, source="local:legacy.semod")
            payload = registry.to_mapping()
            payload["schema"] = 1
            for item in payload["mods"]:
                item.pop("force_load")
            path = root / "mods.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_mod_registry(path)

            self.assertFalse(loaded.installed("legacy.mod").force_load)
            save_mod_registry(path, loaded)
            self.assertEqual(
                2, json.loads(path.read_text(encoding="utf-8"))["schema"])

    def test_enable_requires_dependency_version_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = ModRegistry.empty().with_installed(
                _manifest("core.mod", version="1.0.0"),
                root / "core", "A" * 64, source="local", enabled=False)
            registry = registry.with_installed(
                _manifest("addon.mod", dependencies=(("core.mod", "2.0.0"),)),
                root / "addon", "B" * 64, source="local", enabled=False)
            with self.assertRaisesRegex(ModRegistryError, "requires enabled"):
                registry.with_enabled("addon.mod", True)
            registry = registry.with_enabled("core.mod", True)
            with self.assertRaisesRegex(ModRegistryError, ">= 2.0.0"):
                registry.with_enabled("addon.mod", True)

            conflict = ModRegistry.empty().with_installed(
                _manifest("first.mod"), root / "first", "C" * 64,
                source="local")
            with self.assertRaisesRegex(ModRegistryError, "conflicts"):
                conflict.with_installed(
                    _manifest("second.mod", conflicts=("first.mod",)),
                    root / "second", "D" * 64, source="local")

    def test_dependency_order_cycle_and_uninstall_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = ModRegistry.empty().with_installed(
                _manifest("base.mod"), root / "base", "A" * 64,
                source="local")
            registry = registry.with_installed(
                _manifest("child.mod", dependencies=(("base.mod", "1.0.0"),)),
                root / "child", "B" * 64, source="local")
            with self.assertRaisesRegex(ModRegistryError, "dependents"):
                registry.without("base.mod")
            registry = registry.with_enabled("child.mod", False)
            registry = registry.without("base.mod")
            self.assertEqual(("child.mod",), registry.load_order)

            cycle = ModRegistry.empty().with_installed(
                _manifest("one.mod", load_after=("two.mod",)),
                root / "one", "C" * 64, source="local", enabled=False)
            cycle = cycle.with_installed(
                _manifest("two.mod", load_after=("one.mod",)),
                root / "two", "D" * 64, source="local", enabled=False)
            cycle = cycle.with_enabled("one.mod", True)
            with self.assertRaisesRegex(ModRegistryError, "cycle"):
                cycle.with_enabled("two.mod", True)

    def test_corrupt_or_incomplete_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mods.json"
            path.write_text('{"schema":1,"mods":[],"load_order":["ghost.mod"]}',
                            encoding="utf-8")
            with self.assertRaisesRegex(ModRegistryError, "load_order"):
                load_mod_registry(path)
            path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
            with self.assertRaisesRegex(ModRegistryError, "duplicate"):
                load_mod_registry(path)
            path.write_text(json.dumps({
                "schema": 2,
                "mods": [{
                    **ModRegistry.empty().with_installed(
                        _manifest("bad.mod"), Path(temporary) / "bad",
                        "A" * 64, source="local").mods[0].to_mapping(),
                    "force_load": "yes",
                }],
                "load_order": ["bad.mod"],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ModRegistryError, "force_load"):
                load_mod_registry(path)


if __name__ == "__main__":
    unittest.main()
