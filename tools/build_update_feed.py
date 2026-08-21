"""Build a deterministic signed schema-2 update index from verified packages."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping, Sequence
from urllib.parse import urlparse
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from installer.mod_package import PackageError, VerifiedModPackage, verify_mod_package
from installer.trusted_keys import BUILTIN_TRUSTED_KEYS
from installer.update_feed import (FeedError, UpdateFeed, UpdateFeedIndex,
                                   fetch_update_feed)
from installer.version import MANAGER_VERSION
from tools.build_mod_package import (
    PackageBuildError,
    _load_private_key,
    _trusted_signer,
)


class FeedBuildError(RuntimeError):
    """Raised before incomplete or unverified feed artifacts are published."""


GENESIS_SEQUENCE = 1
HISTORY_FORMAT = "star-empire-ui-feed-publisher-history"
HISTORY_FILE = "publisher-history.json"
HISTORY_SIGNATURE_FILE = "publisher-history.json.sig"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _digest(value: object, label: str) -> str:
    if type(value) is not str:
        raise FeedBuildError(f"{label} must be a SHA-256 value")
    digest = value.upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise FeedBuildError(f"{label} must be a SHA-256 value")
    return digest


def _https_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme.lower() != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise FeedBuildError("package URL must be credential-free HTTPS")
    return parsed.geturl()


def _package_key_id(payload: bytes, label: Path) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            raw = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError,
            zipfile.BadZipFile) as error:
        raise FeedBuildError(f"cannot inspect package manifest: {label}") from error
    if not isinstance(raw, dict) or type(raw.get("key_id")) is not str:
        raise FeedBuildError(f"package signing key ID is invalid: {label}")
    key_id = raw["key_id"].strip()
    if not key_id:
        raise FeedBuildError(f"package signing key ID is missing: {label}")
    return key_id


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str):
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FeedBuildError(f"duplicate prior-feed key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise FeedBuildError(f"non-finite prior-feed number is forbidden: {value}")


def _prior_verification_time(payload: bytes) -> datetime:
    """Read only the clock needed to authenticate an expired historical feed."""
    try:
        raw = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant)
        value = raw["published_at"]
        if type(value) is not str:
            raise (ValueError("published_at is not text"))
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("published_at has no timezone")
        return published.astimezone(timezone.utc)
    except FeedBuildError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError,
            json.JSONDecodeError) as error:
        raise FeedBuildError("prior feed publication time is invalid") from error


def _load_prior_feed(
        directory: Path, feed_key_id: str,
        public_key: bytes) -> tuple[
            UpdateFeedIndex, dict[str, tuple[str, str]]]:
    """Authenticate a previous builder output without trusting local metadata."""
    try:
        root = Path(directory).expanduser().resolve(strict=True)
    except OSError as error:
        raise FeedBuildError("prior signed feed folder does not exist") from error
    if not root.is_dir():
        raise FeedBuildError("prior signed feed must be a folder")
    try:
        payload = (root / "feed-v2.json").read_bytes()
        signature = (root / "feed-v2.json.sig").read_bytes()
    except OSError as error:
        raise FeedBuildError("cannot read prior signed feed artifacts") from error
    verification_time = _prior_verification_time(payload)

    def opener(request, timeout):
        return _Response(
            signature if request.full_url.endswith(".sig") else payload,
            request.full_url)

    try:
        verified = fetch_update_feed(
            "https://local.invalid/prior/feed-v2.json",
            {feed_key_id: public_key}, opener=opener, now=verification_time)
    except FeedError as error:
        raise FeedBuildError("prior signed feed failed authentication") from error
    if not isinstance(verified, UpdateFeedIndex):
        raise FeedBuildError("prior signed feed is not a schema-2 index")
    try:
        history_payload = (root / HISTORY_FILE).read_bytes()
        history_signature = (root / HISTORY_SIGNATURE_FILE).read_bytes()
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            history_signature, history_payload)
    except (OSError, InvalidSignature, ValueError) as error:
        raise FeedBuildError(
            "prior publisher history failed authentication") from error
    try:
        history = json.loads(
            history_payload.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FeedBuildError("prior publisher history is invalid") from error
    expected_fields = {
        "schema", "format", "key_id", "channel", "sequence",
        "pack_identities",
    }
    if (not isinstance(history, dict) or set(history) != expected_fields
            or history["schema"] != 1 or history["format"] != HISTORY_FORMAT
            or history["key_id"] != feed_key_id
            or history["channel"] != verified.channel
            or history["sequence"] != verified.sequence
            or not isinstance(history["pack_identities"], dict)):
        raise FeedBuildError("prior publisher history is invalid")
    identities: dict[str, tuple[str, str]] = {}
    for pack_id, raw_identity in history["pack_identities"].items():
        if (type(pack_id) is not str or not pack_id.strip()
                or not isinstance(raw_identity, dict)
                or set(raw_identity) != {
                    "game_version", "official_client_sha256"}
                or type(raw_identity["game_version"]) is not str
                or not raw_identity["game_version"].strip()):
            raise FeedBuildError("prior publisher history is invalid")
        identities[pack_id] = (
            raw_identity["game_version"].strip(),
            _digest(
                raw_identity["official_client_sha256"],
                "prior publisher-history client"),
        )
    for release in verified.releases:
        if identities.get(release.pack_id) != (
                release.game_version, release.official_client_sha256):
            raise FeedBuildError(
                "prior feed release is absent from publisher history")
    return verified, identities


def _release_row(release: UpdateFeed) -> dict[str, object]:
    return {
        "package_key_id": release.key_id,
        "pack_id": release.pack_id,
        "mod_version": release.mod_version,
        "game_version": release.game_version,
        "official_client_sha256": release.official_client_sha256,
        "manager_version_min": release.manager_version_min,
        "package_url": release.package_url,
        "package_size": release.package_size,
        "package_sha256": release.package_sha256,
    }


def build_update_feed(
        releases: Sequence[tuple[Path, str]], *,
        private_key: Ed25519PrivateKey, feed_key_id: str,
        channel: str, sequence: int, published_at: datetime,
        expires_at: datetime, output_dir: Path,
        trusted_feed_keys: Mapping[str, bytes],
        trusted_package_keys: Mapping[str, bytes],
        genesis: bool = False,
        prior_feed_dir: Path | None = None) -> Path:
    """Verify packages, sign one exact-build index, and publish a new folder."""
    if not releases or type(sequence) is not int or sequence < GENESIS_SEQUENCE:
        raise FeedBuildError("feed requires releases and a positive sequence")
    if not feed_key_id.strip() or not channel.strip():
        raise FeedBuildError("feed key ID and channel are required")
    if (published_at.tzinfo is None or expires_at.tzinfo is None
            or published_at >= expires_at):
        raise FeedBuildError("feed timestamps must be ordered and timezone-aware")
    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite feed folder: {output}")

    if genesis:
        if sequence != GENESIS_SEQUENCE or prior_feed_dir is not None:
            raise FeedBuildError(
                "genesis must explicitly create sequence 1 without a prior feed")
    elif sequence == GENESIS_SEQUENCE or prior_feed_dir is None:
        raise FeedBuildError(
            "non-genesis publication requires a prior signed feed")

    channel_name = channel.strip()
    feed_key_name = feed_key_id.strip()
    try:
        public_key = _trusted_signer(
            private_key=private_key, key_id=feed_key_name,
            trusted_keys=trusted_feed_keys)
    except PackageBuildError as error:
        raise FeedBuildError(f"feed signing key is not deployed: {error}") from error
    prior: UpdateFeedIndex | None = None
    historical_by_pack: dict[str, tuple[str, str]] = {}
    if prior_feed_dir is not None:
        prior, historical_by_pack = _load_prior_feed(
            prior_feed_dir, feed_key_name, public_key)
        if prior.channel != channel_name:
            raise FeedBuildError("prior signed feed channel does not match")
        if sequence != prior.sequence + 1:
            raise FeedBuildError(
                "feed sequence must be exactly one greater than the prior feed")
        if published_at.astimezone(timezone.utc) <= prior.published_at:
            raise FeedBuildError("feed publication time must advance")

    trusted = dict(trusted_package_keys)
    if not trusted:
        raise FeedBuildError("at least one deployed package-signing key is required")
    release_rows = [_release_row(release) for release in prior.releases] if prior else []
    row_indexes = {
        (str(row["game_version"]), str(row["official_client_sha256"])): index
        for index, row in enumerate(release_rows)
    }
    new_identities: set[tuple[str, str]] = set()
    new_pack_ids: set[str] = set()
    snapshots: dict[Path, tuple[bytes, Path]] = {}
    with tempfile.TemporaryDirectory(prefix="star-empire-feed-packages-") as temporary:
        snapshot_root = Path(temporary)
        for raw_path, raw_url in releases:
            try:
                package_path = Path(raw_path).expanduser().resolve(strict=True)
            except OSError as error:
                raise FeedBuildError(f"release package does not exist: {raw_path}") from error
            snapshot = snapshots.get(package_path)
            if snapshot is None:
                try:
                    package_bytes = package_path.read_bytes()
                    snapshot_path = snapshot_root / f"{len(snapshots):04d}.seuimod"
                    snapshot_path.write_bytes(package_bytes)
                except OSError as error:
                    raise FeedBuildError(
                        f"cannot snapshot release package: {package_path}") from error
                snapshot = (package_bytes, snapshot_path)
                snapshots[package_path] = snapshot
            package_bytes, snapshot_path = snapshot
            key_id = _package_key_id(package_bytes, package_path)
            try:
                package = verify_mod_package(snapshot_path, trusted)
            except PackageError as error:
                raise FeedBuildError(
                    f"release package failed authentication: {package_path}") from error
            compatibility = package.compatibility
            identity = (
                compatibility.game_version,
                compatibility.official_client_sha256,
            )
            if identity in new_identities or compatibility.pack_id in new_pack_ids:
                raise FeedBuildError("release set contains a duplicate build or pack ID")
            prior_identity = historical_by_pack.get(compatibility.pack_id)
            if prior_identity is not None and prior_identity != identity:
                raise FeedBuildError(
                    "historical pack ID cannot be assigned to a different exact build")
            historical_by_pack[compatibility.pack_id] = identity
            new_identities.add(identity)
            new_pack_ids.add(compatibility.pack_id)
            row = {
                "package_key_id": key_id,
                "pack_id": compatibility.pack_id,
                "mod_version": compatibility.mod_version,
                "game_version": compatibility.game_version,
                "official_client_sha256": compatibility.official_client_sha256,
                "manager_version_min": str(package.manifest["manager_version_min"]),
                "package_url": _https_url(raw_url),
                "package_size": len(package_bytes),
                "package_sha256": _sha(package_bytes),
            }
            existing_index = row_indexes.get(identity)
            if existing_index is None:
                row_indexes[identity] = len(release_rows)
                release_rows.append(row)
            else:
                release_rows[existing_index] = row

    manifest = {
        "schema": 2,
        "format": "star-empire-ui-mod-feed",
        "channel": channel_name,
        "key_id": feed_key_name,
        "sequence": sequence,
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "manager_version_min": MANAGER_VERSION,
        "releases": sorted(
            release_rows,
            key=lambda row: (str(row["game_version"]),
                             str(row["official_client_sha256"])),
        ),
    }
    payload = (json.dumps(
        manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    signature = private_key.sign(payload)
    history_manifest = {
        "schema": 1,
        "format": HISTORY_FORMAT,
        "key_id": feed_key_name,
        "channel": channel_name,
        "sequence": sequence,
        "pack_identities": {
            pack_id: {
                "game_version": identity[0],
                "official_client_sha256": identity[1],
            }
            for pack_id, identity in sorted(historical_by_pack.items())
        },
    }
    history_payload = (json.dumps(
        history_manifest, sort_keys=True, separators=(",", ":"))
        + "\n").encode("utf-8")
    history_signature = private_key.sign(history_payload)

    def opener(request, timeout):
        return _Response(
            signature if request.full_url.endswith(".sig") else payload,
            request.full_url)

    try:
        verified = fetch_update_feed(
            "https://local.invalid/feed-v2.json",
            {feed_key_name: public_key}, opener=opener,
            now=published_at.astimezone(timezone.utc))
    except FeedError as error:
        raise FeedBuildError("generated feed failed self-verification") from error
    if (not isinstance(verified, UpdateFeedIndex)
            or len(verified.releases) != len(release_rows)
            or verified.sequence != sequence):
        raise FeedBuildError("self-verification returned unexpected feed metadata")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent))
    try:
        (temporary / "feed-v2.json").write_bytes(payload)
        (temporary / "feed-v2.json.sig").write_bytes(signature)
        (temporary / HISTORY_FILE).write_bytes(history_payload)
        (temporary / HISTORY_SIGNATURE_FILE).write_bytes(history_signature)
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def _release_argument(value: str) -> tuple[Path, str]:
    path, separator, url = value.partition("=")
    if not separator or not path or not url:
        raise argparse.ArgumentTypeError("release must be PACKAGE.seuimod=HTTPS_URL")
    return Path(path), url


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="append", type=_release_argument,
                        required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--feed-key-id", required=True)
    parser.add_argument("--channel", default="alpha")
    parser.add_argument("--sequence", type=int, required=True)
    history = parser.add_mutually_exclusive_group(required=True)
    history.add_argument("--genesis", action="store_true",
                         help="explicitly create sequence 1")
    history.add_argument("--prior-feed-dir", type=Path,
                         help="folder containing the authenticated previous feed")
    parser.add_argument("--valid-days", type=int, default=14)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if arguments.valid_days <= 0:
        raise FeedBuildError("valid-days must be positive")
    published = datetime.now(timezone.utc)
    output = build_update_feed(
        arguments.release,
        private_key=_load_private_key(arguments.private_key),
        feed_key_id=arguments.feed_key_id,
        channel=arguments.channel,
        sequence=arguments.sequence,
        published_at=published,
        expires_at=published + timedelta(days=arguments.valid_days),
        output_dir=arguments.output_dir,
        trusted_feed_keys=BUILTIN_TRUSTED_KEYS,
        trusted_package_keys=BUILTIN_TRUSTED_KEYS,
        genesis=arguments.genesis,
        prior_feed_dir=arguments.prior_feed_dir,
    )
    print(f"Built and verified signed update feed: {output}")


if __name__ == "__main__":
    main()
