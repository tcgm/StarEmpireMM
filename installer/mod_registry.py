"""Atomic installed-mod registry and deterministic load-order validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from .mod_manifest import (ModManifest, ModManifestError,
                           mod_manifest_from_mapping, version_at_least)


REGISTRY_SCHEMA = 2


class ModRegistryError(ValueError):
    """Raised when installed mod state is incomplete or contradictory."""


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ModRegistryError(f"{label} must be a SHA-256 string")
    result = value.upper()
    if (len(result) != 64
            or any(char not in "0123456789ABCDEF" for char in result)):
        raise ModRegistryError(f"{label} must be a SHA-256 string")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModRegistryError(f"duplicate mod registry key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class InstalledMod:
    manifest: ModManifest
    install_path: Path
    package_sha256: str
    enabled: bool
    force_load: bool
    source: str
    installed_at: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_mapping(),
            "install_path": str(self.install_path),
            "package_sha256": self.package_sha256,
            "enabled": self.enabled,
            "force_load": self.force_load,
            "source": self.source,
            "installed_at": self.installed_at,
        }


@dataclass(frozen=True)
class ModRegistry:
    mods: tuple[InstalledMod, ...] = ()
    load_order: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "ModRegistry":
        return cls()

    def by_id(self) -> dict[str, InstalledMod]:
        return {item.manifest.mod_id: item for item in self.mods}

    def installed(self, mod_id: str) -> InstalledMod:
        try:
            return self.by_id()[mod_id]
        except KeyError as error:
            raise ModRegistryError(f"mod is not installed: {mod_id}") from error

    def with_installed(
            self, manifest: ModManifest, install_path: Path,
            package_sha256: str, *, source: str,
            enabled: bool | None = None,
            force_load: bool | None = None) -> "ModRegistry":
        items = self.by_id()
        previous = items.get(manifest.mod_id)
        requested_enabled = (previous.enabled if enabled is None and previous
                             else True if enabled is None else bool(enabled))
        requested_force_load = (
            previous.force_load if force_load is None and previous
            else False if force_load is None else bool(force_load))
        source_text = str(source).strip()
        if not source_text:
            raise ModRegistryError("mod source cannot be empty")
        installed_at = (previous.installed_at if previous is not None
                        else datetime.now(timezone.utc).isoformat())
        items[manifest.mod_id] = InstalledMod(
            manifest, Path(install_path).expanduser().resolve(),
            _sha256(package_sha256, "package_sha256"), requested_enabled,
            requested_force_load, source_text, installed_at)
        order = list(self.load_order)
        if manifest.mod_id not in order:
            order.append(manifest.mod_id)
        result = ModRegistry(tuple(items[key] for key in order), tuple(order))
        result.validate_enabled()
        return result

    def with_enabled(self, mod_id: str, enabled: bool) -> "ModRegistry":
        items = self.by_id()
        if mod_id not in items:
            raise ModRegistryError(f"mod is not installed: {mod_id}")
        items[mod_id] = replace(items[mod_id], enabled=bool(enabled))
        result = ModRegistry(
            tuple(items[key] for key in self.load_order), self.load_order)
        result.validate_enabled()
        return result

    def with_force_load(self, mod_id: str, force_load: bool) -> "ModRegistry":
        items = self.by_id()
        if mod_id not in items:
            raise ModRegistryError(f"mod is not installed: {mod_id}")
        items[mod_id] = replace(
            items[mod_id], force_load=bool(force_load))
        result = ModRegistry(
            tuple(items[key] for key in self.load_order), self.load_order)
        result.validate_enabled()
        return result

    def without(self, mod_id: str) -> "ModRegistry":
        items = self.by_id()
        if mod_id not in items:
            raise ModRegistryError(f"mod is not installed: {mod_id}")
        dependents = sorted(
            item.manifest.mod_id for item in self.mods if item.enabled
            and any(dep.mod_id == mod_id
                    for dep in item.manifest.dependencies))
        if dependents:
            raise ModRegistryError(
                f"cannot uninstall {mod_id}; enabled dependents: "
                + ", ".join(dependents))
        order = tuple(key for key in self.load_order if key != mod_id)
        return ModRegistry(tuple(items[key] for key in order), order)

    def validate_enabled(self) -> tuple[str, ...]:
        items = self.by_id()
        enabled = {key: item for key, item in items.items() if item.enabled}
        for mod_id, item in enabled.items():
            for dependency in item.manifest.dependencies:
                installed = enabled.get(dependency.mod_id)
                if installed is None:
                    raise ModRegistryError(
                        f"{mod_id} requires enabled mod {dependency.mod_id}")
                if not version_at_least(
                        installed.manifest.version, dependency.version_min):
                    raise ModRegistryError(
                        f"{mod_id} requires {dependency.mod_id} >= "
                        f"{dependency.version_min}")
            conflicts = sorted(
                other for other in item.manifest.conflicts if other in enabled)
            if conflicts:
                raise ModRegistryError(
                    f"{mod_id} conflicts with enabled mod(s): "
                    + ", ".join(conflicts))
        return self.resolved_load_order()

    def resolved_load_order(self) -> tuple[str, ...]:
        items = self.by_id()
        enabled = {key for key, item in items.items() if item.enabled}
        edges: dict[str, set[str]] = {key: set() for key in enabled}
        indegree = {key: 0 for key in enabled}
        for mod_id in enabled:
            manifest = items[mod_id].manifest
            before = {
                dependency.mod_id for dependency in manifest.dependencies
                if dependency.mod_id in enabled
            }
            before.update(
                key for key in manifest.load_after if key in enabled)
            for predecessor in before:
                if mod_id not in edges[predecessor]:
                    edges[predecessor].add(mod_id)
                    indegree[mod_id] += 1
        priority = {mod_id: index for index, mod_id in enumerate(self.load_order)}
        ready = sorted(
            (key for key, count in indegree.items() if count == 0),
            key=lambda key: (priority.get(key, 10**9), key))
        result: list[str] = []
        while ready:
            current = ready.pop(0)
            result.append(current)
            for dependent in sorted(
                    edges[current], key=lambda key: (priority.get(key, 10**9), key)):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
                    ready.sort(key=lambda key: (priority.get(key, 10**9), key))
        if len(result) != len(enabled):
            raise ModRegistryError("enabled mod dependency/load-order graph contains a cycle")
        return tuple(result)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": REGISTRY_SCHEMA,
            "mods": [item.to_mapping() for item in self.mods],
            "load_order": list(self.load_order),
        }


def _installed_mod(data: Any, schema: int) -> InstalledMod:
    required = {
        "manifest", "install_path", "package_sha256", "enabled",
        "source", "installed_at",
    }
    if schema == 2:
        required.add("force_load")
    if type(data) is not dict or set(data) != required:
        raise ModRegistryError("installed mod entry has unknown or missing fields")
    try:
        manifest = mod_manifest_from_mapping(data["manifest"])
    except ModManifestError as error:
        raise ModRegistryError(str(error)) from error
    if type(data["install_path"]) is not str:
        raise ModRegistryError("install_path must be a string")
    install_path = Path(data["install_path"]).expanduser()
    if not install_path.is_absolute():
        raise ModRegistryError("install_path must be absolute")
    if type(data["enabled"]) is not bool:
        raise ModRegistryError("enabled must be boolean")
    force_load = data.get("force_load", False)
    if type(force_load) is not bool:
        raise ModRegistryError("force_load must be boolean")
    source = str(data["source"]).strip()
    installed_at = str(data["installed_at"]).strip()
    if not source or not installed_at:
        raise ModRegistryError("source and installed_at cannot be empty")
    return InstalledMod(
        manifest, install_path.resolve(),
        _sha256(data["package_sha256"], "package_sha256"),
        data["enabled"], force_load, source, installed_at)


def mod_registry_from_mapping(data: Any) -> ModRegistry:
    if (type(data) is not dict or set(data) != {"schema", "mods", "load_order"}
            or data["schema"] not in {1, REGISTRY_SCHEMA}
            or type(data["mods"]) is not list
            or type(data["load_order"]) is not list):
        raise ModRegistryError("unsupported or malformed mod registry")
    mods = tuple(_installed_mod(item, data["schema"]) for item in data["mods"])
    ids = tuple(item.manifest.mod_id for item in mods)
    if len(set(ids)) != len(ids):
        raise ModRegistryError("mod registry contains duplicate mod IDs")
    if any(type(item) is not str for item in data["load_order"]):
        raise ModRegistryError("load_order must contain mod IDs")
    order = tuple(data["load_order"])
    if len(set(order)) != len(order) or set(order) != set(ids):
        raise ModRegistryError("load_order must contain every installed mod exactly once")
    by_id = {item.manifest.mod_id: item for item in mods}
    result = ModRegistry(tuple(by_id[key] for key in order), order)
    result.validate_enabled()
    return result


def load_mod_registry(path: Path) -> ModRegistry:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        return ModRegistry.empty()
    try:
        data = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModRegistryError(f"cannot read mod registry: {source}") from error
    return mod_registry_from_mapping(data)


def save_mod_registry(path: Path, registry: ModRegistry) -> Path:
    target = Path(path).expanduser().resolve()
    registry.validate_enabled()
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + ".pending")
    if pending.exists():
        raise ModRegistryError(f"stale mod registry write exists: {pending}")
    payload = json.dumps(
        registry.to_mapping(), indent=2, sort_keys=True) + "\n"
    try:
        pending.write_text(payload, encoding="utf-8")
        pending.replace(target)
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    return target
