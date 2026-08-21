"""Prove a standalone Mod Manager contains tooling, not frozen game code."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable

from PyInstaller.archive.readers import CArchiveReader


PYZ_ENTRY = "PYZ.pyz"
REQUIRED_MODULES = frozenset({
    "installer.manager_ui",
    "installer.manager_core",
    "installer.manager_transaction",
    "installer.mod_package",
    "installer.hook_recipe",
    "installer.authored_fragments",
    "installer.candidate_builder",
    "installer.release_profiles",
    "installer.trusted_keys",
    "installer.update_feed",
    "installer.version",
    "installer.manager_mods",
    "installer.manager_settings",
    "installer.diagnostics",
    "installer.mod_manifest",
    "installer.mod_registry",
    "installer.mod_service",
    "installer.mod_updates",
    "installer.semod_package",
    "mod_loader",
    "mod_loader.host_client",
    "mod_loader.runtime",
    "tkinterdnd2",
    "tkinterdnd2.TkinterDnD",
    "tools.build_version_binding",
    "tools.repack_client",
})
FORBIDDEN_GAME_MODULES = frozenset({
    "Client", "render_mixin", "hangar_inventory", "gl_renderer",
    "item_search", "protocol",
})
FORBIDDEN_PATH_MARKERS = (
    "integration/patchsets", "integration\\patchsets",
    "client.py.patch", "render_mixin.py.patch",
    "game/_internal", "game\\_internal",
)
ALLOWED_RELEASE_COMPANION_NAMES = frozenset({
    "README-FIRST.txt",
    "RELEASE.json",
    "SHA256SUMS.txt",
})
LOADER_PACKAGE_SUFFIXES = frozenset({".seloader", ".seuimod"})


class ManagerArtifactError(RuntimeError):
    """Raised when a release artifact is incomplete or contains private/game data."""


@dataclass(frozen=True)
class ManagerArtifactReport:
    path: Path
    sha256: str
    size: int
    outer_entries: int
    python_modules: int


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def audit_manager_artifact(
        path: Path, *,
        reader_factory: Callable[[str], Any] = CArchiveReader) -> ManagerArtifactReport:
    """Inspect the frozen inventory and fail closed on unexpected content."""
    manager = Path(path).expanduser().resolve(strict=True)
    with manager.open("rb") as stream:
        header = stream.read(2)
    if manager.suffix.lower() != ".exe" or header != b"MZ":
        raise ManagerArtifactError("Manager artifact is not a Windows PE executable")
    siblings = [item for item in manager.parent.iterdir() if item != manager]
    loader_packages = [
        item for item in siblings
        if item.is_file() and item.suffix.lower() in LOADER_PACKAGE_SUFFIXES
    ]
    unexpected = [
        item for item in siblings
        if not item.is_file()
        or (item.name not in ALLOWED_RELEASE_COMPANION_NAMES
            and item.suffix.lower()
            not in LOADER_PACKAGE_SUFFIXES.union({".semod"}))
    ]
    if len(loader_packages) > 1:
        unexpected.extend(loader_packages)
    if unexpected:
        raise ManagerArtifactError(
            "Manager dist is not one-file or has unexpected companions: "
            + ", ".join(sorted({item.name for item in unexpected})))
    try:
        archive = reader_factory(str(manager))
        outer_names = tuple(str(name) for name in archive.toc)
        if PYZ_ENTRY not in archive.toc:
            raise ManagerArtifactError("Manager has no embedded PYZ archive")
        pyz = archive.open_embedded_archive(PYZ_ENTRY)
        module_names = frozenset(str(name) for name in pyz.toc)
    except ManagerArtifactError:
        raise
    except Exception as error:
        raise ManagerArtifactError("Manager CArchive cannot be inspected") from error
    lowered = tuple(name.lower() for name in outer_names)
    for name in lowered:
        if any(marker in name for marker in FORBIDDEN_PATH_MARKERS):
            raise ManagerArtifactError(f"Manager contains forbidden path: {name}")
    forbidden = FORBIDDEN_GAME_MODULES.intersection(module_names)
    if forbidden:
        raise ManagerArtifactError(
            "Manager contains frozen game modules: " + ", ".join(sorted(forbidden)))
    missing = REQUIRED_MODULES.difference(module_names)
    if missing:
        raise ManagerArtifactError(
            "Manager is missing required modules: " + ", ".join(sorted(missing)))
    return ManagerArtifactReport(
        manager, _sha(manager), manager.stat().st_size,
        len(outer_names), len(module_names))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager", required=True, type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _arguments()
    report = audit_manager_artifact(arguments.manager)
    print(f"MANAGER_AUDIT_OK path={report.path}")
    print(f"sha256={report.sha256}")
    print(f"bytes={report.size}")
    print(f"outer_entries={report.outer_entries} python_modules={report.python_modules}")
