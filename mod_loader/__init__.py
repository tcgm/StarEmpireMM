"""Stable client-side API exposed by the Star Empire Mod Loader."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Callable


LOADER_API_VERSION = 2
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9.-]{2,63}$")


class ModApiError(ValueError):
    """Raised when a mod attempts an invalid loader registration."""


@dataclass(frozen=True)
class LoaderDiagnostic:
    mod_id: str
    phase: str
    message: str


@dataclass(frozen=True)
class _Hook:
    mod_id: str
    event: str
    callback: Callable[..., Any]
    priority: int
    sequence: int


class ModEventBus:
    """Deterministic callback bus that isolates failures per mod."""

    def __init__(self) -> None:
        self._hooks: list[_Hook] = []
        self._sequence = 0
        self._diagnostics: list[LoaderDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[LoaderDiagnostic, ...]:
        return tuple(self._diagnostics)

    def add_diagnostic(self, mod_id: str, phase: str, message: str) -> None:
        self._diagnostics.append(LoaderDiagnostic(
            str(mod_id), str(phase), str(message)))

    def register(
            self, mod_id: str, event: str, callback: Callable[..., Any],
            *, priority: int = 0) -> Callable[[], None]:
        if not _EVENT_NAME.fullmatch(str(event)):
            raise ModApiError(f"invalid mod event name: {event}")
        if not callable(callback):
            raise ModApiError("mod event callback must be callable")
        if type(priority) is not int or not -1000 <= priority <= 1000:
            raise ModApiError("mod event priority must be between -1000 and 1000")
        hook = _Hook(str(mod_id), str(event), callback, priority, self._sequence)
        self._sequence += 1
        self._hooks.append(hook)

        def unsubscribe() -> None:
            try:
                self._hooks.remove(hook)
            except ValueError:
                pass

        return unsubscribe

    def remove_mod(self, mod_id: str) -> None:
        self._hooks = [hook for hook in self._hooks if hook.mod_id != mod_id]

    def emit(self, event_name: str, *args, **kwargs) -> tuple[Any, ...]:
        if not _EVENT_NAME.fullmatch(str(event_name)):
            raise ModApiError(f"invalid mod event name: {event_name}")
        hooks = sorted(
            (hook for hook in tuple(self._hooks)
             if hook.event == event_name),
            key=lambda hook: (-hook.priority, hook.sequence))
        results: list[Any] = []
        for hook in hooks:
            try:
                results.append(hook.callback(*args, **kwargs))
            except Exception as error:
                logging.getLogger(f"star_empire_mod.{hook.mod_id}").exception(
                    "MOD_CALLBACK_FAILED event=%s", event_name)
                self.add_diagnostic(
                    hook.mod_id, f"event:{event_name}", str(error))
        return tuple(results)


class ScopedModApi:
    """API view bound to one authenticated installed mod identity."""

    def __init__(
            self, bus: ModEventBus, mod_id: str, version: str,
            install_path: Path, config_root: Path) -> None:
        self._bus = bus
        self.mod_id = str(mod_id)
        self.version = str(version)
        self.install_path = Path(install_path).resolve()
        self._config_root = Path(config_root).resolve()
        self.logger = logging.getLogger(f"star_empire_mod.{self.mod_id}")

    @property
    def loader_api_version(self) -> int:
        return LOADER_API_VERSION

    @property
    def settings_path(self) -> Path:
        return self._config_root / self.mod_id / "settings.json"

    def on(
            self, event: str, callback: Callable[..., Any], *,
            priority: int = 0) -> Callable[[], None]:
        return self._bus.register(
            self.mod_id, event, callback, priority=priority)

    def diagnostic(self, phase: str, message: str) -> None:
        self._bus.add_diagnostic(self.mod_id, phase, message)


__all__ = [
    "LOADER_API_VERSION", "LoaderDiagnostic", "ModApiError",
    "ModEventBus", "ScopedModApi",
]
