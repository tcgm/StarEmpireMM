"""Build a provenance-bound public hook recipe from an explicit placement plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from installer.authored_fragments import resolve_active_authored_fragment
from installer.hook_recipe import (HOST_MODULE_PATHS, HookRecipeError,
                                   parse_hook_recipe)


PLAN_SCHEMA = 1
PLAN_FORMAT = "star-empire-ui-hook-placement-plan"
MAX_PLAN_BYTES = 512 * 1024


class HookRecipeBuildError(RuntimeError):
    """Raised when a placement plan cannot reproduce its reviewed target."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HookRecipeBuildError(f"duplicate placement-plan key: {key}")
        result[key] = value
    return result


def _digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise HookRecipeBuildError(f"{label} must be a SHA-256 value")
    digest = value.upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise HookRecipeBuildError(f"{label} must be a SHA-256 value")
    return digest


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise HookRecipeBuildError(f"{label} must be an integer")
    return value


def parse_placement_plan(payload: bytes) -> dict[str, Any]:
    """Validate a data-only plan; it may contain no source or insertion text."""
    if not payload or len(payload) > MAX_PLAN_BYTES:
        raise HookRecipeBuildError("placement plan size is outside limits")
    try:
        plan = json.loads(payload.decode("utf-8"),
                          object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HookRecipeBuildError(
            "placement plan is not unique-key UTF-8 JSON") from error
    if not isinstance(plan, dict) or set(plan) != {
            "schema", "format", "game_version", "modules"}:
        raise HookRecipeBuildError(
            "placement plan has unknown or missing top-level fields")
    if (type(plan["schema"]) is not int or plan["schema"] != PLAN_SCHEMA
            or type(plan["format"]) is not str
            or plan["format"] != PLAN_FORMAT):
        raise HookRecipeBuildError("unsupported placement-plan format")
    if (type(plan["game_version"]) is not str
            or not plan["game_version"].strip()
            or not isinstance(plan["modules"], list)
            or not plan["modules"]):
        raise HookRecipeBuildError(
            "placement plan must name a game version and modules")

    seen: set[str] = set()
    for module in plan["modules"]:
        if not isinstance(module, dict) or set(module) != {
                "module", "path", "input_sha256", "operations"}:
            raise HookRecipeBuildError(
                "placement module has unknown or missing fields")
        name = module["module"]
        path = module["path"]
        if type(name) is not str or type(path) is not str:
            raise HookRecipeBuildError("placement module and path must be strings")
        if name in seen or HOST_MODULE_PATHS.get(name) != path:
            raise HookRecipeBuildError(
                f"unknown, duplicate, or mismatched host module: {name}")
        seen.add(name)
        module["input_sha256"] = _digest(
            module["input_sha256"], "official module input")
        operations = module["operations"]
        if not isinstance(operations, list) or not operations:
            raise HookRecipeBuildError(
                f"placement module must have operations: {name}")
        for operation in operations:
            if not isinstance(operation, dict):
                raise HookRecipeBuildError("placement operation must be an object")
            kind = operation.get("kind")
            if kind == "insert_fragment" and set(operation) == {
                    "kind", "offset", "fragment_id"}:
                operation["offset"] = _integer(
                    operation["offset"], "fragment offset")
                fragment_id = operation["fragment_id"]
                if type(fragment_id) is not str or not fragment_id:
                    raise HookRecipeBuildError("fragment ID must be a string")
                try:
                    fragment = resolve_active_authored_fragment(fragment_id)
                except KeyError as error:
                    raise HookRecipeBuildError(str(error)) from error
                if fragment.module != name:
                    raise HookRecipeBuildError(
                        f"fragment does not target {name}: {fragment_id}")
            elif kind == "delete" and set(operation) == {
                    "kind", "start", "end"}:
                operation["start"] = _integer(
                    operation["start"], "delete start")
                operation["end"] = _integer(operation["end"], "delete end")
            else:
                raise HookRecipeBuildError(
                    "only data-only insert_fragment and delete placements are allowed")
    return plan


def build_hook_recipe(*, baseline_root: Path, target_root: Path,
                      placement_plan: Path, output: Path) -> Path:
    """Emit a canonical recipe only when it exactly reproduces tested targets."""
    baseline_root = Path(baseline_root).resolve(strict=True)
    target_root = Path(target_root).resolve(strict=True)
    placement_plan = Path(placement_plan).resolve(strict=True)
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".hook":
        raise HookRecipeBuildError("recipe output must use the .hook extension")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite recipe: {output}")
    try:
        plan = parse_placement_plan(placement_plan.read_bytes())
    except OSError as error:
        raise HookRecipeBuildError("placement plan could not be read") from error

    modules: list[dict[str, Any]] = []
    for planned in plan["modules"]:
        relative = Path(planned["path"])
        baseline_path = baseline_root / relative
        target_path = target_root / relative
        if not baseline_path.is_file() or not target_path.is_file():
            raise HookRecipeBuildError(
                f"baseline or tested target is missing: {planned['path']}")
        baseline = baseline_path.read_bytes()
        target = target_path.read_bytes()
        if _sha(baseline) != planned["input_sha256"]:
            raise HookRecipeBuildError(
                f"official baseline hash changed: {planned['path']}")
        operations: list[dict[str, Any]] = []
        for placement in planned["operations"]:
            if placement["kind"] == "insert_fragment":
                fragment = resolve_active_authored_fragment(
                    placement["fragment_id"])
                operations.append({
                    "kind": "insert_fragment",
                    "offset": placement["offset"],
                    "fragment_id": fragment.fragment_id,
                    "fragment_sha256": fragment.sha256,
                })
            else:
                start, end = placement["start"], placement["end"]
                if not 0 <= start < end <= len(baseline):
                    raise HookRecipeBuildError(
                        f"delete is outside official source: {planned['path']}")
                operations.append({
                    "kind": "delete", "start": start, "end": end,
                    "preimage_sha256": _sha(baseline[start:end]),
                })
        modules.append({
            "module": planned["module"],
            "path": planned["path"],
            "input_sha256": _sha(baseline),
            "output_sha256": _sha(target),
            "operations": operations,
        })

    recipe_bytes = (json.dumps({
        "schema": 2,
        "format": "star-empire-ui-hook-recipe",
        "game_version": plan["game_version"].strip(),
        "modules": modules,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        recipe = parse_hook_recipe(recipe_bytes)
        for module in recipe.modules:
            baseline = (baseline_root / module.path).read_bytes()
            target = (target_root / module.path).read_bytes()
            if module.apply(baseline) != target:
                raise HookRecipeBuildError(
                    f"placements do not reproduce tested target: {module.path}")
    except HookRecipeError as error:
        raise HookRecipeBuildError(
            "generated recipe failed strict verification") from error

    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(output.name + ".partial")
    if pending.exists():
        raise FileExistsError(f"unfinished recipe already exists: {pending}")
    try:
        pending.write_bytes(recipe_bytes)
        pending.replace(output)
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    return output


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--placement-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _arguments()
    print(build_hook_recipe(
        baseline_root=arguments.baseline_root,
        target_root=arguments.target_root,
        placement_plan=arguments.placement_plan,
        output=arguments.output,
    ))
