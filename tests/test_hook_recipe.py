from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from installer.authored_fragments import (
    ACTIVE_AUTHORED_FRAGMENTS,
    AUTHORED_FRAGMENTS,
    AuthoredFragment,
    CLIENT_COALITION_GEOMETRY_INSTALL_V1,
    CLIENT_FRAMEWORK_CORE_INSTALL_V2,
    CLIENT_WORKSPACE_INPUT_INSTALL_V1,
    CLIENT_WORKSPACE_OWNERSHIP_INSTALL_V1,
    RENDER_COALITION_FRAMEWORK_INSTALL_V1,
    RENDER_FRAMEWORK_CORE_INSTALL_V3,
    RENDER_FRAMEWORK_CORE_INSTALL_V4,
    RENDER_FRAMEWORK_CORE_INSTALL_V5,
    RENDER_SHARED_CONTEXT_MENU_INSTALL_V1,
    resolve_authored_fragment,
)
from installer.hook_recipe import (
    HookRecipeError,
    MAX_INSERT_BYTES,
    parse_hook_recipe,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


FRAGMENT = resolve_authored_fragment(RENDER_FRAMEWORK_CORE_INSTALL_V5)


class HookRecipeTests(unittest.TestCase):
    def _payload(self, source: bytes = b"class RenderMixin:\n    pass\n",
                 operations=None, output: bytes | None = None,
                 *, module: str = "render_mixin",
                 path: str = "render_mixin.py") -> bytes:
        if operations is None:
            operations = [{
                "kind": "insert_fragment",
                "offset": len(source),
                "fragment_id": FRAGMENT.fragment_id,
                "fragment_sha256": FRAGMENT.sha256,
            }]
        if output is None:
            output = source + FRAGMENT.payload
        return json.dumps({
            "schema": 2,
            "format": "star-empire-ui-hook-recipe",
            "game_version": "test",
            "modules": [{
                "module": module,
                "path": path,
                "input_sha256": _sha(source),
                "output_sha256": _sha(output),
                "operations": operations,
            }],
        }, separators=(",", ":")).encode("utf-8")

    def test_fragment_catalog_is_immutable_module_bound_and_hash_pinned(self) -> None:
        self.assertEqual("render_mixin", FRAGMENT.module)
        self.assertEqual(1137, FRAGMENT.size)
        self.assertEqual(
            "8F1C7910A103F5EDFACB67787CA28063BF4D5262CA380B509550A998F0DF76B4",
            FRAGMENT.sha256,
        )
        self.assertIn("CoalitionRendererBridge", FRAGMENT.text)
        self.assertIn("install_bridge_methods(", FRAGMENT.text)
        self.assertIn("    RenderMixin,", FRAGMENT.text)
        self.assertIn("    globals(),", FRAGMENT.text)
        with self.assertRaises(TypeError):
            AUTHORED_FRAGMENTS["new"] = FRAGMENT  # type: ignore[index]
        with self.assertRaises(TypeError):
            ACTIVE_AUTHORED_FRAGMENTS["new"] = FRAGMENT  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            FRAGMENT.text = "changed"  # type: ignore[misc]

    def test_atomic_client_fragment_is_active_and_hash_pinned(self) -> None:
        fragment = resolve_authored_fragment(CLIENT_FRAMEWORK_CORE_INSTALL_V2)
        self.assertIs(fragment, ACTIVE_AUTHORED_FRAGMENTS[fragment.fragment_id])
        self.assertEqual("Client", fragment.module)
        self.assertEqual(680, fragment.size)
        self.assertEqual(
            "34732CBB4F0DFDD3A62BFAA46A2827E3BBCCEEF247E6522266D8CF61C85C17BB",
            fragment.sha256,
        )
        self.assertIn("install_bridge_groups(", fragment.text)
        self.assertIn("WorkspaceHostBridge", fragment.text)
        self.assertIn("CoalitionClientBridge", fragment.text)

    def test_exact_local_source_is_transformed_without_being_mutated(self) -> None:
        source = b"class RenderMixin:\n    pass\n"
        payload = self._payload(source)
        self.assertNotIn(b'"text"', payload)
        self.assertNotIn(FRAGMENT.payload, payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local"
            local.mkdir()
            (local / "render_mixin.py").write_bytes(source)
            recipe = parse_hook_recipe(payload)
            written = recipe.apply_to(local, root / "stage")
            self.assertEqual(source, (local / "render_mixin.py").read_bytes())
            self.assertEqual(source + FRAGMENT.payload, written[0].read_bytes())
            operation = recipe.modules[0].operations[0]
            self.assertEqual(FRAGMENT.fragment_id, operation.fragment_id)
            self.assertEqual(FRAGMENT.sha256, operation.fragment_sha256)

    def test_changed_baseline_or_wrong_output_hash_is_rejected(self) -> None:
        recipe = parse_hook_recipe(self._payload())
        with self.assertRaisesRegex(HookRecipeError, "official baseline"):
            recipe.modules[0].apply(b"changed")
        broken = json.loads(self._payload())
        broken["modules"][0]["output_sha256"] = "F" * 64
        with self.assertRaisesRegex(HookRecipeError, "expected SHA"):
            parse_hook_recipe(json.dumps(broken).encode()).modules[0].apply(
                b"class RenderMixin:\n    pass\n")

    def test_hash_addressed_delete_and_same_boundary_fragment_are_safe(self) -> None:
        source = b"before OLD "
        removed = b"OLD "
        output = b"before " + FRAGMENT.payload
        operations = [
            {"kind": "delete", "start": 7, "end": 11,
             "preimage_sha256": _sha(removed)},
            {"kind": "insert_fragment", "offset": 7,
             "fragment_id": FRAGMENT.fragment_id,
             "fragment_sha256": FRAGMENT.sha256},
        ]
        recipe = parse_hook_recipe(self._payload(source, operations, output))
        self.assertEqual(output, recipe.modules[0].apply(source))

    def test_recipe_rejects_paths_unknown_fields_inline_text_and_overlaps(self) -> None:
        cases = []
        path = json.loads(self._payload())
        path["modules"][0]["path"] = "../render_mixin.py"
        cases.append(path)
        unknown = json.loads(self._payload())
        unknown["surprise"] = True
        cases.append(unknown)
        inline = json.loads(self._payload())
        inline["modules"][0]["operations"] = [
            {"kind": "insert", "offset": 0, "text": "game code"}]
        cases.append(inline)
        smuggled = json.loads(self._payload())
        smuggled["modules"][0]["operations"][0]["text"] = "game code"
        cases.append(smuggled)
        replace = json.loads(self._payload())
        replace["modules"][0]["operations"] = [
            {"kind": "replace", "start": 0, "end": 1, "text": "game code"}]
        cases.append(replace)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(HookRecipeError):
                parse_hook_recipe(json.dumps(case).encode())

        source = b"0123456789"
        operations = [
            {"kind": "delete", "start": 1, "end": 6,
             "preimage_sha256": _sha(source[1:6])},
            {"kind": "delete", "start": 5, "end": 8,
             "preimage_sha256": _sha(source[5:8])},
        ]
        recipe = parse_hook_recipe(self._payload(source, operations, b"0"))
        with self.assertRaisesRegex(HookRecipeError, "overlapping"):
            recipe.modules[0].apply(source)

    def test_exact_json_types_are_required(self) -> None:
        cases = []
        schema = json.loads(self._payload())
        schema["schema"] = True
        cases.append(schema)
        version = json.loads(self._payload())
        version["game_version"] = 2
        cases.append(version)
        module = json.loads(self._payload())
        module["modules"][0]["module"] = 1
        cases.append(module)
        digest = json.loads(self._payload())
        digest["modules"][0]["input_sha256"] = 1
        cases.append(digest)
        fragment_id = json.loads(self._payload())
        fragment_id["modules"][0]["operations"][0]["fragment_id"] = 1
        cases.append(fragment_id)
        fragment_digest = json.loads(self._payload())
        fragment_digest["modules"][0]["operations"][0]["fragment_sha256"] = 1
        cases.append(fragment_digest)
        for value in (True, "0", 0.5):
            offset = json.loads(self._payload())
            offset["modules"][0]["operations"][0]["offset"] = value
            cases.append(offset)
            delete = json.loads(self._payload())
            delete["modules"][0]["operations"] = [{
                "kind": "delete", "start": value, "end": 1,
                "preimage_sha256": "A" * 64,
            }]
            cases.append(delete)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(HookRecipeError):
                parse_hook_recipe(json.dumps(case).encode())

    def test_unknown_wrong_hash_and_wrong_module_fragments_are_rejected(self) -> None:
        unknown = json.loads(self._payload())
        unknown["modules"][0]["operations"][0]["fragment_id"] = "unknown.v1"
        wrong_hash = json.loads(self._payload())
        wrong_hash["modules"][0]["operations"][0]["fragment_sha256"] = "F" * 64
        wrong_module = json.loads(self._payload())
        wrong_module["modules"][0]["module"] = "Client"
        wrong_module["modules"][0]["path"] = "Client.py"
        for case in (unknown, wrong_hash, wrong_module):
            with self.subTest(case=case), self.assertRaises(HookRecipeError):
                parse_hook_recipe(json.dumps(case).encode())

    def test_historical_or_incomplete_fragments_are_not_recipe_selectable(self) -> None:
        for fragment_id in (
                RENDER_SHARED_CONTEXT_MENU_INSTALL_V1,
                RENDER_COALITION_FRAMEWORK_INSTALL_V1,
                RENDER_FRAMEWORK_CORE_INSTALL_V3,
                RENDER_FRAMEWORK_CORE_INSTALL_V4,
                CLIENT_WORKSPACE_INPUT_INSTALL_V1,
                CLIENT_COALITION_GEOMETRY_INSTALL_V1,
                CLIENT_WORKSPACE_OWNERSHIP_INSTALL_V1):
            with self.subTest(fragment_id=fragment_id):
                historical = resolve_authored_fragment(fragment_id)
                payload = json.loads(self._payload())
                operation = payload["modules"][0]["operations"][0]
                operation["fragment_id"] = historical.fragment_id
                operation["fragment_sha256"] = historical.sha256
                with self.assertRaisesRegex(HookRecipeError, "not active"):
                    parse_hook_recipe(json.dumps(payload).encode())

    def test_expanded_catalog_bytes_enforce_existing_insert_budget(self) -> None:
        fragments = {
            fragment_id: AuthoredFragment(
                fragment_id, "render_mixin",
                ("x" * (MAX_INSERT_BYTES // 2 + 1)) + "\n",
            )
            for fragment_id in ("test.large-one.v1", "test.large-two.v1")
        }
        operations = [{
            "kind": "insert_fragment", "offset": index,
            "fragment_id": fragment_id,
            "fragment_sha256": fragment.sha256,
        } for index, (fragment_id, fragment) in enumerate(fragments.items())]
        with patch(
                "installer.hook_recipe.resolve_active_authored_fragment",
                side_effect=lambda fragment_id: fragments[fragment_id]):
            with self.assertRaisesRegex(HookRecipeError, "insert budget"):
                parse_hook_recipe(self._payload(operations=operations))

    def test_fragment_id_may_appear_only_once_across_the_recipe(self) -> None:
        operations = [{
            "kind": "insert_fragment", "offset": offset,
            "fragment_id": FRAGMENT.fragment_id,
            "fragment_sha256": FRAGMENT.sha256,
        } for offset in (0, 1)]
        with self.assertRaisesRegex(HookRecipeError, "duplicate authored"):
            parse_hook_recipe(self._payload(operations=operations))


if __name__ == "__main__":
    unittest.main()
