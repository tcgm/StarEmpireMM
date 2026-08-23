"""Fail-closed inspection model for the Star Empire UI Mod Manager prototype.

This module deliberately performs no game-file mutation.  It decides whether a
future installer action is safe, and reports why it is not when a check fails.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator


STATE_SCHEMA = 3
PACK_SCHEMA = 1
CLIENT_EXE = "Client.exe"
LAUNCHER_EXE = "StarEmpireLauncher.exe"
GAME_PROCESS_NAMES = frozenset({"client.exe", "starempirelauncher.exe"})


class ManagerDataError(ValueError):
    """Raised when a local manager file is malformed or incomplete."""


@dataclass(frozen=True)
class ProcessProbeResult:
    """A process-list result that distinguishes verified-empty from unknown."""

    names: tuple[str, ...] = ()
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.error is None

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    @classmethod
    def unknown(cls, message: str) -> "ProcessProbeResult":
        return cls((), str(message).strip() or "process inspection failed")


class InstallStatus(str, Enum):
    GAME_NOT_FOUND = "game_not_found"
    GAME_RUNNING = "game_running"
    PROCESS_CHECK_FAILED = "process_check_failed"
    NO_UPDATE_PACK = "no_update_pack"
    UNSUPPORTED_VANILLA = "unsupported_vanilla"
    READY_TO_INSTALL = "ready_to_install"
    INSTALLED_HEALTHY = "installed_healthy"
    BACKUP_INVALID = "backup_invalid"
    MODIFIED_OUTSIDE_MANAGER = "modified_outside_manager"
    INSTALL_STATE_INVALID = "install_state_invalid"


def sha256_file(path: Path) -> str:
    """Return an uppercase SHA-256 without loading a game executable at once."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_game_version_text(payload: str) -> str:
    """Parse the official JSON string or the legacy plain-text version form."""
    if not isinstance(payload, str) or not payload.strip():
        raise ManagerDataError("game version file is empty")
    text = payload.strip()
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        if text[:1] in {'"', "[", "{"}:
            raise ManagerDataError("game version file is malformed") from error
        version = text
    else:
        if not isinstance(decoded, str) or not decoded.strip():
            raise ManagerDataError(
                "game version file must contain one version string")
        version = decoded.strip()
    if any(character in version for character in "\r\n\x00"):
        raise ManagerDataError("game version string contains invalid characters")
    return version


def read_game_version(path: Path) -> str | None:
    """Read a local version file without treating JSON quotes as the version."""
    version_path = Path(path)
    if not version_path.is_file():
        return None
    try:
        payload = version_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManagerDataError(
            f"cannot read game version file: {version_path}") from error
    return parse_game_version_text(payload)


