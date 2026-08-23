"""Single version authority and runtime identity for the Mod Manager."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from pathlib import Path
import sys


# Public release version. Keep this aligned with the GitHub release tag.
MANAGER_VERSION = "0.4"

# Loader packages created before public version alignment wrote the old
# implementation number into ``manager_version_min``. Translate that historical
# metadata to the public release that replaced it. New packages always write
# ``MANAGER_VERSION`` and therefore never add another alias.
LEGACY_PACKAGE_REQUIREMENT_ALIASES = {"0.4.6": "0.4"}


def package_requirement_version(value: str) -> str:
    """Return the public Manager version represented by package metadata."""
    required = str(value).strip()
    return LEGACY_PACKAGE_REQUIREMENT_ALIASES.get(required, required)


@dataclass(frozen=True)
class ManagerIdentity:
    """Identify the exact Manager executable a player actually launched."""

    version: str
    executable: Path
    executable_sha256: str
    build_id: str
    frozen: bool


@lru_cache(maxsize=1)
def runtime_identity() -> ManagerIdentity:
    """Return a stable version, path and build hash for this process."""
    frozen = bool(getattr(sys, "frozen", False))
    executable = Path(sys.executable if frozen else __file__).resolve()
    digest = ""
    if frozen:
        hasher = hashlib.sha256()
        with executable.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        digest = hasher.hexdigest().upper()
    return ManagerIdentity(
        MANAGER_VERSION, executable, digest,
        digest[:12] if digest else "SOURCE", frozen)


def record_startup(state_root: Path) -> Path:
    """Append the exact running release identity to a local diagnostic log."""
    identity = runtime_identity()
    log = Path(state_root).expanduser().resolve() / "logs" / "manager-startup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            f"{timestamp} version={identity.version} build={identity.build_id} "
            f"sha256={identity.executable_sha256 or 'SOURCE'} "
            f"executable={identity.executable}\n")
    return log


def version_tuple(value: str) -> tuple[int, int, int]:
    """Parse the deliberately small release-version grammar used by packages."""
    parts = str(value).split(".")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError("version must contain two or three numeric components")
    if len(parts) == 2:
        parts.append("0")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]
