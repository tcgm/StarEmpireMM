from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path, PurePosixPath
import zipfile

from installer.authored_fragments import (
    CLIENT_ALPHA_TARGET_BOOTSTRAP_V1,
    RENDER_FRAMEWORK_CORE_INSTALL_V5,
    resolve_authored_fragment,
)
from installer.candidate_builder import (BuiltCandidate, CandidateBuildError,
                                         build_candidate)
from installer.manager_core import CompatibilityPack
from installer.mod_package import PayloadEntry, VerifiedModPackage
from installer.release_profiles import (ReleaseProfile,
                                        FULL_UI_POLICY,
                                        MOD_LOADER_POLICY,
                                        TARGET_VITALS_ALPHA_POLICY,
                                        parse_release_binding,
                                        profile_from_signed_binding)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


class _Package:
    def __init__(self, payloads: dict[str, bytes], compatibility: CompatibilityPack):
        self._data = payloads
        self._compatibility = compatibility
        self.manifest_sha256 = compatibility.pack_digest
        self.payloads = tuple(
            PayloadEntry(PurePosixPath(path), len(data), _sha(data))
            for path, data in payloads.items())

    @property
    def compatibility(self):
        return self._compatibility

    def extract_payload(self, destination: Path) -> Path:
        destination.mkdir(parents=True)
        for path, data in self._data.items():
            output = destination / Path(*PurePosixPath(path).parts[1:])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        return destination


