"""Build a reviewed, source-free compatibility binding for a new game build.

This maintainer tool never modifies the game installation. It verifies that the
loose Client source matches both frozen Client copies, locates the reviewed
policy integration boundaries structurally, rebuilds two disposable
candidates, and publishes only a hook recipe plus signed-package metadata.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import marshal
from pathlib import Path
import shutil
import sys
import tempfile
from types import CodeType
from typing import Callable, Iterable, Mapping
import uuid

from PyInstaller.archive.readers import CArchiveReader

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from installer.authored_fragments import (
    CLIENT_FULL_UI_BEGIN_FRAME_V1,
    CLIENT_FULL_UI_BOOTSTRAP_V1,
    CLIENT_FULL_UI_EVENT_V1,
    CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4,
    CLIENT_FULL_UI_OVERLAY_V1,
    resolve_active_authored_fragment,
)
from installer.hook_recipe import HookRecipeError, parse_hook_recipe
from installer.manager_core import (ProcessProbeResult, read_game_version,
                                    running_game_processes, sha256_file)
from installer.release_profiles import (BINDING_FORMAT, BINDING_SCHEMA,
                                        ReleasePolicy, release_policy,
                                        TARGET_VITALS_ALPHA_POLICY_ID)
from tools.repack_client import repack_client


CLIENT_ENTRY = "Client"
PYZ_ENTRY = "PYZ.pyz"
UI_MARKERS = (b"star_empire_ui_mod", b"UI_MOD_", b"install_bridge_bundle")


class VersionBindingError(RuntimeError):
    """Raised when a game build cannot safely become a release binding."""


@dataclass(frozen=True)
class BindingBuild:
    output_dir: Path
    recipe_path: Path
    binding_path: Path
    profile_id: str
    candidate_sha256: str


Repack = Callable[[Path, Path, Path, Iterable[str] | None], None]
BaselineProbe = Callable[[Path, bytes], None]
ProcessProbe = Callable[[], ProcessProbeResult]


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _code_value(value: object) -> object:
    if isinstance(value, CodeType):
        return _code_signature(value)
    if isinstance(value, tuple):
        return tuple(_code_value(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_code_value(item) for item in value)
    return value


def _code_signature(code: CodeType) -> tuple[object, ...]:
    """Return execution-relevant code data while ignoring only co_filename."""
    return (
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_stacksize,
        code.co_flags,
        code.co_code,
        tuple(_code_value(item) for item in code.co_consts),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_name,
        code.co_qualname,
        code.co_firstlineno,
        code.co_linetable,
        code.co_exceptiontable,
    )


def verify_clean_client_archive(client_exe: Path, source: bytes) -> None:
    """Prove loose Client.py matches both frozen Client copies exactly."""
    try:
        loose = compile(source.decode("utf-8"), "Client.py", "exec",
                        optimize=0, dont_inherit=True)
        archive = CArchiveReader(str(client_exe))
        if CLIENT_ENTRY not in archive.toc or PYZ_ENTRY not in archive.toc:
            raise VersionBindingError("Client archive is missing required entries")
        if any(str(name).startswith("star_empire_ui_mod") for name in archive.toc):
            raise VersionBindingError("Client archive already contains UI Mod entries")
        outer = marshal.loads(archive.extract(CLIENT_ENTRY))
        pyz = archive.open_embedded_archive(PYZ_ENTRY)
        if any(str(name).startswith("star_empire_ui_mod") for name in pyz.toc):
            raise VersionBindingError("Client PYZ already contains UI Mod entries")
        embedded = pyz.extract(CLIENT_ENTRY)
    except VersionBindingError:
        raise
    except Exception as error:
        raise VersionBindingError("could not verify the frozen Client archive") from error
    if not isinstance(outer, CodeType) or not isinstance(embedded, CodeType):
        raise VersionBindingError("frozen Client entries are not Python code")
    signature = _code_signature(loose)
    if signature != _code_signature(outer) or signature != _code_signature(embedded):
        raise VersionBindingError(
            "loose Client.py does not match both frozen Client entries")


def verify_clean_module_archive(
        client_exe: Path, module_name: str, module_path: str,
        source: bytes) -> None:
    """Prove one loose non-Client module matches its frozen PYZ entry."""
    if module_name == CLIENT_ENTRY:
        verify_clean_client_archive(client_exe, source)
        return
    try:
        loose = compile(source.decode("utf-8"), module_path, "exec",
                        optimize=0, dont_inherit=True)
        archive = CArchiveReader(str(client_exe))
        if PYZ_ENTRY not in archive.toc:
            raise VersionBindingError("Client archive is missing its PYZ entry")
        pyz = archive.open_embedded_archive(PYZ_ENTRY)
        if module_name not in pyz.toc:
            raise VersionBindingError(
                f"Client PYZ is missing required module {module_name}")
        embedded = pyz.extract(module_name)
    except VersionBindingError:
        raise
    except Exception as error:
        raise VersionBindingError(
            f"could not verify frozen module {module_name}") from error
    if not isinstance(embedded, CodeType):
        raise VersionBindingError(
            f"frozen module {module_name} is not Python code")
    if _code_signature(loose) != _code_signature(embedded):
        raise VersionBindingError(
            f"loose {module_path} does not match frozen {module_name}")


def _line_starts(source: bytes) -> tuple[int, ...]:
    starts = [0]
    cursor = 0
    for line in source.splitlines(keepends=True):
        cursor += len(line)
        starts.append(cursor)
    if starts[-1] != len(source):
        starts.append(len(source))
    return tuple(starts)


def _node_start(node: ast.AST, starts: tuple[int, ...]) -> int:
    if not hasattr(node, "lineno"):
        raise VersionBindingError("Client anchor has no source position")
    # Authored fragments already carry the host indentation they require.
    # Insert at the physical line boundary, not after AST ``col_offset``.
    return starts[node.lineno - 1]


def _after_node(node: ast.AST, starts: tuple[int, ...], length: int) -> int:
    end_line = getattr(node, "end_lineno", None)
    if not isinstance(end_line, int):
        raise VersionBindingError("Client anchor has no end position")
    return starts[end_line] if end_line < len(starts) else length


def _attribute_call(node: ast.AST, name: str) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name)


def _is_screen_display_assignment(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    return (isinstance(target, ast.Name) and target.id == "screen"
            and _attribute_call(node.value, "_apply_display_mode")
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "self")


def _is_turret_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    handlers = tuple(
        item for item in ast.walk(node.test)
        if (_attribute_call(item, "_handle_turret_gui_event")
            and isinstance(item.func.value, ast.Name)
            and item.func.value.id == "self")
    )
    return len(handlers) == 1 and any(
        isinstance(item, ast.Continue) for item in node.body)


def _is_event_quit_test(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    for item in ast.walk(node.test):
        if not isinstance(item, ast.Compare) or len(item.ops) != 1:
            continue
        if not isinstance(item.ops[0], ast.Eq) or len(item.comparators) != 1:
            continue
        left, right = item.left, item.comparators[0]
        if (isinstance(left, ast.Attribute) and left.attr == "type"
                and isinstance(left.value, ast.Name) and left.value.id == "event"
                and isinstance(right, ast.Attribute) and right.attr == "QUIT"
                and isinstance(right.value, ast.Name) and right.value.id == "pygame"):
            return any(
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Attribute)
                and isinstance(statement.targets[0].value, ast.Name)
                and statement.targets[0].value.id == "self"
                and statement.targets[0].attr == "_running"
                and isinstance(statement.value, ast.Constant)
                and statement.value.value is False
                for statement in node.body
            )
    return False


def _statement_lists(root: ast.AST) -> Iterable[list[ast.stmt]]:
    for node in ast.walk(root):
        for _field, value in ast.iter_fields(node):
            if (isinstance(value, list) and value
                    and all(isinstance(item, ast.stmt) for item in value)):
                yield value


def _is_profiler_mark(node: ast.AST, label: str) -> bool:
    if not isinstance(node, ast.Expr) or not _attribute_call(node.value, "mark"):
        return False
    call = node.value
    return (isinstance(call.func.value, ast.Attribute)
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "self"
            and call.func.value.attr == "_profiler"
            and len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == label)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    return (isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _is_render_frame_assignment(node: ast.AST) -> bool:
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    value = node.value
    return (isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "RenderFrame")


def _exactly_one(items: Iterable[object], label: str) -> object:
    found = tuple(items)
    if len(found) != 1:
        raise VersionBindingError(
            f"Client semantic anchor is missing or ambiguous: {label}")
    return found[0]


def _is_managed_turret_layout_queue(node: ast.AST) -> bool:
    """The 0.4.66+ managed-window equivalent of the old profiler boundary."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    if (not isinstance(call.func, ast.Name) or call.func.id != "_queue_modal"
            or len(call.args) != 3 or call.keywords):
        return False
    label, active, draw = call.args
    if (not isinstance(label, ast.Constant) or label.value != "turret_layout"
            or not isinstance(active, ast.Attribute)
            or not isinstance(active.value, ast.Name)
            or active.value.id != "self"
            or active.attr != "_turret_layout_open"
            or not isinstance(draw, ast.Lambda)
            or not isinstance(draw.body, ast.Call)):
        return False
    callback = draw.body.func
    return (isinstance(callback, ast.Attribute)
            and isinstance(callback.value, ast.Name)
            and callback.value.id == "self"
            and callback.attr == "_draw_turret_layout")


