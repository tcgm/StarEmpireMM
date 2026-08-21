"""Create bounded, source-free Star Empire Mod Manager diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import uuid
import zipfile

from .manager_core import CLIENT_EXE, read_game_version, sha256_file
from .mod_registry import ModRegistryError, load_mod_registry
from .version import MANAGER_VERSION


MAX_LOG_FILES = 8
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_VIEW_LOG_FILES = 4
MAX_VIEW_LOG_BYTES = 256 * 1024
MAX_VIEW_EVENTS_PER_FILE = 80


_LOG_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s*(?P<message>.*)$")
_EXCEPTION_CAUSE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*.+$")


class DiagnosticsError(RuntimeError):
    """Raised before an incomplete or unsafe report is published."""


@dataclass(frozen=True)
class ParsedLogEvent:
    timestamp: str
    level: str
    logger: str
    message: str
    cause: str = ""


def _friendly_message(message: str) -> str:
    text = str(message).strip()
    mod_match = re.search(r"\bmod_id=([^\s]+)", text)
    event_match = re.search(r"\bevent=([^\s]+)", text)
    if "MOD_LOAD_FAILED" in text:
        return (f"The mod '{mod_match.group(1)}' could not be loaded."
                if mod_match else "A mod could not be loaded.")
    if "MOD_CALLBACK_FAILED" in text:
        return (
            f"A mod failed while handling '{event_match.group(1)}'; "
            "the loader isolated the error."
            if event_match else
            "A mod callback failed; the loader isolated the error.")
    if "STAR_EMPIRE_MOD_LOADER_EVENT_FAILED" in text:
        return "The game could not pass an input event to the mod loader."
    if "STAR_EMPIRE_MOD_LOADER_DRAW_FAILED" in text:
        return "The mod loader could not complete a drawing event."
    if text.startswith("UI_MOD_") and "_FAILED" in text:
        return "A UI component failed and the vanilla display was used instead."
    if "STAR_EMPIRE_MOD_LOG_SETUP_FAILED" in text:
        return "The mod log file could not be opened; the game continued."
    return text or "Log event"


def parse_log_events(payload: bytes) -> tuple[ParsedLogEvent, ...]:
    """Parse standard loader logs into concise player-readable events."""
    lines = payload.decode("utf-8", errors="replace").splitlines()
    events: list[ParsedLogEvent] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        details = [str(line).strip() for line in current["details"]]
        cause = next((
            line for line in reversed(details)
            if _EXCEPTION_CAUSE.match(line)
        ), "")
        events.append(ParsedLogEvent(
            str(current["timestamp"]), str(current["level"]),
            str(current["logger"]),
            _friendly_message(str(current["message"])), cause))
        current = None

    for line in lines:
        match = _LOG_HEADER.match(line)
        if match:
            flush()
            current = {
                "timestamp": match.group("timestamp"),
                "level": match.group("level"),
                "logger": match.group("logger"),
                "message": match.group("message"),
                "details": [],
            }
        elif current is None:
            if line.strip():
                current = {
                    "timestamp": "", "level": "INFO", "logger": "",
                    "message": line.strip(), "details": [],
                }
        else:
            current["details"].append(line)
    flush()
    return tuple(events)


def _display_time(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return value or "time unavailable"
    return parsed.strftime("%d %b %Y %H:%M:%S")


def _report(game_root: Path | None, mod_state_root: Path) -> dict:
    game = None
    if game_root is not None:
        root = Path(game_root).expanduser().resolve()
        client = root / CLIENT_EXE
        try:
            version = read_game_version(root / "version.txt")
        except Exception as error:
            version = f"unreadable: {type(error).__name__}"
        game = {
            "path": str(root),
            "version": version,
            "client_sha256": (
                sha256_file(client) if client.is_file() else None),
        }
    try:
        registry = load_mod_registry(mod_state_root / "mods.json")
        mods = [{
            "mod_id": item.manifest.mod_id,
            "name": item.manifest.name,
            "version": item.manifest.version,
            "author": item.manifest.author,
            "enabled": item.enabled,
            "force_load": item.force_load,
            "package_sha256": item.package_sha256,
            "source": item.source,
        } for item in registry.mods]
        registry_error = None
    except (ModRegistryError, OSError) as error:
        mods = []
        registry_error = str(error)
    return {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manager_version": MANAGER_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "game": game,
        "mods": mods,
        "registry_error": registry_error,
    }


def _log_payloads(
        mod_state_root: Path, *, max_files: int, max_bytes: int,
) -> list[tuple[str, bytes]]:
    """Read newest regular log tails within explicit display/export bounds."""
    logs_root = Path(mod_state_root).expanduser().resolve() / "logs"
    if not logs_root.is_dir():
        return []
    sources = [
        source for source in logs_root.glob("*.log*")
        if source.is_file() and not source.is_symlink()
    ]
    sources.sort(
        key=lambda source: (source.stat().st_mtime_ns, source.name.casefold()),
        reverse=True,
    )
    payloads: list[tuple[str, bytes]] = []
    for source in sources[:max_files]:
        payload = source.read_bytes()
        if len(payload) > max_bytes:
            payload = payload[-max_bytes:]
        payloads.append((source.name, payload))
    return payloads


def current_diagnostics_text(
        *, game_root: Path | None, mod_state_root: Path,
) -> str:
    """Return bounded, selectable diagnostics and current mod log tails."""
    state_root = Path(mod_state_root).expanduser().resolve()
    report = _report(game_root, state_root)
    game = report["game"]
    mods = report["mods"]
    enabled = sum(1 for item in mods if item["enabled"])
    sections = [
        "CURRENT STATUS",
        "==============",
        f"Manager version: {report['manager_version']}",
        (f"Game version: {game['version']}" if game else
         "Game version: no game folder selected"),
        (f"Game folder: {game['path']}" if game else ""),
        (f"Client fingerprint: {game['client_sha256'][:12]}"
         if game and game["client_sha256"] else "Client fingerprint: unavailable"),
        f"Installed mods: {len(mods)} ({enabled} enabled, {len(mods) - enabled} disabled)",
        (f"Mod registry issue: {report['registry_error']}"
         if report["registry_error"] else "Mod registry: healthy"),
        "",
        "RECENT MOD EVENTS",
        "=================",
    ]
    sections = [line for line in sections if line != ""] + [""]
    logs = _log_payloads(
        state_root, max_files=MAX_VIEW_LOG_FILES,
        max_bytes=MAX_VIEW_LOG_BYTES)
    if not logs:
        sections.append("No mod events have been logged yet.")
    for name, payload in logs:
        events = parse_log_events(payload)[-MAX_VIEW_EVENTS_PER_FILE:]
        sections.extend((
            "",
            f"Log file: {name} ({len(payload)} bytes read)",
        ))
        if not events:
            sections.append("  No readable events were found.")
        for event in events:
            sections.append(
                f"[{event.level}] {_display_time(event.timestamp)} — {event.message}")
            if event.logger:
                sections.append(f"  Source: {event.logger}")
            if event.cause:
                sections.append(f"  Cause: {event.cause}")
    return "\n".join(sections).rstrip() + "\n"


def export_diagnostics(
        destination: Path, *, game_root: Path | None,
        mod_state_root: Path,
) -> Path:
    """Atomically publish one diagnostic ZIP with bounded log payloads."""
    output = Path(destination).expanduser().resolve()
    if output.suffix.casefold() != ".zip":
        raise DiagnosticsError("diagnostic export must use a .zip filename")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite diagnostics: {output}")
    state_root = Path(mod_state_root).expanduser().resolve()
    report_bytes = (json.dumps(
        _report(game_root, state_root), indent=2, sort_keys=True) + "\n").encode()
    logs = _log_payloads(
        state_root, max_files=MAX_LOG_FILES, max_bytes=MAX_LOG_BYTES)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.partial.zip")
    try:
        with zipfile.ZipFile(
                pending, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", report_bytes)
            for name, payload in logs:
                archive.writestr(f"logs/{name}", payload)
            inventory = {
                "report.json": hashlib.sha256(report_bytes).hexdigest().upper(),
                **{f"logs/{name}": hashlib.sha256(payload).hexdigest().upper()
                   for name, payload in logs},
            }
            archive.writestr(
                "SHA256SUMS.json",
                (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode())
        pending.replace(output)
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    return output


__all__ = (
    "DiagnosticsError", "ParsedLogEvent", "current_diagnostics_text",
    "export_diagnostics", "parse_log_events",
)
