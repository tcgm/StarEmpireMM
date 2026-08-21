"""Manager-facing operations for external ``.semod`` packages."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from mod_loader.host_client import STATE_DIR_NAME, STATE_ROOT_ENV

from .mod_registry import InstalledMod, ModRegistryError
from .mod_manifest import ModCompatibility
from .mod_service import ModMutationResult, ModService
from .mod_updates import AcquiredModUpdate, acquire_github_update
from .semod_package import (SemodPackageError, VerifiedSemodPackage,
                            verify_semod_package)


def default_mod_state_root() -> Path:
    """Return the one state root shared by Manager and frozen loader."""
    override = os.environ.get(STATE_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return (root / STATE_DIR_NAME).resolve()


@dataclass(frozen=True)
class ModSummary:
    mod_id: str
    name: str
    version: str
    author: str
    enabled: bool
    force_load: bool
    compatibility: ModCompatibility | None
    source: str
    update_repository: str | None


class ModManagerController:
    """Verify packages and delegate all mutations to ``ModService``."""

    def __init__(
            self, state_root: Path | None = None, *, service: ModService | None = None,
    ) -> None:
        self.state_root = (
            default_mod_state_root() if state_root is None
            else Path(state_root).expanduser().resolve())
        self.service = service or ModService(self.state_root)

    def summaries(self) -> tuple[ModSummary, ...]:
        registry = self.service.registry()
        result: list[ModSummary] = []
        for mod_id in registry.load_order:
            item = registry.installed(mod_id)
            update = item.manifest.update
            result.append(ModSummary(
                mod_id=mod_id,
                name=item.manifest.name,
                version=item.manifest.version,
                author=item.manifest.author,
                enabled=item.enabled,
                force_load=item.force_load,
                compatibility=item.manifest.compatibility,
                source=item.source,
                update_repository=(
                    None if update is None else update.repository),
            ))
        return tuple(result)

    def verify(self, package_path: Path) -> VerifiedSemodPackage:
        return verify_semod_package(Path(package_path))

    def install(
            self, package_path: Path, *, enable: bool = False,
            source: str | None = None,
    ) -> ModMutationResult:
        package = self.verify(package_path)
        source_text = source or f"local:{Path(package_path).name}"
        return self.service.install(
            package, source=source_text, enable=enable)

    def check_update(self, mod_id: str) -> AcquiredModUpdate | None:
        installed = self.service.registry().installed(mod_id)
        if installed.manifest.update is None:
            return None
        return acquire_github_update(
            installed, self.state_root / "downloads" / "mods")

    def apply_update(self, update: AcquiredModUpdate) -> ModMutationResult:
        if not isinstance(update, AcquiredModUpdate):
            raise SemodPackageError("update must be a verified mod package")
        current = self.service.registry().installed(update.package.manifest.mod_id)
        if current.package_sha256 != update.previous_package_sha256:
            raise ModRegistryError(
                "installed mod changed while its update was downloading; check again")
        return self.service.install(
            update.package, source=f"github:{update.repository}",
            enable=current.enabled)

    def set_enabled(self, mod_id: str, enabled: bool) -> ModMutationResult:
        return self.service.set_enabled(mod_id, enabled)

    def set_force_load(self, mod_id: str, force_load: bool) -> ModMutationResult:
        return self.service.set_force_load(mod_id, force_load)

    def toggle(self, mod_id: str) -> ModMutationResult:
        item = self.service.registry().installed(mod_id)
        return self.set_enabled(mod_id, not item.enabled)

    def uninstall(self, mod_id: str) -> ModMutationResult:
        return self.service.uninstall(mod_id)

    def installed(self, mod_id: str) -> InstalledMod:
        return self.service.registry().installed(mod_id)


__all__ = (
    "ModManagerController",
    "ModRegistryError",
    "ModSummary",
    "default_mod_state_root",
)