def _turret_foreground_boundary(run: ast.AST) -> ast.AST:
    """Find exactly one reviewed legacy or managed turret foreground edge."""
    return _exactly_one(
        (node for node in ast.walk(run)
         if (_is_profiler_mark(node, "turret_layout")
             or _is_managed_turret_layout_queue(node))),
        "turret-layout foreground boundary",
    )


def _policy_fragment_ids(
        policy: ReleasePolicy, module_name: str) -> tuple[str, ...]:
    return tuple(
        fragment_id for fragment_id in policy.fragment_ids
        if resolve_active_authored_fragment(fragment_id).module == module_name
    )


def locate_target_vitals_offsets(
        source: bytes, policy: ReleasePolicy) -> dict[str, int]:
    """Find reviewed insertion boundaries without copying host source text."""
    try:
        tree = ast.parse(source.decode("utf-8"), filename="Client.py")
    except (UnicodeError, SyntaxError) as error:
        raise VersionBindingError("Client.py is not valid UTF-8 Python") from error
    starts = _line_starts(source)
    window_class = _exactly_one(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "SolarSystemWindow"),
        "SolarSystemWindow class",
    )
    run = _exactly_one(
        (node for node in window_class.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == "run"),
        "SolarSystemWindow.run",
    )
    display_assignment = _exactly_one(
        (node for node in run.body if _is_screen_display_assignment(node)),
        "initial display-mode assignment",
    )
    event_boundaries: list[ast.If] = []
    for statements in _statement_lists(run):
        for index in range(len(statements) - 1):
            if (_is_turret_guard(statements[index])
                    and _is_event_quit_test(statements[index + 1])):
                event_boundaries.append(statements[index + 1])
    event_boundary = _exactly_one(event_boundaries, "turret event guard")
    overlay_boundary = _turret_foreground_boundary(run)
    main_guard = _exactly_one(
        (node for node in tree.body if _is_main_guard(node)),
        "top-level entry point",
    )
    raw_offsets = (
        _after_node(display_assignment, starts, len(source)),
        _node_start(event_boundary, starts),
        _node_start(overlay_boundary, starts),
        _node_start(main_guard, starts),
    )
    fragment_ids = _policy_fragment_ids(policy, "Client")
    if len(fragment_ids) != 4 or len(set(raw_offsets)) != 4:
        raise VersionBindingError("Target+Vitals policy or Client anchors are invalid")
    if tuple(sorted(raw_offsets)) != raw_offsets:
        raise VersionBindingError("Client semantic anchors are out of order")
    return dict(zip(fragment_ids, raw_offsets, strict=True))


