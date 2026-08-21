"""Public Ed25519 trust configuration for signed ``.seuimod`` packages."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .manager_core import ManagerDataError, default_state_path


# Public keys are not secrets; private signing keys must never enter this repo.
# This key signs only the deliberately narrow 0.4.42 Target public alpha.
BUILTIN_TRUSTED_KEYS: Mapping[str, bytes] = MappingProxyType({
    "star-empire-ui-alpha-2026-08": bytes.fromhex(
        "42F13FDF90D978F7FC45FE7BA90742AD"
        "0F8B966682CB0B049F5B04709D0AA80F"
    ),
})

DEFAULT_UPDATE_FEED_URL = (
    "https://raw.githubusercontent.com/dezgard/StarEmpireUIMOD/"
    "master/releases/alpha/feed-v2.json"
)


def trusted_keys_path() -> Path:
    return default_state_path().parent / "trusted-public-keys.json"


@dataclass(frozen=True)
class TrustConfiguration:
    keys: Mapping[str, bytes]
    update_feed_url: str = DEFAULT_UPDATE_FEED_URL


def load_trust_configuration(path: Path | None = None) -> TrustConfiguration:
    """Load strict public keys and an optional signed-feed HTTPS location."""
    result = dict(BUILTIN_TRUSTED_KEYS)
    source = Path(path) if path is not None else trusted_keys_path()
    if not source.is_file():
        return TrustConfiguration(MappingProxyType(result))
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManagerDataError(f"cannot read trusted public keys: {source}") from error
    if (not isinstance(data, dict)
            or not {"schema", "keys"}.issubset(data)
            or not set(data).issubset({"schema", "keys", "update_feed_url"})):
        raise ManagerDataError("trusted public keys file has invalid fields")
    if data["schema"] != 1 or not isinstance(data["keys"], dict):
        raise ManagerDataError("unsupported trusted public keys format")
    for raw_id, encoded in data["keys"].items():
        key_id = str(raw_id).strip()
        if (not key_id or key_id in result or not isinstance(encoded, str)):
            raise ManagerDataError("trusted public key ID is invalid or duplicated")
        try:
            public_key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ManagerDataError(f"trusted public key is not base64: {key_id}") from error
        if len(public_key) != 32:
            raise ManagerDataError(f"trusted Ed25519 public key has invalid length: {key_id}")
        result[key_id] = public_key
    feed_url = str(data.get(
        "update_feed_url", DEFAULT_UPDATE_FEED_URL)).strip()
    return TrustConfiguration(MappingProxyType(result), feed_url)


def load_trusted_keys(path: Path | None = None) -> Mapping[str, bytes]:
    """Compatibility helper returning only the configured public keys."""
    return load_trust_configuration(path).keys
