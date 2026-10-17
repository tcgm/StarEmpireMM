"""Per-mod declarative host-source patches, applied before each launch.

Unlike hook_recipe.py's AuthoredFragment catalog (a small, hand-curated set
of insertion points baked into THIS manager's own source and used to bolt
the mod-loader framework itself onto the game), this is a general-purpose
mechanism any installed, enabled external mod can use: a mod ships a
host_patches.json file declaring exact text replacements against one of
the game's own host modules, and the manager applies them -- idempotently,
safely, and with no insertion text of ITS OWN to review, since the text
comes from the mod package the player already chose to install.

That's a deliberately different trust boundary than hook_recipe.py's: an
installed mod already runs arbitrary Python inside the game process via
the loader (see mod_loader/), so text-patching a host .py file it can
already import and monkeypatch at runtime is not meaningfully more
powerful -- it just reaches a few spots (deep inside one large method,
with no self-contained sub-method to override) that runtime monkeypatching
alone cannot. See ship3d-mod's own use of this for a concrete example: a
hook method + a handful of call-site edits inside render_mixin.py's
_draw_ships, needed because ship name tags are plain screen.blit() calls
with no extractable method around them.

Safety properties, mirroring hook_recipe.py's own where this file's much
lighter trust model still allows:
  - Idempotent: a `marker` string is checked first; if already present,
    the whole module's patch set is skipped (already applied).
  - Exact-match only: every `old` snippet must appear EXACTLY ONCE in the
    current file text before ANY replacement in that module is applied --
    never a partial/best-effort patch, and never silently patches the
    wrong occurrence.
  - All-or-nothing per module: if any snippet doesn't match exactly once,
    the WHOLE module's patch set is skipped (reported, not applied),
    never applied partially.
  - Isolated: one mod's bad or stale patch is caught and reported, never
    allowed to block other mods' patches or the game from launching.

No module here uses the `logging` package (nothing in this installer
package does -- see e.g. ModMutationResult in mod_service.py); results are
returned as plain dataclasses instead, for the caller (manager_ui.py) to
display or ignore as it sees fit.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .hook_recipe import HOST_MODULE_PATHS

HOST_PATCHES_FORMAT = "star-empire-mod-host-patches"
HOST_PATCHES_SCHEMA = 1
HOST_PATCHES_FILENAME = "host_patches.json"
MAX_HOST_PATCHES_BYTES = 256 * 1024
MAX_REPLACEMENTS_PER_MODULE = 64


class ModHostPatchError(ValueError):
    """Raised when a mod's host_patches.json is malformed or unsafe."""


@dataclass(frozen=True)
class HostReplacement:
    old: str
    new: str


@dataclass(frozen=True)
class ModulePatch:
    module: str
    marker: str
    replacements: tuple[HostReplacement, ...]


@dataclass(frozen=True)
class ModHostPatchSet:
    mod_id: str
    modules: tuple[ModulePatch, ...]


@dataclass(frozen=True)
class ModHostPatchReport:
    """Outcome of reconciling every enabled mod's host_patches.json."""

    applied: tuple[str, ...] = ()   # "mod_id:module", newly patched this call
    skipped: tuple[str, ...] = ()   # "mod_id:module: reason"

    def __bool__(self) -> bool:
        return bool(self.applied or self.skipped)


def _require_text(value: Any, label: str, *, maximum: int) -> str:
    if type(value) is not str or not value:
        raise ModHostPatchError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise ModHostPatchError(f"{label} is too long")
    return value


