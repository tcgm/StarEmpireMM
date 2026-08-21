"""Per-mod GitHub release discovery and verified ``.semod`` download."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from .mod_manifest import GithubUpdateSource, version_core
from .mod_registry import InstalledMod
from .semod_package import (
    MAX_TOTAL_SIZE, SemodPackageError, VerifiedSemodPackage,
    verify_semod_package,
)


GITHUB_API = "https://api.github.com"


class ModUpdateError(RuntimeError):
    """Raised when a mod update cannot be discovered or verified."""


@dataclass(frozen=True)
class GithubReleaseAsset:
    repository: str
    tag: str
    name: str
    download_url: str


@dataclass(frozen=True)
class AcquiredModUpdate:
    package: VerifiedSemodPackage
    path: Path
    repository: str
    previous_package_sha256: str


def _read_bounded(response: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ModUpdateError("download exceeds the permitted size")
        chunks.append(chunk)


def _open_https(opener: Callable, request: Request, *, timeout: int):
    response = opener(request, timeout=timeout)
    final_url = str(getattr(response, "geturl", lambda: request.full_url)())
    if urlparse(final_url).scheme.casefold() != "https":
        try:
            response.close()
        finally:
            raise ModUpdateError("GitHub redirected to a non-HTTPS URL")
    return response


def discover_github_release(
        source: GithubUpdateSource, *, opener: Callable = urlopen,
        timeout: int = 20) -> GithubReleaseAsset:
    """Resolve exactly one configured asset from GitHub's latest release."""
    if not isinstance(source, GithubUpdateSource):
        raise ModUpdateError("mod does not declare a GitHub update source")
    api_url = f"{GITHUB_API}/repos/{source.repository}/releases/latest"
    request = Request(api_url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "StarEmpireModManager/1",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with _open_https(opener, request, timeout=timeout) as response:
            payload = _read_bounded(response, 4 * 1024 * 1024)
        data = json.loads(payload.decode("utf-8"))
    except ModUpdateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModUpdateError(
            f"could not read latest release for {source.repository}") from error
    if type(data) is not dict or type(data.get("assets")) is not list:
        raise ModUpdateError("GitHub release response is malformed")
    tag = data.get("tag_name")
    if type(tag) is not str or not tag.strip():
        raise ModUpdateError("GitHub release does not have a valid tag")
    matches = []
    for raw in data["assets"]:
        if type(raw) is not dict:
            continue
        name = raw.get("name")
        download_url = raw.get("browser_download_url")
        if (type(name) is str and type(download_url) is str
                and fnmatch.fnmatchcase(name, source.asset)):
            parsed = urlparse(download_url)
            expected_prefix = f"/{source.repository}/releases/download/"
            if (parsed.scheme.casefold() != "https"
                    or parsed.hostname != "github.com"
                    or not parsed.path.startswith(expected_prefix)):
                raise ModUpdateError("GitHub release asset URL is not trusted")
            matches.append((name, download_url))
    if len(matches) != 1:
        raise ModUpdateError(
            f"expected exactly one release asset matching {source.asset}; "
            f"found {len(matches)}")
    return GithubReleaseAsset(
        source.repository, tag.strip(), matches[0][0], matches[0][1])


def _semver_key(value: str):
    core = version_core(value)
    if "-" not in value:
        return core, 1, ()
    identifiers = []
    for item in value.split("-", 1)[1].split("."):
        identifiers.append((0, int(item)) if item.isdigit() else (1, item))
    return core, 0, tuple(identifiers)


def acquire_github_update(
        installed: InstalledMod, downloads_root: Path, *, opener: Callable = urlopen,
        timeout: int = 60) -> AcquiredModUpdate | None:
    """Download, verify and retain a newer package for one mod."""
    source = installed.manifest.update
    if source is None:
        return None
    release = discover_github_release(
        source, opener=opener, timeout=min(timeout, 20))
    root = Path(downloads_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    pending = root / f".{installed.manifest.mod_id}.{uuid.uuid4().hex}.partial.semod"
    request = Request(release.download_url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "StarEmpireModManager/1",
    })
    try:
        try:
            with _open_https(opener, request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as error:
                        raise ModUpdateError(
                            "release asset Content-Length is invalid") from error
                    if declared < 1 or declared > MAX_TOTAL_SIZE:
                        raise ModUpdateError("release asset size is invalid")
                payload = _read_bounded(response, MAX_TOTAL_SIZE)
        except ModUpdateError:
            raise
        except OSError as error:
            raise ModUpdateError("could not download the mod release asset") from error
        if not payload:
            raise ModUpdateError("release asset is empty")
        with pending.open("xb") as stream:
            stream.write(payload)
        package = verify_semod_package(pending)
        if package.manifest.mod_id != installed.manifest.mod_id:
            raise ModUpdateError("release asset belongs to a different mod")
        if _semver_key(package.manifest.version) <= _semver_key(
                installed.manifest.version):
            return None
        digest = hashlib.sha256(payload).hexdigest().upper()
        if digest != package.package_sha256:
            raise ModUpdateError("downloaded package hash changed after verification")
        destination = root / (
            f"{package.manifest.mod_id}-{package.manifest.version}-"
            f"{digest[:12].lower()}.semod")
        if destination.exists():
            if (not destination.is_file()
                    or hashlib.sha256(destination.read_bytes()).hexdigest().upper()
                    != digest):
                raise ModUpdateError(
                    f"update download path already contains different bytes: {destination}")
            pending.unlink(missing_ok=True)
        else:
            pending.replace(destination)
        return AcquiredModUpdate(
            package, destination, release.repository,
            installed.package_sha256)
    except SemodPackageError as error:
        raise ModUpdateError("downloaded mod package is invalid or unsafe") from error
    finally:
        pending.unlink(missing_ok=True)


__all__ = (
    "AcquiredModUpdate", "GithubReleaseAsset", "ModUpdateError",
    "acquire_github_update", "discover_github_release",
)
