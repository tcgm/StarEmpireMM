"""Small fail-open boundary between Star Empire and external mods.

Only this module is called by the frozen game hook.  Individual mods remain
outside ``Client.exe`` and communicate through the versioned event API.
"""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
from typing import Any

from .runtime import ExternalModLoader


logger = logging.getLogger(__name__)
STATE_ROOT_ENV = "STAR_EMPIRE_MOD_STATE_ROOT"
STATE_DIR_NAME = "StarEmpireModManager"
_HOST_PYGAME: Any = None
LOG_FILENAME = "star-empire-mods.log"
SETTINGS_FILENAME = "manager-settings.json"
LOG_MAX_BYTES = 4 * 1024 * 1024
LOG_BACKUP_COUNT = 4


class _BoundedFileHandler(logging.FileHandler):
    """Small rotating handler that depends only on the frozen ``logging`` module."""

    def __init__(
            self, filename: Path, *, max_bytes: int, backup_count: int,
            encoding: str = "utf-8") -> None:
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        super().__init__(filename, encoding=encoding)

    def _would_exceed_limit(self, record: logging.LogRecord) -> bool:
        if self.max_bytes <= 0:
            return False
        path = Path(self.baseFilename)
        current_size = path.stat().st_size if path.is_file() else 0
        message = f"{self.format(record)}{self.terminator}"
        return current_size + len(message.encode("utf-8")) > self.max_bytes

    def _rollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        path = Path(self.baseFilename)
        if self.backup_count <= 0:
            path.write_bytes(b"")
        else:
            for index in range(self.backup_count - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                if source.is_file():
                    os.replace(
                        source, path.with_name(f"{path.name}.{index + 1}"))
            if path.is_file():
                os.replace(path, path.with_name(f"{path.name}.1"))
        self.stream = self._open()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._would_exceed_limit(record):
                self._rollover()
            super().emit(record)
        except Exception:
            self.handleError(record)


class ModLoaderInstallError(RuntimeError):
    """Raised when the one-time client bridge cannot install atomically."""


def default_registry_path() -> Path:
    """Return the external registry shared with the Mod Manager."""
    override = os.environ.get(STATE_ROOT_ENV, "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = (Path(local) if local else
                Path.home() / "AppData" / "Local") / STATE_DIR_NAME
    return root.resolve() / "mods.json"


def configure_mod_logging(state_root: Path) -> Path:
    """Attach one bounded UTF-8 log file to loader and mod namespaces."""
    root = Path(state_root).expanduser().resolve()
    debug_logging = False
    settings_path = root / SETTINGS_FILENAME
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            valid_schema = (
                type(settings) is dict
                and (
                    (settings.get("schema") == 1
                     and set(settings) == {"schema", "debug_logging"})
                    or (settings.get("schema") == 2
                        and set(settings) == {
                            "schema", "debug_logging", "dark_mode"})))
            debug_logging = bool(
                valid_schema and settings.get("debug_logging") is True)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                "STAR_EMPIRE_MOD_SETTINGS_INVALID; using normal logging")
    level = logging.DEBUG if debug_logging else logging.INFO
    log_path = root / "logs" / LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    namespaces = (__package__.split(".", 1)[0], "star_empire_mod")
    handler = next((
        existing
        for namespace in namespaces
        for existing in logging.getLogger(namespace).handlers
        if getattr(existing, "_star_empire_mod_log", None) == log_path), None)
    if handler is None:
        handler = _BoundedFileHandler(
            log_path, max_bytes=LOG_MAX_BYTES,
            backup_count=LOG_BACKUP_COUNT, encoding="utf-8")
        handler._star_empire_mod_log = log_path
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
    for namespace in namespaces:
        target = logging.getLogger(namespace)
        if handler not in target.handlers:
            target.addHandler(handler)
        target.setLevel(level)
    return log_path


def initialize_mod_loader(
        host: Any, pygame: Any, screen: Any, *,
        registry_path: Path | None = None,
        loader_factory: Any = ExternalModLoader,
) -> bool:
    """Load enabled mods once; registry failure always leaves vanilla usable."""
    existing = getattr(host, "_se_mod_loader", None)
    if existing is not None:
        return True
    try:
        selected_registry = Path(
            registry_path or default_registry_path()).expanduser().resolve()
        try:
            host._se_mod_log_path = configure_mod_logging(
                selected_registry.parent)
        except Exception:
            logger.exception(
                "STAR_EMPIRE_MOD_LOG_SETUP_FAILED; continuing without file log")
            host._se_mod_log_path = None
        loader = loader_factory(selected_registry)
        loader.load_enabled()
        loader.emit(
            "client.startup", host=host, pygame=pygame, screen=screen)
    except Exception:
        host._se_mod_loader = None
        if not getattr(host, "_se_mod_loader_startup_failed", False):
            logger.exception(
                "STAR_EMPIRE_MOD_LOADER_STARTUP_FAILED; continuing without mods")
        host._se_mod_loader_startup_failed = True
        return False
    host._se_mod_loader = loader
    host._se_mod_loader_startup_failed = False
    host._se_mod_loader_shutdown = False
    return True


def shutdown_mod_loader(host: Any) -> None:
    """Shut down external mods at most once without affecting game shutdown."""
    loader = getattr(host, "_se_mod_loader", None)
    if loader is None or getattr(host, "_se_mod_loader_shutdown", False):
        return
    host._se_mod_loader_shutdown = True
    try:
        loader.shutdown()
    except Exception:
        logger.exception("STAR_EMPIRE_MOD_LOADER_SHUTDOWN_FAILED")


