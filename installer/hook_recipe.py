"""Declarative, game-code-free host integration recipes.

A recipe identifies exact locally owned source bytes by SHA-256, then applies
only immutable catalog fragments or hash-addressed deletions.  Recipes carry no
insertion text, original game source, bytecode, executable patcher, or diff.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .authored_fragments import (AuthoredFragment,
                                 resolve_active_authored_fragment)


RECIPE_SCHEMA = 2
RECIPE_FORMAT = "star-empire-ui-hook-recipe"
MAX_RECIPE_BYTES = 2 * 1024 * 1024
MAX_INSERT_BYTES = 256 * 1024
HOST_MODULE_PATHS: Mapping[str, str] = {
    "Client": "Client.py",
    "render_mixin": "render_mixin.py",
    "hangar_inventory": "hangar_inventory.py",
    "gl_renderer": "gl_renderer.py",
    "item_search": "item_search.py",
}


class HookRecipeError(ValueError):
    """Raised when a recipe is malformed or does not match local source."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise HookRecipeError(f"{label} must be a SHA-256 value")
    digest = value.upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise HookRecipeError(f"{label} must be a SHA-256 value")
    return digest


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise HookRecipeError(f"{label} must be an integer")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HookRecipeError(f"duplicate recipe key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class InsertFragmentOperation:
    offset: int
    fragment: AuthoredFragment

    @property
    def fragment_id(self) -> str:
        return self.fragment.fragment_id

    @property
    def fragment_sha256(self) -> str:
        return self.fragment.sha256


@dataclass(frozen=True)
class DeleteOperation:
    start: int
    end: int
    preimage_sha256: str


HookOperation = InsertFragmentOperation | DeleteOperation


@dataclass(frozen=True)
class ModuleRecipe:
    module: str
    path: str
    input_sha256: str
    output_sha256: str
    operations: tuple[HookOperation, ...]

    def apply(self, source: bytes) -> bytes:
        """Apply verified non-overlapping splices against exact source bytes."""
        if _sha(source) != self.input_sha256:
            raise HookRecipeError(
                f"local {self.path} does not match this recipe's official baseline")
        deletes = [operation for operation in self.operations
                   if isinstance(operation, DeleteOperation)]
        previous_end = -1
        for operation in sorted(deletes, key=lambda item: item.start):
            if operation.start < previous_end:
                raise HookRecipeError(f"overlapping deletes in {self.path}")
            if not (0 <= operation.start < operation.end <= len(source)):
                raise HookRecipeError(f"delete is outside {self.path}")
            if _sha(source[operation.start:operation.end]) != operation.preimage_sha256:
                raise HookRecipeError(f"delete preimage changed in {self.path}")
            previous_end = operation.end
        insert_offsets: set[int] = set()
        for operation in self.operations:
            if isinstance(operation, InsertFragmentOperation):
                if not 0 <= operation.offset <= len(source):
                    raise HookRecipeError(f"insert is outside {self.path}")
                if operation.offset in insert_offsets:
                    raise HookRecipeError(f"duplicate insert offset in {self.path}")
                if any(item.start < operation.offset < item.end for item in deletes):
                    raise HookRecipeError(f"insert is inside a deleted range in {self.path}")
                insert_offsets.add(operation.offset)

        output = bytearray(source)
        # At a shared offset the delete is performed first, then the catalog
        # fragment is inserted at the same original boundary.
        ordered = sorted(
            self.operations,
            key=lambda item: (
                item.offset if isinstance(item, InsertFragmentOperation) else item.start,
                0 if isinstance(item, InsertFragmentOperation) else 1),
            reverse=True,
        )
        for operation in ordered:
            if isinstance(operation, InsertFragmentOperation):
                output[operation.offset:operation.offset] = operation.fragment.payload
            else:
                del output[operation.start:operation.end]
        result = bytes(output)
        if _sha(result) != self.output_sha256:
            raise HookRecipeError(f"rebuilt {self.path} failed its expected SHA-256")
        return result


@dataclass(frozen=True)
class HookRecipe:
    game_version: str
    modules: tuple[ModuleRecipe, ...]

    def apply_to(self, source_root: Path, destination_root: Path) -> tuple[Path, ...]:
        """Create host sources in a new staging directory without mutating input."""
        source_root = Path(source_root).resolve(strict=True)
        destination_root = Path(destination_root).resolve()
        if destination_root.exists():
            raise HookRecipeError(
                f"refusing to overwrite recipe destination: {destination_root}")
        destination_root.mkdir(parents=True)
        written: list[Path] = []
        try:
            for module in self.modules:
                source_path = source_root / module.path
                if not source_path.is_file():
                    raise HookRecipeError(f"local host source is missing: {module.path}")
                output_path = destination_root / module.path
                output_path.write_bytes(module.apply(source_path.read_bytes()))
                written.append(output_path)
        except Exception:
            import shutil
            shutil.rmtree(destination_root, ignore_errors=True)
            raise
        return tuple(written)


def parse_hook_recipe(payload: bytes) -> HookRecipe:
    """Validate exact schema-2 JSON and resolve its inert catalog references."""
    if not payload or len(payload) > MAX_RECIPE_BYTES:
        raise HookRecipeError("hook recipe size is outside limits")
    try:
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HookRecipeError("hook recipe is not unique-key UTF-8 JSON") from error
    if not isinstance(data, dict) or set(data) != {
            "schema", "format", "game_version", "modules"}:
        raise HookRecipeError("hook recipe has unknown or missing top-level fields")
    if (type(data["schema"]) is not int or data["schema"] != RECIPE_SCHEMA
            or type(data["format"]) is not str
            or data["format"] != RECIPE_FORMAT):
        raise HookRecipeError("unsupported hook recipe format")
    raw_game_version = data["game_version"]
    raw_modules = data["modules"]
    if (type(raw_game_version) is not str or not raw_game_version.strip()
            or not isinstance(raw_modules, list) or not raw_modules):
        raise HookRecipeError("hook recipe must name a game version and modules")
    game_version = raw_game_version.strip()
    modules: list[ModuleRecipe] = []
    seen_modules: set[str] = set()
    seen_fragment_ids: set[str] = set()
    for raw_module in raw_modules:
        if not isinstance(raw_module, dict) or set(raw_module) != {
                "module", "path", "input_sha256", "output_sha256", "operations"}:
            raise HookRecipeError("module recipe has unknown or missing fields")
        module_name = raw_module["module"]
        path = raw_module["path"]
        if type(module_name) is not str or type(path) is not str:
            raise HookRecipeError("host module and path must be strings")
        if module_name in seen_modules or HOST_MODULE_PATHS.get(module_name) != path:
            raise HookRecipeError(f"unknown, duplicate, or mismatched host module: {module_name}")
        seen_modules.add(module_name)
        raw_operations = raw_module["operations"]
        if not isinstance(raw_operations, list) or not raw_operations:
            raise HookRecipeError(f"{module_name} must declare at least one operation")
        operations: list[HookOperation] = []
        inserted_bytes = 0
        for raw_operation in raw_operations:
            if not isinstance(raw_operation, dict):
                raise HookRecipeError("hook operation must be an object")
            kind = raw_operation.get("kind")
            if kind == "insert_fragment" and set(raw_operation) == {
                    "kind", "offset", "fragment_id", "fragment_sha256"}:
                offset = _integer(raw_operation["offset"], "insert offset")
                fragment_id = raw_operation["fragment_id"]
                if type(fragment_id) is not str or not fragment_id:
                    raise HookRecipeError("authored fragment ID must be a string")
                try:
                    fragment = resolve_active_authored_fragment(fragment_id)
                except KeyError as error:
                    raise HookRecipeError(str(error)) from error
                if fragment.module != module_name:
                    raise HookRecipeError(
                        f"authored fragment does not target {module_name}: {fragment_id}")
                if fragment_id in seen_fragment_ids:
                    raise HookRecipeError(
                        f"duplicate authored fragment: {fragment_id}")
                seen_fragment_ids.add(fragment_id)
                declared_fragment_sha256 = _digest(
                    raw_operation["fragment_sha256"], "authored fragment")
                if declared_fragment_sha256 != fragment.sha256:
                    raise HookRecipeError(
                        f"authored fragment hash mismatch: {fragment_id}")
                inserted_bytes += fragment.size
                if inserted_bytes > MAX_INSERT_BYTES:
                    raise HookRecipeError(
                        f"authored insert budget exceeded for {module_name}")
                operations.append(InsertFragmentOperation(offset, fragment))
            elif kind == "delete" and set(raw_operation) == {
                    "kind", "start", "end", "preimage_sha256"}:
                start = _integer(raw_operation["start"], "delete start")
                end = _integer(raw_operation["end"], "delete end")
                operations.append(DeleteOperation(
                    start, end, _digest(
                        raw_operation["preimage_sha256"], "delete preimage")))
            else:
                raise HookRecipeError(
                    "only strict insert_fragment and delete operations are allowed")
        modules.append(ModuleRecipe(
            module_name, path,
            _digest(raw_module["input_sha256"], "module input"),
            _digest(raw_module["output_sha256"], "module output"),
            tuple(operations),
        ))
    return HookRecipe(game_version, tuple(modules))
