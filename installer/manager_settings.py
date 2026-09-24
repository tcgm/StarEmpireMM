"""Small atomic settings store shared by the Manager and game loader."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid


SETTINGS_FILENAME = "manager-settings.json"


class ManagerSettingsError(ValueError):
    """Raised when Manager settings are malformed or cannot be saved."""


@dataclass(frozen=True)
class ManagerSettings:
    debug_logging: bool = False
    dark_mode: bool = False

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": 2,
            "debug_logging": self.debug_logging,
            "dark_mode": self.dark_mode,
        }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManagerSettingsError(f"duplicate Manager setting: {key}")
        result[key] = value
    return result


def load_manager_settings(path: Path) -> ManagerSettings:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        return ManagerSettings()
    if not source.is_file():
        raise ManagerSettingsError(f"Manager settings path is not a file: {source}")
    try:
        raw = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object)
    except ManagerSettingsError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManagerSettingsError("Manager settings are not valid UTF-8 JSON") from error
    if type(raw) is not dict or type(raw.get("schema")) is not int:
        raise ManagerSettingsError("Manager settings have an unsupported schema")
    schema = raw["schema"]
    if (schema == 1
            and set(raw) == {"schema", "debug_logging"}
            and type(raw["debug_logging"]) is bool):
        return ManagerSettings(debug_logging=raw["debug_logging"])
    if (schema == 2
            and set(raw) == {"schema", "debug_logging", "dark_mode"}
            and type(raw["debug_logging"]) is bool
            and type(raw["dark_mode"]) is bool):
        return ManagerSettings(
            debug_logging=raw["debug_logging"], dark_mode=raw["dark_mode"])
    raise ManagerSettingsError("Manager settings have an unsupported schema")


def save_manager_settings(path: Path, settings: ManagerSettings) -> Path:
    if not isinstance(settings, ManagerSettings):
        raise ManagerSettingsError("settings must be a ManagerSettings value")
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    payload = (json.dumps(
        settings.to_mapping(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with pending.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(target)
    except Exception as error:
        pending.unlink(missing_ok=True)
        if isinstance(error, ManagerSettingsError):
            raise
        raise ManagerSettingsError("could not save Manager settings") from error
    return target


__all__ = (
    "ManagerSettings", "ManagerSettingsError", "SETTINGS_FILENAME",
    "load_manager_settings", "save_manager_settings",
)
