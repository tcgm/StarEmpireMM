"""Restricted Windows elevation for replacing Star Empire's Client.exe.

The normal Manager process prepares and verifies every transaction.  This
module only performs the final atomic copy when Windows denies that process
permission to replace Client.exe.  It cannot target another filename or use a
source outside the Manager's candidate and backup folders.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Iterable
import uuid

from .manager_core import (GAME_PROCESS_NAMES, ProcessProbeResult,
                           default_state_path, running_game_processes,
                           sha256_file)


REQUEST_SCHEMA = 1
REQUEST_DIRECTORY = "elevation-requests"
MAX_REQUEST_BYTES = 32 * 1024


class ElevationError(RuntimeError):
    """Raised when the restricted elevated replacement cannot be completed."""


class ElevationCancelled(ElevationError):
    """Raised when the player declines the Windows administrator prompt."""


def _normalise_hash(value: object, label: str) -> str:
    digest = str(value).strip().upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise ElevationError(f"Elevated copy request has an invalid {label} hash.")
    return digest


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_paths(state_root: Path, source: Path, target: Path) -> tuple[Path, Path]:
    state_root = Path(state_root).expanduser().resolve(strict=True)
    source = Path(source).expanduser().resolve(strict=True)
    target = Path(target).expanduser().resolve(strict=True)
    allowed_sources = (
        (state_root / "work").resolve(),
        (state_root / "backups").resolve(),
    )
    if (not source.is_file() or source.is_symlink()
            or not any(_inside(source, root) for root in allowed_sources)):
        raise ElevationError(
            "Elevated copy source is outside the Manager work and backup folders.")
    if source == target:
        raise ElevationError("Elevated copy source and target must be different files.")
    if target.name.lower() != "client.exe" or not target.is_file() or target.is_symlink():
        raise ElevationError("Elevated copy target must be an existing Client.exe.")
    game_root = target.parent
    launcher = game_root / "StarEmpireLauncher.exe"
    version = game_root / "version.txt"
    if (not launcher.is_file() or launcher.is_symlink()
            or not version.is_file() or version.is_symlink()):
        raise ElevationError(
            "Elevated copy target is not a recognised Star Empire game folder.")
    return source, target


def _require_game_closed(
        process_probe: Callable[[], Iterable[str]]) -> None:
    try:
        result = process_probe()
        if isinstance(result, ProcessProbeResult) and not result.verified:
            raise ElevationError(
                "Cannot verify that Star Empire is closed: " + str(result.error))
        names = {str(name).lower() for name in result}
    except ElevationError:
        raise
    except Exception as error:
        raise ElevationError(
            "Cannot verify that Star Empire is closed; no files were changed.") from error
    if names & GAME_PROCESS_NAMES:
        raise ElevationError("Close Star Empire and its launcher before changing files.")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _result_path(request_path: Path) -> Path:
    return request_path.with_name(request_path.stem + ".result.json")


def _read_request(request_path: Path, state_root: Path) -> dict[str, object]:
    state_root = Path(state_root).expanduser().resolve(strict=True)
    request_root = (state_root / REQUEST_DIRECTORY).resolve()
    request_path = Path(request_path).expanduser().resolve(strict=True)
    if (request_path.parent != request_root or request_path.suffix.lower() != ".json"
            or not request_path.name.startswith("copy-")
            or request_path.name.endswith(".result.json")):
        raise ElevationError("Elevated copy request is outside its private request folder.")
    if request_path.stat().st_size > MAX_REQUEST_BYTES:
        raise ElevationError("Elevated copy request is too large.")
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ElevationError("Elevated copy request cannot be read.") from error
    if not isinstance(payload, dict) or payload.get("schema") != REQUEST_SCHEMA:
        raise ElevationError("Elevated copy request has an unsupported format.")
    expected_id = request_path.stem.removeprefix("copy-")
    if str(payload.get("request_id", "")) != expected_id:
        raise ElevationError("Elevated copy request identity does not match its filename.")
    return payload


def _perform_elevated_copy(
        request_path: Path, state_root: Path,
        process_probe: Callable[[], Iterable[str]] = running_game_processes) -> None:
    payload = _read_request(request_path, state_root)
    expected_sha256 = _normalise_hash(payload.get("expected_sha256"), "expected")
    before_sha256 = _normalise_hash(payload.get("before_sha256"), "original")
    source, target = _validate_paths(
        state_root, Path(str(payload.get("source", ""))),
        Path(str(payload.get("target", ""))))
    if sha256_file(source) != expected_sha256:
        raise ElevationError("Elevated copy source changed after verification.")
    if sha256_file(target) != before_sha256:
        raise ElevationError("Client.exe changed before elevated replacement.")
    _require_game_closed(process_probe)

    pending = target.with_name(
        f".{target.name}.mod-manager-elevated-{uuid.uuid4().hex}.pending")
    try:
        shutil.copy2(source, pending)
        if sha256_file(pending) != expected_sha256:
            raise ElevationError("Elevated copy failed SHA-256 verification.")
        _require_game_closed(process_probe)
        if sha256_file(source) != expected_sha256:
            raise ElevationError("Elevated copy source changed before replacement.")
        if sha256_file(target) != before_sha256:
            raise ElevationError("Client.exe changed before elevated replacement.")
        os.replace(pending, target)
        if sha256_file(target) != expected_sha256:
            raise ElevationError("Elevated replacement failed SHA-256 verification.")
    finally:
        pending.unlink(missing_ok=True)


def handle_elevated_copy_request(
        request_path: Path, *, state_root: Path | None = None,
        process_probe: Callable[[], Iterable[str]] = running_game_processes) -> int:
    """Run one validated request and leave a bounded result for the parent."""
    root = (Path(state_root).expanduser().resolve()
            if state_root is not None else default_state_path().parent.resolve())
    request = Path(request_path).expanduser().resolve()
    request_root = (root / REQUEST_DIRECTORY).resolve()
    if request.parent != request_root:
        return 64
    result = _result_path(request)
    try:
        _perform_elevated_copy(request, root, process_probe)
    except Exception as error:
        try:
            _write_json_atomic(result, {
                "schema": REQUEST_SCHEMA,
                "success": False,
                "message": str(error)[:2048],
            })
        except OSError:
            pass
        return 1
    try:
        _write_json_atomic(result, {
            "schema": REQUEST_SCHEMA,
            "success": True,
            "message": "Client.exe replaced and verified.",
        })
    except OSError:
        return 2
    return 0


def _launch_elevated(request_path: Path) -> int:
    if os.name != "nt":
        raise ElevationError("Administrator elevation is only available on Windows.")

    if getattr(sys, "frozen", False):
        executable = str(Path(sys.executable).resolve())
        arguments = ["--elevated-copy-request", str(request_path)]
    else:
        executable = str(Path(sys.executable).resolve())
        arguments = ["-m", "installer.manager_entry",
                     "--elevated-copy-request", str(request_path)]

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell_execute = ctypes.windll.shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell_execute.restype = wintypes.BOOL
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(Path(executable).parent)
    info.nShow = 0
    if not shell_execute(ctypes.byref(info)):
        error = ctypes.windll.kernel32.GetLastError()
        if error == 1223:
            raise ElevationCancelled("Administrator permission was declined.")
        raise ElevationError(
            f"Windows could not start the restricted administrator step (error {error}).")
    if not info.hProcess:
        raise ElevationError("Windows did not return the administrator process handle.")
    try:
        wait = ctypes.windll.kernel32.WaitForSingleObject
        wait_result = wait(info.hProcess, 0xFFFFFFFF)
        if wait_result == 0xFFFFFFFF:
            raise ElevationError("Could not wait for the administrator step.")
        exit_code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
                info.hProcess, ctypes.byref(exit_code)):
            raise ElevationError("Could not read the administrator step result.")
        return int(exit_code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)


def request_elevated_atomic_copy(
        state_root: Path, source: Path, target: Path,
        expected_sha256: str, before_sha256: str,
        *, launcher: Callable[[Path], int] = _launch_elevated) -> None:
    """Ask Windows to run the restricted final replacement as administrator."""
    root = Path(state_root).expanduser().resolve(strict=True)
    source, target = _validate_paths(root, source, target)
    expected = _normalise_hash(expected_sha256, "expected")
    before = _normalise_hash(before_sha256, "original")
    if sha256_file(source) != expected:
        raise ElevationError("Elevated copy source no longer matches its verified hash.")
    if sha256_file(target) != before:
        raise ElevationError("Client.exe changed before administrator permission was requested.")

    request_root = root / REQUEST_DIRECTORY
    request_root.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    request = request_root / f"copy-{request_id}.json"
    result = _result_path(request)
    _write_json_atomic(request, {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "source": str(source),
        "target": str(target),
        "expected_sha256": expected,
        "before_sha256": before,
    })
    try:
        exit_code = launcher(request)
        try:
            response = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ElevationError(
                f"Administrator step ended with code {exit_code} without a valid result.") from error
        if (not isinstance(response, dict)
                or response.get("schema") != REQUEST_SCHEMA
                or response.get("success") is not True
                or exit_code != 0):
            message = (str(response.get("message", "")).strip()
                       if isinstance(response, dict) else "")
            raise ElevationError(message or "Administrator replacement was blocked.")
        if sha256_file(target) != expected:
            raise ElevationError("Client.exe did not match the verified result after elevation.")
    finally:
        request.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
