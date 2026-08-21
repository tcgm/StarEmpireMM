"""Tests for provenance-bound recipe generation without game source output."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from installer.authored_fragments import (
    RENDER_FRAMEWORK_CORE_INSTALL_V5,
    RENDER_SHARED_CONTEXT_MENU_INSTALL_V1,
    resolve_authored_fragment,
)
from installer.hook_recipe import parse_hook_recipe
from tools.build_hook_recipe import HookRecipeBuildError, build_hook_recipe


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


class HookRecipeBuilderTests(unittest.TestCase):
    def _fixture(self, root: Path):
        baseline = root / "baseline"
        target = root / "target"
        baseline.mkdir()
        target.mkdir()
        source = b"class RenderMixin:\n    old = True\n"
        removed = b"    old = True\n"
        fragment = resolve_authored_fragment(
            RENDER_FRAMEWORK_CORE_INSTALL_V5)
        rebuilt = source[:len(source) - len(removed)] + fragment.payload
        (baseline / "render_mixin.py").write_bytes(source)
        (target / "render_mixin.py").write_bytes(rebuilt)
        plan = root / "placement.json"
        plan.write_text(json.dumps({
            "schema": 1,
            "format": "star-empire-ui-hook-placement-plan",
            "game_version": "0.4.42",
            "modules": [{
                "module": "render_mixin", "path": "render_mixin.py",
                "input_sha256": _sha(source),
                "operations": [
                    {"kind": "delete",
                     "start": len(source) - len(removed), "end": len(source)},
                    {"kind": "insert_fragment",
                     "offset": len(source) - len(removed),
                     "fragment_id": fragment.fragment_id},
                ],
            }],
        }), encoding="utf-8")
        return baseline, target, plan, source, removed, rebuilt, fragment

    def test_output_is_deterministic_source_free_and_replays_exact_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, source, removed, rebuilt, fragment = (
                self._fixture(root))
            first = build_hook_recipe(
                baseline_root=baseline, target_root=target,
                placement_plan=plan, output=root / "one.hook")
            second = build_hook_recipe(
                baseline_root=baseline, target_root=target,
                placement_plan=plan, output=root / "two.hook")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            payload = first.read_bytes()
            self.assertNotIn(source, payload)
            self.assertNotIn(removed, payload)
            self.assertNotIn(fragment.payload, payload)
            self.assertNotIn(b'"text"', payload)
            recipe = parse_hook_recipe(payload)
            self.assertEqual(rebuilt, recipe.modules[0].apply(source))

    def test_wrong_baseline_and_unmatched_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, *_ = self._fixture(root)
            (baseline / "render_mixin.py").write_bytes(b"changed")
            with self.assertRaisesRegex(HookRecipeBuildError, "baseline hash"):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=plan, output=root / "wrong.hook")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, *_ = self._fixture(root)
            with (target / "render_mixin.py").open("ab") as stream:
                stream.write(b"unmatched")
            with self.assertRaisesRegex(
                    HookRecipeBuildError, "strict verification"):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=plan, output=root / "unmatched.hook")

    def test_patch_input_inline_text_and_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, *_ = self._fixture(root)
            patch = root / "private.patch"
            patch.write_text("--- a/render_mixin.py\n+++ b/render_mixin.py\n",
                             encoding="utf-8")
            with self.assertRaisesRegex(HookRecipeBuildError, "placement plan"):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=patch, output=root / "patch.hook")
            contaminated = json.loads(plan.read_text(encoding="utf-8"))
            contaminated["modules"][0]["operations"][1]["text"] = "code"
            plan.write_text(json.dumps(contaminated), encoding="utf-8")
            with self.assertRaisesRegex(HookRecipeBuildError, "data-only"):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=plan, output=root / "inline.hook")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, *_ = self._fixture(root)
            output = root / "release.hook"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=plan, output=output)
            self.assertEqual("keep", output.read_text(encoding="utf-8"))

    def test_inactive_historical_fragment_is_rejected_by_the_plan_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, target, plan, *_ = self._fixture(root)
            historical = json.loads(plan.read_text(encoding="utf-8"))
            historical["modules"][0]["operations"][1]["fragment_id"] = (
                RENDER_SHARED_CONTEXT_MENU_INSTALL_V1)
            plan.write_text(json.dumps(historical), encoding="utf-8")

            with self.assertRaisesRegex(HookRecipeBuildError, "not active"):
                build_hook_recipe(
                    baseline_root=baseline, target_root=target,
                    placement_plan=plan, output=root / "inactive.hook")


if __name__ == "__main__":
    unittest.main()