def locate_release_offsets(
        source: bytes, policy: ReleasePolicy) -> dict[str, int]:
    """Locate the exact structural boundaries for one compiled policy."""
    fragment_ids = _policy_fragment_ids(policy, "Client")
    if len(fragment_ids) == 4:
        return locate_target_vitals_offsets(source, policy)
    if len(fragment_ids) != 5:
        raise VersionBindingError(
            "release policy has no reviewed Client anchor layout")
    try:
        tree = ast.parse(source.decode("utf-8"), filename="Client.py")
    except (UnicodeError, SyntaxError) as error:
        raise VersionBindingError("Client.py is not valid UTF-8 Python") from error
    starts = _line_starts(source)
    window_class = _exactly_one(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "SolarSystemWindow"),
        "SolarSystemWindow class",
    )
    run = _exactly_one(
        (node for node in window_class.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == "run"),
        "SolarSystemWindow.run",
    )
    display_assignment = _exactly_one(
        (node for node in run.body if _is_screen_display_assignment(node)),
        "initial display-mode assignment",
    )
    event_boundaries: list[ast.If] = []
    for statements in _statement_lists(run):
        for index in range(len(statements) - 1):
            if (_is_turret_guard(statements[index])
                    and _is_event_quit_test(statements[index + 1])):
                event_boundaries.append(statements[index + 1])
    event_boundary = _exactly_one(event_boundaries, "turret event guard")
    frame_assignment = _exactly_one(
        (node for node in ast.walk(run)
         if _is_render_frame_assignment(node)),
        "RenderFrame construction",
    )
    overlay_boundary = _turret_foreground_boundary(run)
    main_guard = _exactly_one(
        (node for node in tree.body if _is_main_guard(node)),
        "top-level entry point",
    )
    raw_offsets = (
        _after_node(display_assignment, starts, len(source)),
        _node_start(event_boundary, starts),
        _after_node(frame_assignment, starts, len(source)),
        _node_start(overlay_boundary, starts),
        _node_start(main_guard, starts),
    )
    if len(set(raw_offsets)) != len(raw_offsets):
        raise VersionBindingError("full UI Client anchors are invalid")
    if tuple(sorted(raw_offsets)) != raw_offsets:
        raise VersionBindingError("Client semantic anchors are out of order")
    return dict(zip(fragment_ids, raw_offsets, strict=True))


