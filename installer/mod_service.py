"""Transactional external mod installation, enablement, update and removal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Iterable
import uuid

from .manager_core import (GAME_PROCESS_NAMES, ProcessProbeResult,
                           running_game_processes)
from .mod_registry import (ModRegistry, ModRegistryError, load_mod_registry,
                           save_mod_registry)
from .semod_package import VerifiedSemodPackage


class ModServiceError(RuntimeError):
    """Raised when an external mod mutation cannot be completed safely."""


@dataclass(frozen=True)
class ModMutationResult:
    action: str
    mod_id: str
    version: str
    install_path: Path | None
    removed_path: Path | None = None


class ModService:
    """Own one external mod root and its atomic installed-mod registry."""

    def __init__(
            self, state_root: Path, *,
            process_probe: Callable[[], Iterable[str]] = running_game_processes) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.mods_root = self.state_root / "mods"
        self.removed_root = self.state_root / "removed"
        self.registry_path = self.state_root / "mods.json"
        self.lock_path = self.state_root / "mods.lock"
        self._process_probe = process_probe

    def registry(self) -> ModRegistry:
        return load_mod_registry(self.registry_path)

    def _require_game_closed(self) -> None:
        try:
            result = self._process_probe()
            if isinstance(result, ProcessProbeResult) and not result.verified:
                raise ModServiceError(
                    "cannot verify that Star Empire is closed: "
                    f"{result.error}")
            names = {str(name).casefold() for name in result}
        except ModServiceError:
            raise
        except Exception as error:
            raise ModServiceError(
                "cannot verify that Star Empire is closed; no mods were changed") from error
        if names & GAME_PROCESS_NAMES:
            raise ModServiceError(
                "close Star Empire and its launcher before changing mods")

    @contextmanager
    def _exclusive(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        locked = False
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(
                        stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ModServiceError(
                    "another Star Empire mod operation is already active") from error
            locked = True
            yield
        finally:
            if locked:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()

    def _install_target(self, package: VerifiedSemodPackage) -> Path:
        safe_version = package.manifest.version.replace("-", "_")
        return (self.mods_root / package.manifest.mod_id
                / f"{safe_version}-{package.package_sha256[:12].lower()}")

    def _require_managed_path(self, path: Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        try:
            resolved.relative_to(self.mods_root.resolve())
        except ValueError as error:
            raise ModServiceError(
                f"recorded mod path is outside the managed mod root: {resolved}") from error
        return resolved

    @staticmethod
    def _write_integrity_record(
            target: Path, package: VerifiedSemodPackage) -> None:
        """Persist the verified inventory for startup re-verification."""
        # Compatibility loader v4 expects the schema-1 field shape.  This fixed
        # value is a format marker only; no key exists and no trust decision is
        # made from it.  A future loader package can migrate receipts to schema 2.
        record = {
            "schema": 1,
            "package_sha256": package.package_sha256,
            "package_manifest_sha256": package.package_manifest_sha256,
            "key_id": "keyless-external-mod",
            "payloads": [item.to_mapping() for item in package.payloads],
        }
        payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(
            "utf-8")
        path = target / ".semod-install.json"
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _installed_package_is_healthy(
            target: Path, package: VerifiedSemodPackage) -> bool:
        """Return false for unreadable, incomplete, changed, or extra payloads."""
        try:
            expected = {
                item.path.as_posix() for item in package.payloads
            } | {".semod-install.json"}
            actual = set()
            for path in target.rglob("*"):
                if path.is_symlink():
                    return False
                if path.is_file():
                    actual.add(path.relative_to(target).as_posix())
            if actual != expected:
                return False
            for item in package.payloads:
                path = target.joinpath(*item.path.parts)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if (path.stat().st_size != item.size
                        or digest.hexdigest().upper() != item.sha256):
                    return False
            receipt = json.loads((target / ".semod-install.json").read_text(
                encoding="utf-8"))
            return receipt.get("package_sha256") == package.package_sha256
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                AttributeError, TypeError, ValueError):
            return False

    def _repair_install(
            self, registry, package: VerifiedSemodPackage, target: Path,
            *, source: str, enable: bool) -> ModMutationResult:
        """Replace one unhealthy same-package install with rollback."""
        archived = self._archive_path(
            package.manifest.mod_id, package.manifest.version)
        archived.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.replace(archived)
        except OSError as error:
            raise ModServiceError(
                "installed mod is unreadable and could not be moved for repair") from error
        try:
            package.extract(target)
            self._write_integrity_record(target, package)
            next_registry = registry.with_installed(
                package.manifest, target, package.package_sha256,
                source=source, enabled=enable)
            save_mod_registry(self.registry_path, next_registry)
        except Exception as error:
            shutil.rmtree(target, ignore_errors=True)
            try:
                archived.replace(target)
            except OSError as rollback_error:
                raise ModServiceError(
                    "mod repair failed and the previous installation could not "
                    f"be restored; it remains at {archived}") from rollback_error
            raise
        return ModMutationResult(
            "repair", package.manifest.mod_id, package.manifest.version,
            target, archived)

    def install(
            self, package: VerifiedSemodPackage, *, source: str,
            enable: bool = True) -> ModMutationResult:
        if not isinstance(package, VerifiedSemodPackage):
            raise ModServiceError("install requires a verified .semod package")
        with self._exclusive():
            self._require_game_closed()
            registry = self.registry()
            existing = registry.by_id().get(package.manifest.mod_id)
            target = self._install_target(package)
            if (existing is not None
                    and existing.package_sha256 == package.package_sha256
                    and existing.install_path == target
                    and target.is_dir()):
                if not self._installed_package_is_healthy(target, package):
                    return self._repair_install(
                        registry, package, target, source=source, enable=enable)
                next_registry = registry.with_enabled(
                    package.manifest.mod_id, enable)
                save_mod_registry(self.registry_path, next_registry)
                return ModMutationResult(
                    "enable" if enable else "disable",
                    package.manifest.mod_id, package.manifest.version, target)
            if target.exists():
                raise ModServiceError(
                    f"mod install target already exists without matching state: {target}")
            try:
                package.extract(target)
                self._write_integrity_record(target, package)
                next_registry = registry.with_installed(
                    package.manifest, target, package.package_sha256,
                    source=source, enabled=enable)
                save_mod_registry(self.registry_path, next_registry)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                raise

            removed = None
            if existing is not None and existing.install_path != target:
                old_path = self._require_managed_path(existing.install_path)
                if old_path.is_dir():
                    removed = self._archive_path(
                        existing.manifest.mod_id, existing.manifest.version)
                    removed.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        old_path.replace(removed)
                    except OSError:
                        # The registry already points to the verified new version;
                        # retaining an inert old directory is safer than rollback.
                        removed = None
            return ModMutationResult(
                "update" if existing is not None else "install",
                package.manifest.mod_id, package.manifest.version, target, removed)

    def set_enabled(self, mod_id: str, enabled: bool) -> ModMutationResult:
        with self._exclusive():
            self._require_game_closed()
            registry = self.registry()
            next_registry = registry.with_enabled(mod_id, enabled)
            save_mod_registry(self.registry_path, next_registry)
            item = next_registry.installed(mod_id)
            return ModMutationResult(
                "enable" if enabled else "disable", mod_id,
                item.manifest.version, item.install_path)

    def set_force_load(self, mod_id: str, force_load: bool) -> ModMutationResult:
        with self._exclusive():
            self._require_game_closed()
            registry = self.registry()
            next_registry = registry.with_force_load(mod_id, force_load)
            save_mod_registry(self.registry_path, next_registry)
            item = next_registry.installed(mod_id)
            return ModMutationResult(
                "force-load-enable" if force_load else "force-load-disable",
                mod_id, item.manifest.version, item.install_path)

    def _archive_path(self, mod_id: str, version: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return (self.removed_root / mod_id
                / f"{stamp}-{version}-{uuid.uuid4().hex[:8]}")

    def uninstall(self, mod_id: str) -> ModMutationResult:
        with self._exclusive():
            self._require_game_closed()
            registry = self.registry()
            item = registry.installed(mod_id)
            next_registry = registry.without(mod_id)
            install_path = self._require_managed_path(item.install_path)
            if not install_path.is_dir():
                raise ModServiceError(
                    f"recorded mod directory is missing: {install_path}")
            removed = self._archive_path(mod_id, item.manifest.version)
            removed.parent.mkdir(parents=True, exist_ok=True)
            try:
                install_path.replace(removed)
            except PermissionError:
                # Managers before 0.4.3 could leave an elevated, owner-only
                # installation behind.  A standard player process cannot move
                # that legacy directory, but removing it from the registry is
                # sufficient to make it inert and stop the loader seeing it.
                removed = None
            try:
                save_mod_registry(self.registry_path, next_registry)
            except Exception:
                if removed is not None:
                    install_path.parent.mkdir(parents=True, exist_ok=True)
                    removed.replace(install_path)
                raise
            return ModMutationResult(
                ("uninstall" if removed is not None
                 else "uninstall-legacy-orphan"),
                mod_id, item.manifest.version, None, removed)