def default_state_path() -> Path:
    """Keep manager state outside the game folder and outside player settings."""
    local_app_data = Path(os.environ.get(
        "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "StarEmpireModManager" / "install-state.json"


@dataclass(frozen=True)
class CompatibilityPack:
    """Metadata for one authorised, version-specific local integration pack."""

    pack_id: str
    mod_version: str
    game_version: str
    official_client_sha256: str
    expected_client_sha256: str
    payload_root: Path
    pack_digest: str = ""
    key_id: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], source: Path) -> "CompatibilityPack":
        if data.get("schema") != PACK_SCHEMA:
            raise ManagerDataError("unsupported update-pack schema")
        required = (
            "pack_id", "mod_version", "game_version",
            "official_client_sha256", "expected_client_sha256",
        )
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise ManagerDataError("update pack is missing: " + ", ".join(missing))
        hashes = (str(data["official_client_sha256"]),
                  str(data["expected_client_sha256"]))
        if any(len(value) != 64 or any(char not in "0123456789abcdefABCDEF"
                                       for char in value) for value in hashes):
            raise ManagerDataError("update-pack hashes must be SHA-256 values")
        pack_digest = str(data.get("pack_digest", "")).upper()
        if (pack_digest and (len(pack_digest) != 64
                or any(char not in "0123456789ABCDEF" for char in pack_digest))):
            raise ManagerDataError("update-pack digest must be a SHA-256 value")
        return cls(
            str(data["pack_id"]), str(data["mod_version"]),
            str(data["game_version"]), hashes[0].upper(), hashes[1].upper(),
            source.parent.resolve(), pack_digest,
            str(data.get("key_id", "")).strip(),
        )

    @classmethod
    def load(cls, path: Path) -> "CompatibilityPack":
        try:
            payload = path.read_bytes()
            raw = json.loads(payload.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ManagerDataError(f"cannot read update pack: {path}") from error
        if not isinstance(raw, dict):
            raise ManagerDataError("update pack must contain one JSON object")
        pack = cls.from_mapping(raw, path)
        if not pack.pack_digest:
            pack = replace(
                pack, pack_digest=hashlib.sha256(payload).hexdigest().upper())
        return pack


@dataclass(frozen=True)
class InstallState:
    """The minimum evidence needed to update or restore safely."""

    game_root: Path
    original_client: Path
    original_sha256: str
    installed_sha256: str
    pack_id: str
    created_at: str
    pack_digest: str = ""
    key_id: str = ""
    mod_version: str = ""
    game_version: str = ""
    compatibility_source_game_version: str = ""

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "schema": STATE_SCHEMA,
            "game_root": str(self.game_root),
            "original_client": str(self.original_client),
            "original_sha256": self.original_sha256,
            "installed_sha256": self.installed_sha256,
            "pack_id": self.pack_id,
            "created_at": self.created_at,
            "pack_digest": self.pack_digest,
            "key_id": self.key_id,
            "mod_version": self.mod_version,
            "game_version": self.game_version,
            "compatibility_source_game_version": (
                self.compatibility_source_game_version),
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "InstallState":
        schema = data.get("schema")
        if schema not in {1, 2, STATE_SCHEMA}:
            raise ManagerDataError("unsupported install-state schema")
        if (schema == STATE_SCHEMA
                and "compatibility_source_game_version" not in data):
            raise ManagerDataError(
                "install state is missing: compatibility_source_game_version")
        required = ("game_root", "original_client", "original_sha256",
                    "installed_sha256", "pack_id", "created_at")
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise ManagerDataError("install state is missing: " + ", ".join(missing))
        hashes = (str(data["original_sha256"]), str(data["installed_sha256"]))
        if any(len(value) != 64 or any(char not in "0123456789abcdefABCDEF"
                                       for char in value) for value in hashes):
            raise ManagerDataError("install-state hashes must be SHA-256 values")
        pack_digest = str(data.get("pack_digest", "")).upper()
        if (pack_digest and (len(pack_digest) != 64
                or any(char not in "0123456789ABCDEF" for char in pack_digest))):
            raise ManagerDataError("install-state pack digest must be a SHA-256 value")
        game_version = str(data.get("game_version", "")).strip()
        compatibility_source = str(
            data.get("compatibility_source_game_version", "")).strip()
        if any(any(character in value for character in "\r\n\x00")
               for value in (game_version, compatibility_source)):
            raise ManagerDataError(
                "install-state game versions contain invalid characters")
        return cls(
            Path(str(data["game_root"])), Path(str(data["original_client"])),
            hashes[0].upper(), hashes[1].upper(), str(data["pack_id"]),
            str(data["created_at"]), pack_digest,
            str(data.get("key_id", "")).strip(),
            str(data.get("mod_version", "")).strip(),
            game_version, compatibility_source,
        )

    @classmethod
    def new(cls, game_root: Path, original_client: Path,
            original_sha256: str, installed_sha256: str,
            pack_id: str, *, pack_digest: str = "", key_id: str = "",
            mod_version: str = "", game_version: str = "",
            compatibility_source_game_version: str = "") -> "InstallState":
        return cls(game_root.resolve(), original_client.resolve(),
                   original_sha256.upper(), installed_sha256.upper(), pack_id,
                   datetime.now(timezone.utc).isoformat(),
                   pack_digest.upper(), key_id, mod_version, game_version,
                   compatibility_source_game_version)


def load_install_state(path: Path) -> InstallState | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManagerDataError(f"cannot read install state: {path}") from error
    if not isinstance(raw, dict):
        raise ManagerDataError("install state must contain one JSON object")
    return InstallState.from_mapping(raw)


def save_install_state(path: Path, state: InstallState) -> None:
    """Save state atomically; later install work must call this only on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    if pending.exists():
        raise FileExistsError(f"stale pending install state: {pending}")
    try:
        pending.write_text(json.dumps(state.to_mapping(), indent=2) + "\n",
                           encoding="utf-8")
        pending.replace(path)
    except Exception:
        pending.unlink(missing_ok=True)
        raise


def _windows_process_names() -> tuple[str, ...]:
    """Enumerate executable names from one read-only Windows process snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    def api_error(operation: str) -> OSError:
        error = ctypes.get_last_error()
        detail = (ctypes.FormatError(error).strip() if error
                  else "Windows did not provide an error code")
        return OSError(error, f"{operation} failed: {detail}")

    ctypes.set_last_error(0)
    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise api_error("CreateToolhelp32Snapshot")

    names: list[str] = []
    failure: OSError | None = None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ctypes.set_last_error(0)
        if not process_first(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error != 18:  # ERROR_NO_MORE_FILES
                failure = api_error("Process32FirstW")
        else:
            while True:
                names.append(str(entry.szExeFile))
                ctypes.set_last_error(0)
                if not process_next(snapshot, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error != 18:  # ERROR_NO_MORE_FILES
                        failure = api_error("Process32NextW")
                    break
    finally:
        ctypes.set_last_error(0)
        if not close_handle(snapshot) and failure is None:
            failure = api_error("CloseHandle")
    if failure is not None:
        raise failure
    return tuple(names)


def running_game_processes() -> ProcessProbeResult:
    """Return a verified process snapshot; uncertainty is never an empty list."""
    if os.name != "nt":
        return ProcessProbeResult.unknown(
            "Game process verification is only supported on Windows.")
    try:
        process_names = _windows_process_names()
    except Exception as error:
        return ProcessProbeResult.unknown(
            f"Could not inspect running game processes: {error}")
    names = {
        name.lower() for name in process_names
        if name.lower() in GAME_PROCESS_NAMES
    }
    return ProcessProbeResult(tuple(sorted(names)))


@dataclass(frozen=True)
class Inspection:
    status: InstallStatus
    game_root: Path
    client_path: Path
    version: str | None
    current_sha256: str | None
    message: str
    state: InstallState | None = None
    pack: CompatibilityPack | None = None

    @property
    def can_install(self) -> bool:
        return self.status is InstallStatus.READY_TO_INSTALL

    @property
    def can_update(self) -> bool:
        return (self.status is InstallStatus.INSTALLED_HEALTHY
                and self.pack is not None
                and self.state is not None
                and self.state.original_sha256 == self.pack.official_client_sha256
                and (self.version is None
                     or self.version == self.pack.game_version)
                and not (self.state.pack_id == self.pack.pack_id
                         and self.state.pack_digest
                         and self.pack.pack_digest
                         and self.state.pack_digest != self.pack.pack_digest)
                and self.state.pack_id != self.pack.pack_id)

    @property
    def can_restore(self) -> bool:
        return self.status is InstallStatus.INSTALLED_HEALTHY


def inspect_installation(game_root: Path, pack: CompatibilityPack | None,
                         state_path: Path | None = None,
                         running_processes: Iterable[str] = ()) -> Inspection:
    """Classify one installation and fail closed on every uncertain condition."""
    root = Path(game_root).expanduser()
    client = root / CLIENT_EXE
    version_path = root / "version.txt"
    if not client.is_file() or not (root / LAUNCHER_EXE).is_file():
        return Inspection(InstallStatus.GAME_NOT_FOUND, root, client, None,
                          None, "Select a folder containing Client.exe and StarEmpireLauncher.exe.",
                          pack=pack)
    try:
        version = read_game_version(version_path)
    except ManagerDataError as error:
        return Inspection(
            InstallStatus.UNSUPPORTED_VANILLA, root, client, None, None,
            f"Cannot verify the selected game's version: {error}", pack=pack)
    if (isinstance(running_processes, ProcessProbeResult)
            and not running_processes.verified):
        return Inspection(
            InstallStatus.PROCESS_CHECK_FAILED, root, client, version, None,
            "Cannot safely continue because the game process check failed: "
            f"{running_processes.error}", pack=pack)
    names = {str(name).lower() for name in running_processes}
    if names & GAME_PROCESS_NAMES:
        return Inspection(InstallStatus.GAME_RUNNING, root, client, version,
                          None, "Close Star Empire and its launcher before changing files.",
                          pack=pack)
    current = sha256_file(client)
    state_file = state_path or default_state_path()
    try:
        state = load_install_state(state_file)
    except ManagerDataError as error:
        return Inspection(InstallStatus.INSTALL_STATE_INVALID, root, client,
                          version, current, str(error), pack=pack)

    pack_version_matches = (pack is not None
                            and (version is None or version == pack.game_version))
    state_matches_root = (state is not None
                          and state.game_root.resolve() == root.resolve())

    if state_matches_root and state is not None:
        if current == state.installed_sha256:
            if (not state.original_client.is_file()
                    or sha256_file(state.original_client) != state.original_sha256):
                return Inspection(InstallStatus.BACKUP_INVALID, root, client,
                                  version, current,
                                  "The recorded vanilla backup is missing or has changed; restore and update are blocked.",
                                  state, pack)
            compatible_update = (pack is None
                                 or (pack_version_matches
                                     and pack.official_client_sha256
                                     == state.original_sha256))
            message = "Installed UI Mod and original backup both verify correctly."
            if not compatible_update:
                message += " The selected update pack targets a different official game build; update is blocked."
            elif (pack is not None and state.pack_id == pack.pack_id
                  and state.pack_digest and pack.pack_digest
                  and state.pack_digest != pack.pack_digest):
                message += " The selected package reuses an installed pack ID with different signed bytes; update is blocked."
            return Inspection(InstallStatus.INSTALLED_HEALTHY, root, client,
                              version, current, message, state, pack)

        # The official launcher can replace a modded client during a game
        # update.  An exact selected-pack hash is sufficient to recognise the
        # new vanilla baseline; the old backup must never be restored over it.
        if (pack is not None and pack_version_matches
                and current == pack.official_client_sha256):
            return Inspection(
                InstallStatus.READY_TO_INSTALL, root, client, version, current,
                "A supported official game update is installed. Install will create a new version-specific vanilla backup.",
                state, pack)

        # The launcher also updates builds before a matching signed binding is
        # available.  A changed reported version plus a third Client.exe hash
        # is an update candidate, not proof of an unsafe same-build edit.  The
        # compatibility builder still has to verify the clean embedded source,
        # locate every reviewed hook structurally, compile, repack, and audit
        # the candidate before the transaction can touch this new baseline.
        game_version_changed = (
            bool(version) and bool(state.game_version)
            and version != state.game_version
            and current not in {
                state.original_sha256, state.installed_sha256,
            }
        )
        if game_version_changed:
            if pack is None:
                return Inspection(
                    InstallStatus.NO_UPDATE_PACK, root, client, version, current,
                    "A new game version replaced the managed client, but this "
                    "Manager has no usable internal compatibility template. "
                    "Update the Mod Manager and try again.",
                    state, None)
            return Inspection(
                InstallStatus.UNSUPPORTED_VANILLA, root, client, version,
                current,
                "A new game version replaced the managed client. The Manager must structurally verify and rebuild the loader hooks before installing; the new vanilla client will not be overwritten if any required hook changed.",
                state, pack)

        if (current != state.original_sha256
                and (not state.original_client.is_file()
                     or sha256_file(state.original_client) != state.original_sha256)):
            return Inspection(InstallStatus.BACKUP_INVALID, root, client,
                              version, current,
                              "The recorded vanilla backup is missing or has changed; restore is blocked.",
                              state, pack)

        if current != state.original_sha256:
            return Inspection(InstallStatus.MODIFIED_OUTSIDE_MANAGER, root, client,
                              version, current,
                              "Client.exe differs from both recorded vanilla and modded hashes; no overwrite is safe.",
                              state, pack)

    if pack is None:
        return Inspection(InstallStatus.NO_UPDATE_PACK, root, client, version,
                          current,
                          "Internal compatibility support is unavailable. "
                          "Update the Mod Manager and try again.",
                          state, None)
    if not pack_version_matches or current != pack.official_client_sha256:
        return Inspection(InstallStatus.UNSUPPORTED_VANILLA, root, client,
                          version, current,
                          "This official game build or reported version is not supported by the selected update pack.",
                          state, pack)
    return Inspection(InstallStatus.READY_TO_INSTALL, root, client, version,
                      current,
                      "Exact official build recognised. Install may create a verified candidate.",
                      state, pack)