def locate_render_mixin_offsets(
        source: bytes, policy: ReleasePolicy) -> dict[str, int]:
    """Locate the inline Storage extension boundary structurally."""
    fragment_ids = _policy_fragment_ids(policy, "render_mixin")
    if not fragment_ids:
        return {}
    if len(fragment_ids) != 1:
        raise VersionBindingError(
            "release policy has no reviewed render_mixin anchor layout")
    try:
        tree = ast.parse(source.decode("utf-8"), filename="render_mixin.py")
    except (UnicodeError, SyntaxError) as error:
        raise VersionBindingError(
            "render_mixin.py is not valid UTF-8 Python") from error
    starts = _line_starts(source)
    renderer = _exactly_one(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "RenderMixin"),
        "RenderMixin class",
    )
    station_draw = _exactly_one(
        (node for node in renderer.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == "_draw_station_overlay"),
        "RenderMixin._draw_station_overlay",
    )
    search_advance = _exactly_one(
        (node for node in ast.walk(station_draw)
         if isinstance(node, ast.AugAssign)
         and isinstance(node.target, ast.Name)
         and node.target.id == "_iy"
         and any(isinstance(item, ast.Name)
                 and item.id == "_STOR_SEARCH_H"
                 for item in ast.walk(node.value))),
        "Storage search layout advance",
    )
    return {fragment_ids[0]: _after_node(
        search_advance, starts, len(source))}