def handle_mod_loader_event(
        host: Any, pygame: Any, event: Any, screen: Any,
) -> bool:
    """Offer one safe input event to mods and report explicit consumption."""
    loader = getattr(host, "_se_mod_loader", None)
    if loader is None:
        return False
    event_type = getattr(event, "type", None)
    if event_type == getattr(pygame, "QUIT", None):
        shutdown_mod_loader(host)
        return False
    try:
        results = loader.emit(
            "client.event", host=host, pygame=pygame,
            event=event, screen=screen)
    except Exception:
        logger.exception("STAR_EMPIRE_MOD_LOADER_EVENT_FAILED")
        return False
    # Escape remains game-owned even if a mod mistakenly returns True.
    if (event_type in (getattr(pygame, "KEYDOWN", None),
                       getattr(pygame, "KEYUP", None))
            and getattr(event, "key", None)
            == getattr(pygame, "K_ESCAPE", None)):
        return False
    return any(result is True for result in results)


def begin_mod_loader_frame(host: Any, render_target: Any) -> None:
    loader = getattr(host, "_se_mod_loader", None)
    if loader is None:
        return
    try:
        loader.emit(
            "client.frame.begin", host=host, render_target=render_target)
    except Exception:
        logger.exception("STAR_EMPIRE_MOD_LOADER_BEGIN_FRAME_FAILED")


def draw_mod_loader_overlay(
        host: Any, pygame: Any, screen: Any, render_target: Any,
) -> None:
    loader = getattr(host, "_se_mod_loader", None)
    if loader is None:
        return
    try:
        loader.emit(
            "client.draw", host=host, pygame=pygame,
            screen=screen, render_target=render_target)
    except Exception:
        logger.exception("STAR_EMPIRE_MOD_LOADER_DRAW_FAILED")


def draw_mod_loader_region(
        host: Any, pygame: Any, screen: Any, region: str, rect: Any,
) -> int:
    """Draw one inline extension region and return reserved layout height."""
    loader = getattr(host, "_se_mod_loader", None)
    if loader is None:
        return 0
    try:
        results = loader.emit(
            "client.region.draw", host=host, pygame=pygame,
            screen=screen, region=region, rect=rect)
    except Exception:
        logger.exception("STAR_EMPIRE_MOD_LOADER_REGION_DRAW_FAILED")
        return 0
    try:
        available = max(0, int(rect.height))
    except (AttributeError, TypeError, ValueError):
        return 0
    requested = max(
        (result for result in results
         if type(result) is int and result > 0),
        default=0,
    )
    return min(requested, available)


class ModLoaderClientBridge:
    """Methods rebound into ``SolarSystemWindow`` by a tiny host fragment."""

    def _initialize_star_empire_mod_loader(self, screen: Any) -> bool:
        return initialize_mod_loader(self, _HOST_PYGAME, screen)

    def _handle_star_empire_mod_event(
            self, event: Any, screen: Any) -> bool:
        return handle_mod_loader_event(self, _HOST_PYGAME, event, screen)

    def _begin_star_empire_mod_frame(self, render_target: Any) -> None:
        begin_mod_loader_frame(self, render_target)

    def _draw_star_empire_mod_overlay(
            self, screen: Any, render_target: Any) -> None:
        draw_mod_loader_overlay(
            self, _HOST_PYGAME, screen, render_target)

    def _draw_star_empire_mod_region(
            self, region: str, screen: Any, rect: Any) -> int:
        return draw_mod_loader_region(
            self, _HOST_PYGAME, screen, region, rect)

    def _shutdown_star_empire_mod_loader(self) -> None:
        shutdown_mod_loader(self)


def install_mod_loader_bridge(
        client_type: type, pygame_module: Any,
) -> tuple[str, ...]:
    """Install the complete loader boundary or leave the client untouched."""
    global _HOST_PYGAME
    names = (
        "_initialize_star_empire_mod_loader",
        "_handle_star_empire_mod_event",
        "_begin_star_empire_mod_frame",
        "_draw_star_empire_mod_overlay",
        "_draw_star_empire_mod_region",
        "_shutdown_star_empire_mod_loader",
    )
    collision = next(
        (name for name in names if hasattr(client_type, name)), None)
    if collision is not None:
        raise ModLoaderInstallError(
            f"client loader method already exists: {collision}")
    if pygame_module is None:
        raise ModLoaderInstallError("pygame host module is unavailable")
    installed: list[str] = []
    previous_pygame = _HOST_PYGAME
    try:
        _HOST_PYGAME = pygame_module
        for name in names:
            setattr(client_type, name, getattr(ModLoaderClientBridge, name))
            installed.append(name)
    except Exception:
        for name in reversed(installed):
            try:
                delattr(client_type, name)
            except AttributeError:
                pass
        _HOST_PYGAME = previous_pygame
        raise
    return names


__all__ = (
    "ModLoaderClientBridge",
    "ModLoaderInstallError",
    "STATE_DIR_NAME",
    "STATE_ROOT_ENV",
    "SETTINGS_FILENAME",
    "begin_mod_loader_frame",
    "configure_mod_logging",
    "default_registry_path",
    "draw_mod_loader_overlay",
    "draw_mod_loader_region",
    "handle_mod_loader_event",
    "initialize_mod_loader",
    "install_mod_loader_bridge",
    "shutdown_mod_loader",
)
