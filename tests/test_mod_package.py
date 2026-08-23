"""Security-focused tests for the signed loader package boundary."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from installer.authored_fragments import resolve_active_authored_fragment
from installer.mod_package import PackageError, verify_mod_package
from installer.release_profiles import (MOD_LOADER_POLICY,
                                        TARGET_VITALS_ALPHA_POLICY,
                                        TARGET_VITALS_ALPHA_POLICY_ID)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


class ModPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def _package(self, root: Path, payloads: dict[str, bytes] | None = None,
                 mutate_manifest=None, extra_entries=()) -> Path:
        payloads = payloads or {"payload/ui_mod/example.py": b"VALUE = 1\n"}
        manifest = {
            "schema": 1,
            "format": "star-empire-ui-mod",
            "key_id": "test-key",
            "pack_id": "ui-test-1",
            "mod_version": "1.0.0",
            "game_version": "0.4.42",
            "manager_version_min": "0.1.0",
            "official_client_sha256": "A" * 64,
            "expected_client_sha256": "B" * 64,
            "payloads": [
                {"path": path, "size": len(data), "sha256": _sha(data)}
                for path, data in sorted(payloads.items())
            ],
        }
        if mutate_manifest is not None:
            mutate_manifest(manifest)
        manifest_bytes = (json.dumps(
            manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        package = root / "test.seuimod"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("manifest.sig", self.private.sign(manifest_bytes))
            for path, data in payloads.items():
                archive.writestr(path, data)
            for name, data in extra_entries:
                archive.writestr(name, data)
        return package

    def _dynamic_package(self, root: Path, *, binding_mutator=None,
                         recipe_mutator=None, manifest_mutator=None,
                         payload_mutator=None,
                         policy=TARGET_VITALS_ALPHA_POLICY) -> Path:
        policy_base = (policy.policy_id[:-3]
                       if policy.policy_id.endswith("-v1")
                       else policy.policy_id)
        profile_id = f"{policy_base}-0.4.45-aaaaaaaaaaaa-v1"
        offsets = {
            fragment_id: offset for fragment_id, offset in zip(
                policy.fragment_ids,
                range(651462, 651462 + 200000 * len(policy.fragment_ids),
                      200000), strict=True)
        }
        recipe = {
            "schema": 2,
            "format": "star-empire-ui-hook-recipe",
            "game_version": "0.4.45",
            "modules": [{
                "module": module_name, "path": module_path,
                "input_sha256": "C" * 64,
                "output_sha256": "D" * 64,
                "operations": [{
                    "kind": "insert_fragment",
                    "offset": offsets[fragment_id],
                    "fragment_id": fragment_id,
                    "fragment_sha256": resolve_active_authored_fragment(
                        fragment_id).sha256,
                } for fragment_id in policy.fragment_ids
                  if resolve_active_authored_fragment(
                      fragment_id).module == module_name],
            } for module_name, module_path in policy.hosts],
        }
        if recipe_mutator is not None:
            recipe_mutator(recipe)
        recipe_bytes = (json.dumps(
            recipe, sort_keys=True, separators=(",", ":")) + "\n").encode()
        recipe_name = f"{profile_id}.hook"
        ui_payloads = {
            f"payload/{policy.payload_directory}/{name}":
            f'"""{name}"""\n'.encode()
            for name in policy.ui_source_files
        }
        binding = {
            "schema": 2,
            "format": "star-empire-ui-release-binding",
            "policy_id": policy.policy_id,
            "profile_id": profile_id,
            "game_version": "0.4.45",
            "official_client_sha256": "A" * 64,
            "expected_client_sha256": "B" * 64,
            "host_input_sha256": {
                name: "C" * 64 for name, _path in policy.hosts
            },
            "host_output_sha256": {
                name: "D" * 64 for name, _path in policy.hosts
            },
            "fragment_offsets": offsets,
            "ui_source_files": list(policy.ui_source_files),
            "ui_source_sha256": {
                name: _sha(ui_payloads[
                    f"payload/{policy.payload_directory}/{name}"])
                for name in policy.ui_source_files
            },
            "recipe_file": recipe_name,
            "recipe_sha256": _sha(recipe_bytes),
        }
        if binding_mutator is not None:
            binding_mutator(binding)
        binding_bytes = (json.dumps(
            binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
        binding_path = f"payload/bindings/{profile_id}.binding.json"
        payloads = {
            binding_path: binding_bytes,
            f"payload/recipes/{recipe_name}": recipe_bytes,
        }
        payloads.update(ui_payloads)
        if payload_mutator is not None:
            payload_mutator(payloads)

        def configure(manifest):
            manifest["schema"] = 2
            manifest["game_version"] = "0.4.45"
            manifest["release_binding"] = binding_path
            if manifest_mutator is not None:
                manifest_mutator(manifest)

        return self._package(root, payloads, mutate_manifest=configure)

    def test_valid_package_verifies_and_extracts_only_declared_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root)
            verified = verify_mod_package(package, {"test-key": self.public})
            extracted = verified.extract_payload(root / "extracted")

            self.assertEqual("ui-test-1", verified.compatibility.pack_id)
            self.assertEqual(verified.manifest_sha256,
                             verified.compatibility.pack_digest)
            self.assertEqual("test-key", verified.compatibility.key_id)
            self.assertEqual("VALUE = 1\n", (extracted / "ui_mod" / "example.py").read_text())
            self.assertFalse((extracted / "manifest.json").exists())
            self.assertFalse(tuple(root.glob(".extracted.*.partial")))

    def test_failed_extract_removes_destination_local_partial_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root)
            verified = verify_mod_package(package, {"test-key": self.public})
            package.write_bytes(b"not a zip anymore")

            with self.assertRaises(zipfile.BadZipFile):
                verified.extract_payload(root / "extracted")

            self.assertFalse((root / "extracted").exists())
            self.assertFalse(tuple(root.glob(".extracted.*.partial")))

    def test_seloader_is_primary_and_legacy_seuimod_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = self._package(root)
            current = legacy.with_suffix(".seloader")
            legacy.replace(current)

            verified = verify_mod_package(current, {"test-key": self.public})
            self.assertEqual("ui-test-1", verified.compatibility.pack_id)

            current.replace(legacy)
            verified_legacy = verify_mod_package(
                legacy, {"test-key": self.public})
            self.assertEqual("ui-test-1", verified_legacy.compatibility.pack_id)

            wrong = legacy.with_suffix(".zip")
            legacy.replace(wrong)
            with self.assertRaisesRegex(PackageError, "\\.seloader"):
                verify_mod_package(wrong, {"test-key": self.public})

    def test_tampered_manifest_or_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root)
            with zipfile.ZipFile(package, "a") as archive:
                archive.writestr("manifest.json", b"{}")
            with self.assertRaises(PackageError):
                verify_mod_package(package, {"test-key": self.public})

            package = self._package(root)
            with zipfile.ZipFile(package, "a") as archive:
                archive.writestr("payload/ui_mod/example.py", b"changed")
            with self.assertRaises(PackageError):
                verify_mod_package(package, {"test-key": self.public})

    def test_unknown_signing_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._package(Path(temporary))
            with self.assertRaisesRegex(PackageError, "not trusted"):
                verify_mod_package(package, {})

    def test_traversal_case_collision_and_undeclared_entries_are_rejected(self) -> None:
        cases = (
            (("../escape.py", b"x"),),
            (("payload/UI_MOD/example.py", b"x"),),
            (("surprise.txt", b"x"),),
        )
        for extras in cases:
            with self.subTest(extras=extras), tempfile.TemporaryDirectory() as temporary:
                package = self._package(Path(temporary), extra_entries=extras)
                with self.assertRaises(PackageError):
                    verify_mod_package(package, {"test-key": self.public})

    def test_executable_bytecode_private_patch_and_game_host_source_are_rejected(self) -> None:
        bad_payloads = {
            "payload/ui_mod/tool.exe": b"MZpayload",
            "payload/ui_mod/cache.pyc": b"bytecode",
            "payload/recipes/Client.py": b"game source",
            "payload/recipes/Client.py.patch": b"private diff",
        }
        for path, data in bad_payloads.items():
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temporary:
                package = self._package(Path(temporary), {path: data})
                with self.assertRaises(PackageError):
                    verify_mod_package(package, {"test-key": self.public})

    def test_payload_inventory_must_match_manifest_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._package(
                Path(temporary),
                mutate_manifest=lambda manifest: manifest["payloads"].clear())
            with self.assertRaisesRegex(PackageError, "at least one payload"):
                verify_mod_package(package, {"test-key": self.public})

    def test_newer_or_malformed_manager_requirement_is_rejected(self) -> None:
        for required in ("99.0.0", "latest"):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as temporary:
                package = self._package(
                    Path(temporary), mutate_manifest=lambda manifest, value=required:
                    manifest.__setitem__("manager_version_min", value))
                with self.assertRaisesRegex(PackageError, "Manager|manager"):
                    verify_mod_package(package, {"test-key": self.public})

    def test_legacy_loader_capability_is_independent_of_public_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._package(
                Path(temporary),
                mutate_manifest=lambda manifest: manifest.__setitem__(
                    "manager_version_min", "0.4.6"))

            verified = verify_mod_package(package, {"test-key": self.public})

        self.assertEqual("ui-test-1", verified.compatibility.pack_id)

    def test_valid_dynamic_package_authenticates_its_closed_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._dynamic_package(root)
            verified = verify_mod_package(package, {"test-key": self.public})
            self.assertIsNotNone(verified.release_binding)
            self.assertIsNotNone(verified.release_profile)
            self.assertEqual(
                "target-vitals-alpha-0.4.45-aaaaaaaaaaaa-v1",
                verified.release_profile.profile_id)
            self.assertEqual("0.4.45", verified.release_profile.game_version)
            self.assertEqual(
                TARGET_VITALS_ALPHA_POLICY.ui_source_files,
                verified.release_profile.ui_source_files)

    def test_valid_loader_package_uses_independent_frozen_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._dynamic_package(
                root, policy=MOD_LOADER_POLICY)
            verified = verify_mod_package(
                package, {"test-key": self.public})

            self.assertEqual(
                "star_empire_mod_loader",
                verified.release_profile.source_package)
            self.assertEqual(
                "mod_loader", verified.release_profile.payload_directory)
            self.assertEqual(
                MOD_LOADER_POLICY.frozen_module_names,
                verified.release_profile.frozen_module_names)
            self.assertEqual(
                "A" * 64, verified.compatibility.official_client_sha256)

    def test_dynamic_binding_policy_hash_and_inventory_tampering_is_rejected(self):
        cases = (
            {"binding_mutator": lambda value: value.__setitem__(
                "policy_id", "unknown")},
            {"binding_mutator": lambda value: value.__setitem__(
                "recipe_sha256", "0" * 64)},
            {"binding_mutator": lambda value: value.__setitem__(
                "ui_source_files", value["ui_source_files"][:-1])},
            {"binding_mutator": lambda value: value["fragment_offsets"].pop(
                TARGET_VITALS_ALPHA_POLICY.fragment_ids[0])},
            {"recipe_mutator": lambda value: value["modules"][0][
                "operations"].pop()},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temporary:
                package = self._dynamic_package(Path(temporary), **kwargs)
                with self.assertRaises(PackageError):
                    verify_mod_package(package, {"test-key": self.public})

    def test_dynamic_manifest_must_match_binding_and_cannot_downgrade(self):
        cases = (
            lambda value: value.__setitem__("game_version", "0.4.46"),
            lambda value: value.__setitem__("official_client_sha256", "E" * 64),
            lambda value: value.__setitem__("expected_client_sha256", "E" * 64),
            lambda value: value.__setitem__("release_profile", "legacy"),
            lambda value: value.__setitem__(
                "release_binding", "payload/bindings/other.binding.json"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                package = self._dynamic_package(
                    Path(temporary), manifest_mutator=mutate)
                with self.assertRaises(PackageError):
                    verify_mod_package(package, {"test-key": self.public})

    def test_dynamic_payload_must_be_exact_and_complete(self):
        def remove_binding(payloads):
            key = next(key for key in payloads if key.endswith(".binding.json"))
            payloads.pop(key)

        def remove_ui(payloads):
            payloads.pop("payload/ui_mod/theme.py")

        def add_extra(payloads):
            payloads["payload/ui_mod/extra.py"] = b"VALUE = 1\n"

        def change_bound_source(payloads):
            payloads["payload/ui_mod/theme.py"] += b"# changed after binding\n"

        for mutate in (remove_binding, remove_ui, add_extra, change_bound_source):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                package = self._dynamic_package(
                    Path(temporary), payload_mutator=mutate)
                with self.assertRaises(PackageError):
                    verify_mod_package(package, {"test-key": self.public})


if __name__ == "__main__":
    unittest.main()