def locate_policy_offsets(
        sources: Mapping[str, bytes], policy: ReleasePolicy) -> dict[str, int]:
    """Locate every reviewed host insertion for one compiled policy."""
    host_names = tuple(name for name, _path in policy.hosts)
    if set(sources) != set(host_names):
        raise VersionBindingError("release policy host source inventory changed")
    offsets: dict[str, int] = {}
    for module_name in host_names:
        if module_name == "Client":
            located = locate_release_offsets(sources[module_name], policy)
        elif module_name == "render_mixin":
            located = locate_render_mixin_offsets(
                sources[module_name], policy)
        else:
            raise VersionBindingError(
                f"release policy has no locator for {module_name}")
        offsets.update(located)
    if set(offsets) != set(policy.fragment_ids):
        raise VersionBindingError("release policy fragment anchors are incomplete")
    return offsets


def _apply_module_fragments(
        source: bytes, policy: ReleasePolicy, offsets: dict[str, int],
        module_name: str) -> bytes:
    output = bytearray(source)
    placements = []
    for fragment_id in policy.fragment_ids:
        fragment = resolve_active_authored_fragment(fragment_id)
        if fragment.module != module_name:
            continue
        offset = offsets[fragment_id]
        if not 0 <= offset <= len(source):
            raise VersionBindingError(
                f"fragment offset is outside {module_name}: {fragment_id}")
        placements.append((offset, fragment.payload))
    for offset, payload in sorted(placements, reverse=True):
        output[offset:offset] = payload
    return bytes(output)


def _apply_fragments(source: bytes, policy: ReleasePolicy,
                     offsets: dict[str, int]) -> bytes:
    """Backward-compatible Client-only helper used by focused tests."""
    return _apply_module_fragments(source, policy, offsets, "Client")


def _apply_policy_fragments(
        sources: Mapping[str, bytes], policy: ReleasePolicy,
        offsets: dict[str, int]) -> dict[str, bytes]:
    return {
        module_name: _apply_module_fragments(
            sources[module_name], policy, offsets, module_name)
        for module_name, _path in policy.hosts
    }


def _build_recipe(game_version: str, sources: Mapping[str, bytes],
                  staged_sources: Mapping[str, bytes],
                  policy: ReleasePolicy,
                  offsets: dict[str, int]) -> bytes:
    modules = []
    for module_name, module_path in policy.hosts:
        operations = []
        for fragment_id in policy.fragment_ids:
            fragment = resolve_active_authored_fragment(fragment_id)
            if fragment.module != module_name:
                continue
            operations.append({
                "kind": "insert_fragment",
                "offset": offsets[fragment_id],
                "fragment_id": fragment_id,
                "fragment_sha256": fragment.sha256,
            })
        modules.append({
            "module": module_name,
            "path": module_path,
            "input_sha256": _sha(sources[module_name]),
            "output_sha256": _sha(staged_sources[module_name]),
            "operations": operations,
        })
    payload = _canonical_json({
        "schema": 2,
        "format": "star-empire-ui-hook-recipe",
        "game_version": game_version,
        "modules": modules,
    })
    try:
        recipe = parse_hook_recipe(payload)
        if tuple(module.module for module in recipe.modules) != tuple(sources):
            raise VersionBindingError(
                "generated recipe host inventory changed")
        for module in recipe.modules:
            if module.apply(sources[module.module]) != staged_sources[module.module]:
                raise VersionBindingError(
                    f"generated recipe did not reproduce {module.path}")
    except HookRecipeError as error:
        raise VersionBindingError("generated recipe failed strict validation") from error
    return payload


def _profile_id(policy: ReleasePolicy, game_version: str,
                official_client_sha256: str) -> str:
    if (not game_version
            or any(character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_"
                   for character in game_version)):
        raise VersionBindingError("game version is unsafe for a release identity")
    base = (policy.policy_id[:-3]
            if policy.policy_id.endswith("-v1") else policy.policy_id)
    return f"{base}-{game_version}-{official_client_sha256[:12].lower()}-v1"


