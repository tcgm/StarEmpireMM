"""Signed HTTPS update-feed verification and atomic package download."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .mod_package import (MAX_TOTAL_SIZE, PackageError, VerifiedModPackage,
                          verify_mod_package)
from .version import MANAGER_VERSION, version_tuple


FEED_SCHEMA = 1
DYNAMIC_FEED_SCHEMA = 2
FEED_FORMAT = "star-empire-ui-mod-feed"
MAX_FEED_BYTES = 256 * 1024
MAX_RELEASES = 128
DOWNLOAD_CHUNK = 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class FeedError(RuntimeError):
    """Raised when remote update metadata or content is not trustworthy."""


@dataclass(frozen=True)
class UpdateFeed:
    channel: str
    key_id: str
    pack_id: str
    mod_version: str
    game_version: str
    manager_version_min: str
    expires_at: datetime
    package_url: str
    package_size: int
    package_sha256: str
    official_client_sha256: str = ""


@dataclass(frozen=True)
class UpdateFeedIndex:
    """Authenticated release index; selection is always by exact game build."""

    channel: str
    key_id: str
    sequence: int
    published_at: datetime
    expires_at: datetime
    manager_version_min: str
    releases: tuple[UpdateFeed, ...]
    feed_sha256: str


Opener = Callable[..., Any]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeedError(f"duplicate update-feed key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise FeedError(f"non-finite update-feed number is forbidden: {value}")


def _digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise FeedError(f"{label} must be a SHA-256 value")
    digest = value.upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise FeedError(f"{label} must be a SHA-256 value")
    return digest


def _timestamp(value: Any, label: str) -> datetime:
    if type(value) is not str:
        raise FeedError(f"{label} is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FeedError(f"{label} is invalid") from error
    if result.tzinfo is None:
        raise FeedError(f"{label} must include a timezone")
    return result.astimezone(timezone.utc)


def _required_version(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise FeedError(f"{label} is invalid")
    try:
        version_tuple(value)
    except ValueError as error:
        raise FeedError(f"{label} is invalid") from error
    return value


def _package_size(value: Any) -> int:
    if type(value) is not int or value <= 0 or value > MAX_TOTAL_SIZE:
        raise FeedError("update feed package metadata is invalid")
    return value


def _https_url(value: str, label: str) -> str:
    if type(value) is not str:
        raise FeedError(f"{label} must be a credential-free HTTPS URL")
    parsed = urlparse(value)
    if (parsed.scheme.lower() != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise FeedError(f"{label} must be a credential-free HTTPS URL")
    return parsed.geturl()


def _read_url(url: str, maximum: int, opener: Opener) -> bytes:
    request = Request(url, headers={"User-Agent": f"StarEmpireModManager/{MANAGER_VERSION}"})
    try:
        with opener(request, timeout=20) as response:
            final_url = _https_url(response.geturl(), "redirect target")
            if not final_url:
                raise FeedError("download redirect is invalid")
            payload = response.read(maximum + 1)
    except FeedError:
        raise
    except Exception as error:
        raise FeedError(f"could not download {url}") from error
    if len(payload) > maximum:
        raise FeedError("download exceeds its safety limit")
    return payload


def fetch_update_feed(url: str, trusted_keys: Mapping[str, bytes], *,
                      opener: Opener = urlopen,
                      now: datetime | None = None) -> UpdateFeed | UpdateFeedIndex:
    """Download and authenticate exact feed bytes plus ``.sig``."""
    feed_url = _https_url(url, "update feed")
    payload = _read_url(feed_url, MAX_FEED_BYTES, opener)
    signature = _read_url(feed_url + ".sig", 128, opener)
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant)
    except FeedError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FeedError("update feed is not valid UTF-8 JSON") from error
    if (not isinstance(data, dict)
            or type(data.get("schema")) is not int
            or data.get("schema") not in {FEED_SCHEMA, DYNAMIC_FEED_SCHEMA}
            or data.get("format") != FEED_FORMAT):
        raise FeedError("unsupported update feed format")
    schema = data["schema"]
    legacy_fields = {
        "schema", "format", "channel", "key_id", "pack_id",
        "mod_version", "game_version", "manager_version_min",
        "expires_at", "package_url", "package_size", "package_sha256",
    }
    index_fields = {
        "schema", "format", "channel", "key_id", "sequence",
        "published_at", "expires_at", "manager_version_min", "releases",
    }
    if set(data) != (legacy_fields if schema == FEED_SCHEMA else index_fields):
        raise FeedError("update feed has unknown or missing fields")
    if type(data["key_id"]) is not str:
        raise FeedError("update feed key ID is invalid")
    key_id = data["key_id"].strip()
    public_key = trusted_keys.get(key_id)
    if public_key is None or len(public_key) != 32:
        raise FeedError(f"update feed signing key is not trusted: {key_id or '<missing>'}")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError) as error:
        raise FeedError("update feed signature verification failed") from error
    expires = _timestamp(data["expires_at"], "update feed expiry")
    manager_min = _required_version(
        data["manager_version_min"], "update feed Manager version")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= current:
        raise FeedError("update feed has expired")
    if version_tuple(manager_min) > version_tuple(MANAGER_VERSION):
        raise FeedError("update feed requires a newer Manager")
    if type(data["channel"]) is not str:
        raise FeedError("update feed channel is missing")
    channel = data["channel"].strip()
    if not channel:
        raise FeedError("update feed channel is missing")

    if schema == FEED_SCHEMA:
        for field in ("pack_id", "mod_version", "game_version", "package_url"):
            if type(data[field]) is not str or not data[field].strip():
                raise FeedError("legacy update feed identity is incomplete")
        size = _package_size(data["package_size"])
        return UpdateFeed(
            channel, key_id, data["pack_id"].strip(),
            data["mod_version"].strip(), data["game_version"].strip(),
            manager_min, expires,
            _https_url(data["package_url"], "package URL"), size,
            _digest(data["package_sha256"], "update feed package hash"))

    sequence = data["sequence"]
    releases_data = data["releases"]
    published = _timestamp(data["published_at"], "update feed publication time")
    if (type(sequence) is not int or sequence < 0
            or published >= expires or published > current + timedelta(minutes=5)
            or not isinstance(releases_data, list)
            or not releases_data or len(releases_data) > MAX_RELEASES):
        raise FeedError("dynamic update feed metadata is invalid")
    release_fields = {
        "package_key_id", "pack_id", "mod_version", "game_version",
        "official_client_sha256", "manager_version_min", "package_url",
        "package_size", "package_sha256",
    }
    releases: list[UpdateFeed] = []
    identities: set[tuple[str, str]] = set()
    pack_ids: set[str] = set()
    for raw_release in releases_data:
        if not isinstance(raw_release, dict) or set(raw_release) != release_fields:
            raise FeedError("dynamic update release has unknown or missing fields")
        for field in ("package_key_id", "pack_id", "mod_version",
                      "game_version", "package_url"):
            if type(raw_release[field]) is not str:
                raise FeedError("dynamic update release has invalid scalar metadata")
        package_key_id = raw_release["package_key_id"].strip()
        pack_id = raw_release["pack_id"].strip()
        mod_version = raw_release["mod_version"].strip()
        game_version = raw_release["game_version"].strip()
        if not pack_id or not mod_version or not game_version:
            raise FeedError("dynamic update release identity is incomplete")
        official_digest = _digest(
            raw_release["official_client_sha256"],
            "dynamic update official client hash")
        identity = (game_version, official_digest)
        if identity in identities or pack_id in pack_ids:
            raise FeedError("dynamic update feed contains an ambiguous release")
        identities.add(identity)
        pack_ids.add(pack_id)
        release_manager_min = _required_version(
            raw_release["manager_version_min"],
            "dynamic update release Manager version")
        releases.append(UpdateFeed(
            channel, package_key_id, pack_id, mod_version, game_version,
            release_manager_min, expires,
            _https_url(raw_release["package_url"], "package URL"),
            _package_size(raw_release["package_size"]),
            _digest(raw_release["package_sha256"],
                    "dynamic update package hash"),
            official_digest,
        ))
    return UpdateFeedIndex(
        channel, key_id, sequence, published, expires, manager_min,
        tuple(releases), hashlib.sha256(payload).hexdigest().upper())


def select_update_release(
        feed: UpdateFeedIndex, game_version: str,
        official_client_sha256: str, *,
        manager_version: str = MANAGER_VERSION) -> UpdateFeed | None:
    """Select one exact official build; never substitute the newest release."""
    if not isinstance(feed, UpdateFeedIndex):
        raise FeedError("exact build selection requires a dynamic update feed")
    digest = _digest(official_client_sha256, "installed official client hash")
    try:
        running_manager = version_tuple(manager_version)
    except ValueError as error:
        raise FeedError("running Manager version is invalid") from error
    matching = tuple(
        release for release in feed.releases
        if release.game_version == str(game_version)
        and release.official_client_sha256 == digest
    )
    if not matching:
        return None
    if len(matching) != 1:
        raise FeedError("dynamic update feed contains an ambiguous exact match")
    selected = matching[0]
    if version_tuple(selected.manager_version_min) > running_manager:
        raise FeedError("matching update requires a newer Manager")
    return selected


@contextmanager
def _exclusive_sequence_state(target: Path):
    """Serialize anti-rollback state across Manager processes."""
    lock_path = target.with_name(target.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
    except OSError as error:
        raise FeedError("cannot open update-feed sequence lock") from error
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
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise FeedError(
                "another Manager is updating feed rollback state") from error
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


def accept_update_feed_sequence(feed: UpdateFeedIndex, path: Path) -> bool:
    """Persist anti-rollback evidence for one authenticated feed channel."""
    if not isinstance(feed, UpdateFeedIndex):
        raise FeedError("feed rollback protection requires a dynamic update feed")
    target = Path(path).expanduser().resolve()
    with _exclusive_sequence_state(target):
        return _accept_update_feed_sequence_locked(feed, target)


def _accept_update_feed_sequence_locked(
        feed: UpdateFeedIndex, target: Path) -> bool:
    channels: dict[str, Any] = {}
    if target.is_file():
        try:
            raw = json.loads(
                target.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FeedError("cannot read update-feed sequence state") from error
        if (not isinstance(raw, dict) or set(raw) != {"schema", "channels"}
                or type(raw["schema"]) is not int or raw["schema"] != 1
                or not isinstance(raw["channels"], dict)):
            raise FeedError("update-feed sequence state is invalid")
        channels = dict(raw["channels"])
        for channel, entry in channels.items():
            if (type(channel) is not str or not isinstance(entry, dict)
                    or set(entry) != {"key_id", "sequence", "feed_sha256"}
                    or type(entry["key_id"]) is not str
                    or type(entry["sequence"]) is not int
                    or entry["sequence"] < 0):
                raise FeedError("update-feed sequence state is invalid")
            _digest(entry["feed_sha256"], "stored update-feed hash")
    previous = channels.get(feed.channel)
    if previous is not None:
        if feed.sequence < previous["sequence"]:
            raise FeedError("signed update feed is older than the accepted sequence")
        if feed.sequence == previous["sequence"]:
            if feed.feed_sha256 != previous["feed_sha256"]:
                raise FeedError(
                    "signed update feed reuses a sequence with different bytes")
            return False
    channels[feed.channel] = {
        "key_id": feed.key_id,
        "sequence": feed.sequence,
        "feed_sha256": feed.feed_sha256,
    }
    payload = (json.dumps(
        {"schema": 1, "channels": channels},
        sort_keys=True, separators=(",", ":")) + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + ".pending")
    if pending.exists():
        raise FeedError(f"unfinished update-feed sequence state exists: {pending}")
    try:
        pending.write_text(payload, encoding="utf-8")
        pending.replace(target)
    except OSError as error:
        pending.unlink(missing_ok=True)
        raise FeedError("cannot save update-feed sequence state") from error
    return True


def download_update_package(feed: UpdateFeed, destination: Path,
                            trusted_keys: Mapping[str, bytes], *,
                            opener: Opener = urlopen) -> VerifiedModPackage:
    """Stream, hash, atomically publish, then authenticate one package."""
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != ".seuimod":
        raise FeedError("download destination must use .seuimod")
    if destination.exists():
        raise FeedError(f"refusing to overwrite existing package: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(f".{destination.stem}.partial.seuimod")
    if pending.exists():
        raise FeedError(f"unfinished package download already exists: {pending}")
    request = Request(
        feed.package_url,
        headers={"User-Agent": f"StarEmpireModManager/{MANAGER_VERSION}"})
    digest = hashlib.sha256()
    total = 0
    try:
        with opener(request, timeout=60) as response, pending.open("xb") as output:
            _https_url(response.geturl(), "package redirect target")
            while chunk := response.read(DOWNLOAD_CHUNK):
                total += len(chunk)
                if total > feed.package_size or total > MAX_TOTAL_SIZE:
                    raise FeedError("package download exceeds signed size")
                digest.update(chunk)
                output.write(chunk)
        if total != feed.package_size:
            raise FeedError("package download size does not match signed feed")
        if digest.hexdigest().upper() != feed.package_sha256:
            raise FeedError("package download hash does not match signed feed")
        package = verify_mod_package(pending, trusted_keys)
        compatibility = package.compatibility
        if (compatibility.pack_id != feed.pack_id
                or compatibility.mod_version != feed.mod_version
                or compatibility.game_version != feed.game_version
                or compatibility.key_id != feed.key_id
                or (feed.official_client_sha256
                    and compatibility.official_client_sha256
                    != feed.official_client_sha256)):
            raise FeedError("downloaded package does not match signed feed metadata")
        try:
            os.link(pending, destination)
        except FileExistsError as error:
            raise FeedError(
                f"refusing to overwrite package created concurrently: {destination}"
            ) from error
        pending.unlink(missing_ok=True)
        return (replace(package, path=destination)
                if isinstance(package, VerifiedModPackage) else package)
    except (PackageError, OSError) as error:
        raise FeedError("downloaded package failed verification") from error
    finally:
        pending.unlink(missing_ok=True)


def package_filename(feed: UpdateFeed) -> str:
    """Return a filesystem-safe local name derived from signed identifiers."""
    stem = _SAFE_NAME.sub(
        "-", f"{feed.pack_id}-{feed.mod_version}-{feed.package_sha256[:12].lower()}"
    ).strip("-.")
    return (stem or "star-empire-ui-mod") + ".seuimod"