class CandidateBuilderTests(unittest.TestCase):
    def _setup(self, root: Path, *, expected: bytes = b"candidate"):
        official_exe = b"official exe"
        official_source = b"print('game')\n# obsolete host method\n"
        removed = b"# obsolete host method\n"
        rebuilt_source = b"print('game')\n"
        render_source = b"class RenderMixin:\n    pass\n"
        fragment = resolve_authored_fragment(
            RENDER_FRAMEWORK_CORE_INSTALL_V5)
        rebuilt_render = render_source + fragment.payload
        game = root / "game"
        (game / "_internal").mkdir(parents=True)
        (game / "Client.exe").write_bytes(official_exe)
        (game / "version.txt").write_text("test\n", encoding="utf-8")
        (game / "_internal" / "Client.py").write_bytes(official_source)
        (game / "_internal" / "render_mixin.py").write_bytes(render_source)
        recipe = json.dumps({
            "schema": 2,
            "format": "star-empire-ui-hook-recipe",
            "game_version": "test",
            "modules": [
                {
                    "module": "Client", "path": "Client.py",
                    "input_sha256": _sha(official_source),
                    "output_sha256": _sha(rebuilt_source),
                    "operations": [{
                        "kind": "delete",
                        "start": len(rebuilt_source),
                        "end": len(official_source),
                        "preimage_sha256": _sha(removed),
                    }],
                },
                {
                    "module": "render_mixin", "path": "render_mixin.py",
                    "input_sha256": _sha(render_source),
                    "output_sha256": _sha(rebuilt_render),
                    "operations": [{
                        "kind": "insert_fragment",
                        "offset": len(render_source),
                        "fragment_id": fragment.fragment_id,
                        "fragment_sha256": fragment.sha256,
                    }],
                },
            ],
        }).encode()
        pack = CompatibilityPack(
            "pack", "1.0", "test", _sha(official_exe), _sha(expected), root,
            "D" * 64, "test-key")
        package = _Package({
            "payload/recipes/test.hook": recipe,
            "payload/ui_mod/__init__.py": b"VALUE = 1\n",
        }, pack)
        return game, package, expected, rebuilt_source, fragment

    def _dynamic_setup(self, root: Path,
                       policy=TARGET_VITALS_ALPHA_POLICY):
        official_exe = b"official 0.4.45 exe"
        expected = b"dynamic candidate"
        host_sources = {
            "Client": b"# reviewed official Client source\n" * 8,
        }
        if any(name == "render_mixin" for name, _path in policy.hosts):
            host_sources["render_mixin"] = (
                b"# reviewed official render source\n" * 8)
        offsets = {}
        rebuilt_sources = {}
        for module_name, _module_path in policy.hosts:
            fragment_ids = [
                fragment_id for fragment_id in policy.fragment_ids
                if resolve_authored_fragment(fragment_id).module == module_name
            ]
            module_offsets = {
                fragment_id: index * 32
                for index, fragment_id in enumerate(fragment_ids)
            }
            offsets.update(module_offsets)
            rebuilt = bytearray(host_sources[module_name])
            for fragment_id, offset in sorted(
                    module_offsets.items(),
                    key=lambda item: item[1], reverse=True):
                rebuilt[offset:offset] = resolve_authored_fragment(
                    fragment_id).payload
            rebuilt_sources[module_name] = bytes(rebuilt)
        policy_base = (policy.policy_id[:-3]
                       if policy.policy_id.endswith("-v1")
                       else policy.policy_id)
        profile_id = (
            f"{policy_base}-0.4.45-"
            f"{_sha(official_exe)[:12].lower()}-v1")
        recipe_name = profile_id + ".hook"
        recipe = {
            "schema": 2,
            "format": "star-empire-ui-hook-recipe",
            "game_version": "0.4.45",
            "modules": [{
                "module": module_name,
                "path": module_path,
                "input_sha256": _sha(host_sources[module_name]),
                "output_sha256": _sha(rebuilt_sources[module_name]),
                "operations": [{
                    "kind": "insert_fragment",
                    "offset": offsets[fragment_id],
                    "fragment_id": fragment_id,
                    "fragment_sha256": resolve_authored_fragment(
                        fragment_id).sha256,
                } for fragment_id in policy.fragment_ids
                  if resolve_authored_fragment(
                      fragment_id).module == module_name],
            } for module_name, module_path in policy.hosts],
        }
        recipe_bytes = (json.dumps(
            recipe, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ui_payloads = {
            name: f'"""{name}"""\n'.encode()
            for name in policy.ui_source_files
        }
        binding = {
            "schema": 2,
            "format": "star-empire-ui-release-binding",
            "policy_id": policy.policy_id,
            "profile_id": profile_id,
            "game_version": "0.4.45",
            "official_client_sha256": _sha(official_exe),
            "expected_client_sha256": _sha(expected),
            "host_input_sha256": {
                name: _sha(source) for name, source in host_sources.items()
            },
            "host_output_sha256": {
                name: _sha(source) for name, source in rebuilt_sources.items()
            },
            "fragment_offsets": offsets,
            "ui_source_files": list(policy.ui_source_files),
            "ui_source_sha256": {
                name: _sha(payload) for name, payload in ui_payloads.items()
            },
            "recipe_file": recipe_name,
            "recipe_sha256": _sha(recipe_bytes),
        }
        binding_bytes = (json.dumps(
            binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
        release_binding = parse_release_binding(binding_bytes)
        release_profile = profile_from_signed_binding(
            binding_bytes, recipe_bytes)
        binding_path = f"payload/bindings/{profile_id}.binding.json"
        payload_data = {
            binding_path: binding_bytes,
            f"payload/recipes/{recipe_name}": recipe_bytes,
            **{f"payload/{policy.payload_directory}/{name}": payload
               for name, payload in ui_payloads.items()},
        }
        archive_path = root / "dynamic.seuimod"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for path, data in payload_data.items():
                archive.writestr(path, data)
        payloads = tuple(
            PayloadEntry(PurePosixPath(path), len(data), _sha(data))
            for path, data in sorted(payload_data.items()))
        manifest = {
            "schema": 2,
            "pack_id": "dynamic-pack", "mod_version": "1.0",
            "game_version": "0.4.45",
            "official_client_sha256": _sha(official_exe),
            "expected_client_sha256": _sha(expected),
            "key_id": "test-key", "release_binding": binding_path,
        }
        package = VerifiedModPackage(
            archive_path, manifest, "F" * 64, payloads,
            release_binding, release_profile)
        game = root / "game-dynamic"
        (game / "_internal").mkdir(parents=True)
        (game / "Client.exe").write_bytes(official_exe)
        (game / "version.txt").write_text('"0.4.45"\n', encoding="utf-8")
        for module_name, module_path in policy.hosts:
            (game / "_internal" / module_path).write_bytes(
                host_sources[module_name])
        return game, package, expected, profile_id

    @staticmethod
    def _compatible_unknown_source() -> bytes:
        return b'''class RenderMixin:\n    def _draw_panel(self):\n        pass\n\nclass SolarSystemWindow(RenderMixin):\n    def run(self):\n        self._refresh_desktop_size()\n        screen = self._apply_display_mode(self._fullscreen)\n        logger.info("ready")\n        while self._running:\n            for event in events:\n                if (event.type != pygame.QUIT\n                        and self._handle_turret_gui_event(event)):\n                    continue\n                if event.type == pygame.QUIT:\n                    self._running = False\n            self._profiler.mark("warp_flash")\n            self._draw_warp_flash(frame)\n            self._profiler.mark("turret_layout")\n            self._draw_turret_layout(frame)\n\nif __name__ == "__main__":\n    start()\n'''

    def test_build_is_isolated_hash_bound_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, expected, rebuilt_source, fragment = self._setup(root)
            calls = []

            def repack(input_exe, source, output, modules):
                calls.append((input_exe, source.read_bytes(), tuple(modules)))
                output.write_bytes(expected)

            built = build_candidate(
                package, game, root / "work", repack=repack)
            self.assertIsInstance(built, BuiltCandidate)
            self.assertEqual(expected, built.path.read_bytes())
            self.assertEqual(b"official exe", (game / "Client.exe").read_bytes())
            self.assertEqual(rebuilt_source, calls[0][1])
            self.assertEqual(
                ("Client", "render_mixin", "star_empire_ui_mod"),
                calls[0][2])
            self.assertEqual(built.path, built.verify_for(package.compatibility))
            audit = json.loads(built.audit_path.read_text(encoding="utf-8"))
            self.assertEqual("D" * 64, audit["pack_digest"])
            self.assertEqual(_sha(package._data["payload/recipes/test.hook"]),
                             audit["recipe_sha256"])
            self.assertEqual([{
                "fragment_id": fragment.fragment_id,
                "module": "render_mixin",
                "sha256": fragment.sha256,
            }], audit["authored_fragments"])
            temporary_root = built.temporary_root
            self.assertIsNotNone(temporary_root)
            self.assertTrue(temporary_root.is_dir())
            built.cleanup()
            self.assertFalse(temporary_root.exists())

    def test_wrong_vanilla_or_wrong_candidate_is_removed_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _source, _fragment = self._setup(root)
            (game / "Client.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(CandidateBuildError, "official baseline"):
                build_candidate(package, game, root / "work")

            game, package, _expected, _source, _fragment = self._setup(
                root / "second", expected=b"expected")
            with self.assertRaisesRegex(CandidateBuildError, "expected SHA"):
                build_candidate(
                    package, game, root / "second" / "work",
                    repack=lambda _input, _source, output, _modules:
                    output.write_bytes(b"wrong"))
            self.assertEqual([], list((root / "second" / "work").iterdir()))

    def test_installed_client_can_rebuild_from_its_verified_vanilla_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, expected, _source, _fragment = self._setup(root)
            backup = root / "backups" / "Client.exe"
            backup.parent.mkdir()
            backup.write_bytes((game / "Client.exe").read_bytes())
            (game / "Client.exe").write_bytes(b"currently modded")

            built = build_candidate(
                package, game, root / "work", baseline_client=backup,
                repack=lambda input_exe, _source, output, _modules: (
                    self.assertEqual(backup, input_exe), output.write_bytes(expected))[1])

            self.assertEqual(expected, built.path.read_bytes())
            self.assertEqual(b"currently modded", (game / "Client.exe").read_bytes())

    def test_official_json_string_version_builds_without_quote_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, expected, _source, _fragment = self._setup(root)
            (game / "version.txt").write_text(
                json.dumps("test") + "\n", encoding="utf-8")

            built = build_candidate(
                package, game, root / "work",
                repack=lambda _input, _source, output, _modules:
                output.write_bytes(expected),
            )

            self.assertEqual(expected, built.path.read_bytes())

    def test_malformed_json_version_is_blocked_before_work_directory_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _source, _fragment = self._setup(root)
            (game / "version.txt").write_text('"test\n', encoding="utf-8")
            with self.assertRaisesRegex(
                    CandidateBuildError, "version file could not be verified"):
                build_candidate(package, game, root / "work")
            self.assertFalse((root / "work").exists())

    def test_invalid_utf8_version_is_blocked_before_work_directory_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _source, _fragment = self._setup(root)
            (game / "version.txt").write_bytes(b"\xff\xfe\x80")

            with self.assertRaisesRegex(
                    CandidateBuildError, "version file could not be verified"):
                build_candidate(package, game, root / "work")

            self.assertFalse((root / "work").exists())

    def test_real_package_requires_a_known_release_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _source, _fragment = self._setup(root)
            base_manifest = {
                "pack_id": "pack", "mod_version": "1.0",
                "game_version": "test",
                "official_client_sha256": _sha(b"official exe"),
                "expected_client_sha256": _sha(b"candidate"),
                "key_id": "test-key",
            }
            for label, profile_id in (("missing", None),
                                      ("unknown", "not-a-profile")):
                with self.subTest(label=label):
                    manifest = dict(base_manifest)
                    if profile_id is not None:
                        manifest["release_profile"] = profile_id
                    verified = VerifiedModPackage(
                        root / f"{label}.seuimod", manifest,
                        "D" * 64, package.payloads)
                    work = root / f"work-{label}"
                    with self.assertRaisesRegex(
                            CandidateBuildError, "release profile"):
                        build_candidate(verified, game, work)
                    self.assertFalse(work.exists())

    def test_real_profile_enforces_inventory_recipe_and_module_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            official_exe = b"official exe"
            expected = b"candidate"
            official_source = b"print('game')\n"
            fragment = resolve_authored_fragment(
                CLIENT_ALPHA_TARGET_BOOTSTRAP_V1)
            rebuilt_source = official_source + fragment.payload
            profile = ReleaseProfile(
                profile_id="fixture-profile",
                game_version="test",
                official_client_sha256=_sha(official_exe),
                host_input_sha256={"Client": _sha(official_source)},
                fragment_offsets={fragment.fragment_id: len(official_source)},
                ui_source_files=("__init__.py",),
            )
            recipe = json.dumps({
                "schema": 2,
                "format": "star-empire-ui-hook-recipe",
                "game_version": "test",
                "modules": [{
                    "module": "Client", "path": "Client.py",
                    "input_sha256": _sha(official_source),
                    "output_sha256": _sha(rebuilt_source),
                    "operations": [{
                        "kind": "insert_fragment",
                        "offset": len(official_source),
                        "fragment_id": fragment.fragment_id,
                        "fragment_sha256": fragment.sha256,
                    }],
                }],
            }).encode()
            game = root / "game"
            (game / "_internal").mkdir(parents=True)
            (game / "Client.exe").write_bytes(official_exe)
            (game / "version.txt").write_text("test\n", encoding="utf-8")
            (game / "_internal" / "Client.py").write_bytes(official_source)

            def make_package(name: str, payload_data: dict[str, bytes]):
                archive_path = root / f"{name}.seuimod"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for path, data in payload_data.items():
                        archive.writestr(path, data)
                payloads = tuple(
                    PayloadEntry(PurePosixPath(path), len(data), _sha(data))
                    for path, data in payload_data.items()
                )
                return VerifiedModPackage(
                    archive_path,
                    {
                        "pack_id": "pack", "mod_version": "1.0",
                        "game_version": "test",
                        "official_client_sha256": _sha(official_exe),
                        "expected_client_sha256": _sha(expected),
                        "key_id": "test-key",
                        "release_profile": profile.profile_id,
                    },
                    "D" * 64,
                    payloads,
                )

            valid_payload = {
                "payload/recipes/test.hook": recipe,
                "payload/ui_mod/__init__.py": b"VALUE = 1\n",
            }
            package = make_package("valid", valid_payload)
            calls = []

            def repack(_input, _source, output, modules):
                calls.append(tuple(modules))
                output.write_bytes(expected)

            with patch(
                    "installer.candidate_builder.release_profile",
                    return_value=profile):
                built = build_candidate(
                    package, game, root / "work", repack=repack)
            self.assertEqual(("Client", "star_empire_ui_mod"), calls[0])
            audit = json.loads(built.audit_path.read_text(encoding="utf-8"))
            self.assertEqual(profile.profile_id, audit["release_profile"])

            extra = dict(valid_payload)
            extra["payload/ui_mod/theme.py"] = b"VALUE = 2\n"
            extra_package = make_package("extra", extra)
            with (patch("installer.candidate_builder.release_profile",
                        return_value=profile),
                  self.assertRaisesRegex(
                      CandidateBuildError, "outside its release profile")):
                build_candidate(extra_package, game, root / "work-extra")
            self.assertFalse((root / "work-extra").exists())

    def test_dynamic_profile_builds_without_compiled_version_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, expected, profile_id = self._dynamic_setup(root)
            calls = []

            def repack(_input, _source, output, modules):
                calls.append(tuple(modules))
                output.write_bytes(expected)

            with patch(
                    "installer.candidate_builder.release_profile",
                    side_effect=AssertionError("dynamic profile used registry")):
                built = build_candidate(
                    package, game, root / "work-dynamic", repack=repack)
            self.assertEqual(
                ("Client", *TARGET_VITALS_ALPHA_POLICY.frozen_module_names),
                calls[0])
            audit = json.loads(built.audit_path.read_text(encoding="utf-8"))
            self.assertEqual(profile_id, audit["release_profile"])

    def test_dynamic_loader_profile_freezes_only_loader_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, expected, _profile_id = self._dynamic_setup(
                root, policy=MOD_LOADER_POLICY)
            selected = []

            def repack(_input, _source, output, modules):
                selected.extend(modules)
                output.write_bytes(expected)

            with patch(
                    "installer.candidate_builder._registered_modules",
                    return_value={
                        "Client": Path("Client.py"),
                        "render_mixin": Path("render_mixin.py"),
                        **{name: Path(name) for name in
                           MOD_LOADER_POLICY.frozen_module_names},
                    }):
                built = build_candidate(
                    package, game, root / "work", repack=repack)

            self.assertEqual(
                ("Client", "render_mixin",
                 *MOD_LOADER_POLICY.frozen_module_names),
                tuple(selected))
            self.assertEqual(expected, built.path.read_bytes())

    def test_forged_dynamic_package_object_is_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _profile_id = self._dynamic_setup(root)
            missing = VerifiedModPackage(
                package.path, package.manifest, package.manifest_sha256,
                package.payloads)
            with self.assertRaisesRegex(
                    CandidateBuildError, "authenticated release binding"):
                build_candidate(missing, game, root / "work-missing")
            self.assertFalse((root / "work-missing").exists())

            mismatched = VerifiedModPackage(
                package.path, package.manifest, package.manifest_sha256,
                package.payloads,
                replace(package.release_binding,
                        expected_client_sha256="0" * 64),
                package.release_profile)
            with self.assertRaisesRegex(
                    CandidateBuildError, "binding and profile disagree"):
                build_candidate(mismatched, game, root / "work-mismatch")
            self.assertFalse((root / "work-mismatch").exists())

    def test_compatibility_test_rebases_authenticated_fragments_in_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _profile_id = self._dynamic_setup(root)
            source = self._compatible_unknown_source()
            (game / "Client.exe").write_bytes(b"new compatible official exe")
            (game / "version.txt").write_text('"0.4.46"\n', encoding="utf-8")
            (game / "_internal" / "Client.py").write_bytes(source)
            repacked = b"locally verified compatibility candidate"
            calls = []

            def repack(input_exe, staged_source, output, modules):
                calls.append((input_exe, staged_source.read_bytes(), tuple(modules)))
                output.write_bytes(repacked)

            with patch(
                    "tools.build_version_binding.verify_clean_client_archive") as verify:
                built = build_candidate(
                    package, game, root / "work-compatibility",
                    compatibility_test=True, repack=repack)

            verify.assert_called_once_with(game / "Client.exe", source)
            self.assertTrue(built.compatibility_test)
            self.assertEqual("0.4.46", built.observed_version)
            self.assertEqual(_sha(b"new compatible official exe"),
                             built.official_sha256)
            self.assertEqual(_sha(repacked), built.sha256)
            self.assertEqual(built.path, built.verify_for(package.compatibility))
            self.assertEqual(
                ("Client", *TARGET_VITALS_ALPHA_POLICY.frozen_module_names),
                calls[0][2])
            self.assertNotEqual(source, calls[0][1])
            audit = json.loads(built.audit_path.read_text(encoding="utf-8"))
            self.assertTrue(audit["compatibility_test"])
            self.assertEqual("0.4.46", audit["observed_game_version"])
            self.assertEqual(4, len(audit["local_fragment_offsets"]))
            self.assertNotEqual(
                audit["recipe_sha256"], audit["signed_recipe_sha256"])
            self.assertEqual(b"new compatible official exe",
                             (game / "Client.exe").read_bytes())

    def test_full_ui_compatibility_test_uses_all_five_policy_fragments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _profile_id = self._dynamic_setup(
                root, FULL_UI_POLICY)
            source = self._compatible_unknown_source().replace(
                b'            self._profiler.mark("turret_layout")\n',
                b'            frame = RenderFrame()\n'
                b'            self._draw_world(frame)\n'
                b'            self._profiler.mark("turret_layout")\n')
            (game / "Client.exe").write_bytes(
                b"new compatible full UI official exe")
            (game / "version.txt").write_text(
                '"0.4.48"\n', encoding="utf-8")
            (game / "_internal" / "Client.py").write_bytes(source)
            repacked = b"locally verified full UI compatibility candidate"
            calls = []

            def repack(_input_exe, staged_source, output, modules):
                calls.append((staged_source.read_bytes(), tuple(modules)))
                output.write_bytes(repacked)

            with patch(
                    "tools.build_version_binding.verify_clean_client_archive") as verify:
                built = build_candidate(
                    package, game, root / "work-full-compatibility",
                    compatibility_test=True, repack=repack)

            verify.assert_called_once_with(game / "Client.exe", source)
            self.assertTrue(built.compatibility_test)
            self.assertEqual("0.4.48", built.observed_version)
            self.assertEqual(
                ("Client", *FULL_UI_POLICY.frozen_module_names),
                calls[0][1])
            self.assertNotEqual(source, calls[0][0])
            audit = json.loads(built.audit_path.read_text(encoding="utf-8"))
            self.assertEqual(5, len(audit["local_fragment_offsets"]))
            self.assertEqual(
                set(FULL_UI_POLICY.fragment_ids),
                set(audit["local_fragment_offsets"]))
            self.assertEqual(b"new compatible full UI official exe",
                             (game / "Client.exe").read_bytes())

    def test_compatibility_test_fails_closed_for_unusable_or_dirty_host(self):
        from tools.build_version_binding import VersionBindingError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, package, _expected, _profile_id = self._dynamic_setup(root)
            (game / "Client.exe").write_bytes(b"new compatible official exe")
            (game / "version.txt").write_text('"0.4.46"\n', encoding="utf-8")
            missing_anchor = self._compatible_unknown_source().replace(
                b'self._profiler.mark("turret_layout")',
                b'self._profiler.mark("changed")')
            (game / "_internal" / "Client.py").write_bytes(missing_anchor)
            with (patch("tools.build_version_binding.verify_clean_client_archive"),
                  self.assertRaisesRegex(
                      CandidateBuildError, "missing or ambiguous")):
                build_candidate(
                    package, game, root / "work-missing-anchor",
                    compatibility_test=True,
                    repack=lambda *_args: self.fail("repack must not run"))
            self.assertFalse((root / "work-missing-anchor").exists())

            (game / "_internal" / "Client.py").write_bytes(
                self._compatible_unknown_source())
            with (patch(
                    "tools.build_version_binding.verify_clean_client_archive",
                    side_effect=VersionBindingError("archive is already modded")),
                  self.assertRaisesRegex(CandidateBuildError, "already modded")):
                build_candidate(
                    package, game, root / "work-dirty-archive",
                    compatibility_test=True,
                    repack=lambda *_args: self.fail("repack must not run"))
            self.assertFalse((root / "work-dirty-archive").exists())


if __name__ == "__main__":
    unittest.main()