def _copy_ui_sources(ui_source: Path, destination: Path,
                     policy: ReleasePolicy) -> dict[str, str]:
    destination.mkdir(parents=True)
    inventory: dict[str, str] = {}
    for name in policy.ui_source_files:
        source = ui_source / name
        if not source.is_file():
            raise VersionBindingError(
                f"policy authored source is missing: {name}")
        payload = source.read_bytes()
        (destination / name).write_bytes(payload)
        inventory[name] = _sha(payload)
    return inventory


def _input_snapshot(
        game_root: Path, policy: ReleasePolicy,
) -> tuple[str, str, dict[str, str]]:
    version = read_game_version(game_root / "version.txt")
    if version is None:
        raise VersionBindingError("game version file is missing")
    client = game_root / "Client.exe"
    sources = {
        name: game_root / "_internal" / path
        for name, path in policy.hosts
    }
    if not client.is_file() or any(
            not source.is_file() for source in sources.values()):
        raise VersionBindingError(
            "game Client.exe or required loose host source is missing")
    return version, sha256_file(client), {
        name: sha256_file(source) for name, source in sources.items()
    }


def build_version_binding(
        *, game_root: Path, output_dir: Path,
        expected_official_client_sha256: str,
        policy_id: str = TARGET_VITALS_ALPHA_POLICY_ID,
        ui_source: Path | None = None,
        repack: Repack = repack_client,
        baseline_probe: BaselineProbe = verify_clean_client_archive,
        process_probe: ProcessProbe = running_game_processes) -> BindingBuild:
    """Build and atomically publish metadata for one exact clean game build."""
    game_root = Path(game_root).resolve(strict=True)
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir == game_root or game_root in output_dir.parents:
        raise VersionBindingError(
            "binding output must be outside the selected game installation")
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite binding output: {output_dir}")
    policy = release_policy(policy_id)
    if ui_source is None:
        ui_source = (
            Path(__file__).resolve().parents[1]
            / policy.payload_directory)
    ui_source = Path(ui_source).resolve(strict=True)

    process_result = process_probe()
    if not process_result.verified:
        raise VersionBindingError(
            f"cannot verify game processes are closed: {process_result.error}")
    if process_result.names:
        raise VersionBindingError(
            "close the game and launcher before generating a binding: "
            + ", ".join(process_result.names))

    before = _input_snapshot(game_root, policy)
    game_version, official_client_sha256, host_input_sha256 = before
    trusted_hash = str(expected_official_client_sha256).strip().upper()
    if (len(trusted_hash) != 64
            or any(character not in "0123456789ABCDEF"
                   for character in trusted_hash)):
        raise VersionBindingError("trusted official Client hash is invalid")
    if official_client_sha256 != trusted_hash:
        raise VersionBindingError(
            "Client.exe does not match the trusted official build hash")
    client = game_root / "Client.exe"
    host_paths = {
        name: game_root / "_internal" / path
        for name, path in policy.hosts
    }
    sources = {name: path.read_bytes() for name, path in host_paths.items()}
    if any(marker in sources["Client"] for marker in UI_MARKERS):
        raise VersionBindingError("loose Client.py already contains UI Mod markers")
    baseline_probe(client, sources["Client"])
    if baseline_probe is verify_clean_client_archive:
        for module_name, module_path in policy.hosts[1:]:
            verify_clean_module_archive(
                client, module_name, module_path, sources[module_name])
    offsets = locate_policy_offsets(sources, policy)
    staged_sources = _apply_policy_fragments(sources, policy, offsets)
    for module_name, module_path in policy.hosts:
        try:
            compile(staged_sources[module_name].decode("utf-8"), module_path,
                    "exec", optimize=0, dont_inherit=True)
        except (UnicodeError, SyntaxError) as error:
            raise VersionBindingError(
                f"generated {module_path} does not compile") from error
    recipe_bytes = _build_recipe(
        game_version, sources, staged_sources, policy, offsets)
    profile_id = _profile_id(policy, game_version, official_client_sha256)

    with tempfile.TemporaryDirectory(prefix="star-empire-ui-binding-") as temporary:
        temporary_root = Path(temporary)
        stage_internal = temporary_root / "stage" / "_internal"
        stage_internal.mkdir(parents=True)
        for module_name, module_path in policy.hosts:
            (stage_internal / module_path).write_bytes(
                staged_sources[module_name])
        stage_client = stage_internal / "Client.py"
        authored_stage = (
            stage_internal / Path(*policy.source_package.split(".")))
        ui_source_sha256 = _copy_ui_sources(
            ui_source, authored_stage, policy)
        modules = (
            *(name for name, _path in policy.hosts),
            *policy.frozen_module_names,
        )
        candidate_one = temporary_root / "candidate-one.exe"
        candidate_two = temporary_root / "candidate-two.exe"
        repack(client, stage_client, candidate_one, modules)
        repack(client, stage_client, candidate_two, modules)
        if not candidate_one.is_file() or not candidate_two.is_file():
            raise VersionBindingError("repacker did not create both candidates")
        first_hash = sha256_file(candidate_one)
        second_hash = sha256_file(candidate_two)
        if first_hash != second_hash:
            raise VersionBindingError("repacking the same build was not deterministic")
        staged_ui_hashes = {
            name: sha256_file(authored_stage / name)
            for name in policy.ui_source_files
        }
        if staged_ui_hashes != ui_source_sha256:
            raise VersionBindingError(
                "staged UI source changed while binding was generated")

    if _input_snapshot(game_root, policy) != before:
        raise VersionBindingError("game updated or changed while binding was generated")

    recipe_name = f"{profile_id}.hook"
    binding_name = f"{profile_id}.binding.json"
    binding_bytes = _canonical_json({
        "schema": BINDING_SCHEMA,
        "format": BINDING_FORMAT,
        "policy_id": policy.policy_id,
        "profile_id": profile_id,
        "game_version": game_version,
        "official_client_sha256": official_client_sha256,
        "expected_client_sha256": first_hash,
        "host_input_sha256": host_input_sha256,
        "host_output_sha256": {
            name: _sha(staged_sources[name]) for name, _path in policy.hosts
        },
        "fragment_offsets": offsets,
        "ui_source_files": list(policy.ui_source_files),
        "ui_source_sha256": ui_source_sha256,
        "recipe_file": recipe_name,
        "recipe_sha256": _sha(recipe_bytes),
    })

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    pending = output_dir.with_name(
        f".{output_dir.name}.partial-{uuid.uuid4().hex}")
    try:
        pending.mkdir()
        (pending / recipe_name).write_bytes(recipe_bytes)
        (pending / binding_name).write_bytes(binding_bytes)
        pending.replace(output_dir)
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return BindingBuild(
        output_dir=output_dir,
        recipe_path=output_dir / recipe_name,
        binding_path=output_dir / binding_name,
        profile_id=profile_id,
        candidate_sha256=first_hash,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-official-client-sha256", required=True,
        help="trusted SHA-256 recorded from the reviewed official Client.exe")
    parser.add_argument("--policy", default=TARGET_VITALS_ALPHA_POLICY_ID)
    parser.add_argument(
        "--authored-source", "--ui-source", dest="ui_source", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        result = build_version_binding(
            game_root=arguments.game_root,
            output_dir=arguments.output_dir,
            expected_official_client_sha256=(
                arguments.expected_official_client_sha256),
            policy_id=arguments.policy,
            ui_source=arguments.ui_source,
        )
    except (OSError, ValueError, VersionBindingError) as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Profile: {result.profile_id}")
    print(f"Recipe: {result.recipe_path}")
    print(f"Binding: {result.binding_path}")
    print(f"Candidate SHA-256: {result.candidate_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