def parse_mod_host_patches(payload: bytes, *, mod_id: str) -> ModHostPatchSet:
    """Validate strict schema-1 JSON; raises ModHostPatchError otherwise."""
    if not payload or len(payload) > MAX_HOST_PATCHES_BYTES:
        raise ModHostPatchError("host_patches.json size is outside limits")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModHostPatchError("host_patches.json is not valid UTF-8 JSON") from error
    if not isinstance(data, dict) or set(data) != {"schema", "format", "patches"}:
        raise ModHostPatchError("host_patches.json has unknown or missing fields")
    if (data.get("schema") != HOST_PATCHES_SCHEMA
            or data.get("format") != HOST_PATCHES_FORMAT):
        raise ModHostPatchError("unsupported host_patches.json format")
    raw_patches = data["patches"]
    if not isinstance(raw_patches, list) or not raw_patches:
        raise ModHostPatchError("host_patches.json must declare at least one patch")

    modules: list[ModulePatch] = []
    seen_modules: set[str] = set()
    for raw_module in raw_patches:
        if not isinstance(raw_module, dict) or set(raw_module) != {
                "module", "marker", "replacements"}:
            raise ModHostPatchError("each patch must contain module, marker, replacements")
        module = raw_module["module"]
        if type(module) is not str or module not in HOST_MODULE_PATHS:
            raise ModHostPatchError(f"unknown host module: {module!r}")
        if module in seen_modules:
            raise ModHostPatchError(f"duplicate patch for module: {module}")
        seen_modules.add(module)
        marker = _require_text(raw_module["marker"], "marker", maximum=200)
        raw_replacements = raw_module["replacements"]
        if (not isinstance(raw_replacements, list) or not raw_replacements
                or len(raw_replacements) > MAX_REPLACEMENTS_PER_MODULE):
            raise ModHostPatchError(
                f"{module}: replacements must be a non-empty, bounded list")
        replacements: list[HostReplacement] = []
        for raw_replacement in raw_replacements:
            if not isinstance(raw_replacement, dict) or set(raw_replacement) != {
                    "old", "new"}:
                raise ModHostPatchError("each replacement must contain old and new")
            old = _require_text(
                raw_replacement["old"], f"{module} replacement.old", maximum=16384)
            new_raw = raw_replacement["new"]
            if type(new_raw) is not str or len(new_raw) > 16384:
                raise ModHostPatchError(f"{module} replacement.new is invalid")
            replacements.append(HostReplacement(old, new_raw))
        modules.append(ModulePatch(module, marker, tuple(replacements)))
    return ModHostPatchSet(mod_id, tuple(modules))


def _mod_host_patches_path(item: Any) -> Path:
    """host_patches.json ships INSIDE the mod package's own "mod/<package>/"
    tree (so it's covered by the same manifest `files` validation and
    package-integrity hash check as every other mod file), at the top of
    the entrypoint's own top-level package directory -- e.g. entrypoint
    "ship3d_mod.main:apply" -> mod/ship3d_mod/host_patches.json."""
    module_name = item.manifest.entrypoint.split(":", 1)[0]
    package_dir = module_name.split(".", 1)[0]
    return Path(item.install_path) / "mod" / package_dir / HOST_PATCHES_FILENAME


def _apply_one_mod(
        internal_root: Path, patch_set: ModHostPatchSet,
        applied: list[str], skipped: list[str]) -> None:
    for module_patch in patch_set.modules:
        label = f"{patch_set.mod_id}:{module_patch.module}"
        try:
            rel_path = HOST_MODULE_PATHS[module_patch.module]
            path = Path(internal_root) / rel_path
            if not path.is_file():
                skipped.append(f"{label}: host module not found at {path}")
                continue
            text = path.read_text(encoding="utf-8")
            if module_patch.marker in text:
                continue  # already applied (by this mod, a previous launch)
            missing_or_ambiguous = sum(
                1 for replacement in module_patch.replacements
                if text.count(replacement.old) != 1)
            if missing_or_ambiguous:
                skipped.append(
                    f"{label}: {missing_or_ambiguous} snippet(s) not found "
                    "exactly once in the current host source -- the mod "
                    "may need an update for the current game version")
                continue
            for replacement in module_patch.replacements:
                text = text.replace(replacement.old, replacement.new, 1)
            if module_patch.marker not in text:
                skipped.append(
                    f"{label}: patch applied but its own marker is still "
                    "missing afterward -- not written")
                continue
            path.write_text(text, encoding="utf-8")
            applied.append(label)
        except Exception as error:
            skipped.append(f"{label}: {type(error).__name__}: {error}")


def reconcile_all_mod_host_patches(
        internal_root: Path, installed_mods: Mapping[str, Any]) -> ModHostPatchReport:
    """Scan every ENABLED installed mod for a host_patches.json in its
    package, and apply any that aren't already applied. `installed_mods`
    is a {mod_id: InstalledMod} mapping (ModRegistry.by_id()). Never
    raises -- one mod's bad patch is reported in `skipped`, never allowed
    to block another mod's patch or the caller's launch flow."""
    applied: list[str] = []
    skipped: list[str] = []
    for mod_id, item in installed_mods.items():
        if not item.enabled:
            continue
        patches_path = _mod_host_patches_path(item)
        if not patches_path.is_file():
            continue
        try:
            payload = patches_path.read_bytes()
            patch_set = parse_mod_host_patches(payload, mod_id=mod_id)
        except ModHostPatchError as error:
            skipped.append(f"{mod_id}: invalid host_patches.json ({error})")
            continue
        except OSError as error:
            skipped.append(f"{mod_id}: could not read host_patches.json ({error})")
            continue
        _apply_one_mod(internal_root, patch_set, applied, skipped)
    return ModHostPatchReport(tuple(applied), tuple(skipped))
