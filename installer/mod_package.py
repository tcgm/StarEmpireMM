"""Verification and safe extraction for signed loader packages.

The package format deliberately contains no game executable, host source,
bytecode, or private unified patch.  ``manifest.sig`` is an Ed25519 signature
over the exact UTF-8 bytes stored as ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .manager_core import CompatibilityPack, ManagerDataError
from .hook_recipe import HookRecipeError, parse_hook_recipe
from .release_profiles import (ReleaseBinding, ReleaseProfile,
                               ReleaseProfileError, parse_release_binding,
                               profile_from_signed_binding)
from .version import (MANAGER_VERSION, package_requirement_version,
                      version_tuple)


PACKAGE_SCHEMA = 1
DYNAMIC_PACKAGE_SCHEMA = 2
PACKAGE_FORMAT = "star-empire-ui-mod"
MAX_ENTRIES = 1024
MAX_FILE_SIZE = 32 * 1024 * 1024
MAX_TOTAL_SIZE = 192 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
ALLOWED_SUFFIXES = frozenset({
    ".py", ".png", ".json", ".toml", ".txt", ".md", ".hook",
})
FORBIDDEN_NAMES = frozenset({
    "client.py", "render_mixin.py", "hangar_inventory.py",
    "gl_renderer.py", "item_search.py", "protocol.py", "client.exe",
})
FORBIDDEN_SUFFIXES = frozenset({
    ".exe", ".dll", ".pyd", ".pyc", ".pyo", ".patch", ".diff",
    ".zip", ".7z", ".rar", ".tar", ".gz",
})


class PackageError(ValueError):
    """Raised when a package is malformed, untrusted, or unsafe."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _safe_payload_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise PackageError("payload path is empty or malformed")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageError(f"unsafe payload path: {value}")
    if not path.parts or path.parts[0] != "payload":
        raise PackageError(f"payload path must start with payload/: {value}")
    if path.name.lower() in FORBIDDEN_NAMES:
        raise PackageError(f"game-owned host source is forbidden: {value}")
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES or suffix not in ALLOWED_SUFFIXES:
        raise PackageError(f"payload type is not allowed: {value}")
    return path


@dataclass(frozen=True)
class PayloadEntry:
    path: PurePosixPath
    size: int
    sha256: str


@dataclass(frozen=True)
class VerifiedModPackage:
    """An authenticated package whose complete ZIP inventory was validated."""

    path: Path
    manifest: Mapping[str, Any]
    manifest_sha256: str
    payloads: tuple[PayloadEntry, ...]
    release_binding: ReleaseBinding | None = None
    release_profile: ReleaseProfile | None = None

    @property
    def compatibility(self) -> CompatibilityPack:
        try:
            return CompatibilityPack.from_mapping({
                "schema": 1,
                "pack_id": self.manifest["pack_id"],
                "mod_version": self.manifest["mod_version"],
                "game_version": self.manifest["game_version"],
                "official_client_sha256": self.manifest["official_client_sha256"],
                "expected_client_sha256": self.manifest["expected_client_sha256"],
                "pack_digest": self.manifest_sha256,
                "key_id": self.manifest["key_id"],
            }, self.path.parent / "manifest.json")
        except (KeyError, ManagerDataError) as error:
            raise PackageError("verified package has invalid compatibility metadata") from error

    def extract_payload(self, destination: Path) -> Path:
        """Extract declared authored payloads atomically to a new directory."""
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise PackageError(f"refusing to overwrite package directory: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # A normal child directory inherits the destination's Windows ACL.
        # tempfile.mkdtemp() applies owner-only permissions which can survive
        # an elevated build and become unreadable when renamed into place.
        temporary = target.parent / (
            f".{target.name}.{uuid.uuid4().hex}.partial")
        temporary.mkdir()
        try:
            with zipfile.ZipFile(self.path) as archive:
                for entry in self.payloads:
                    payload = archive.read(entry.path.as_posix())
                    if len(payload) != entry.size or _sha256(payload) != entry.sha256:
                        raise PackageError(
                            f"package payload changed after verification: {entry.path}")
                    relative = Path(*entry.path.parts[1:])
                    output = temporary / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(payload)
            temporary.replace(target)
        except Exception:
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target


LOADER_PACKAGE_SUFFIXES = frozenset((".seloader", ".seuimod"))


def verify_mod_package(path: Path,
                       trusted_keys: Mapping[str, bytes]) -> VerifiedModPackage:
    """Authenticate and inspect one package without extracting it."""
    source = Path(path).expanduser().resolve()
    if (not source.is_file()
            or source.suffix.lower() not in LOADER_PACKAGE_SUFFIXES):
        raise PackageError(
            "select a .seloader package file "
            "(.seuimod is supported for legacy bundles)")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageError(f"cannot read UI Mod package: {source}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageError("package contains too many entries")
        exact_names: set[str] = set()
        folded_names: set[str] = set()
        total_size = 0
        for info in infos:
            name = info.filename
            folded = name.casefold()
            if name in exact_names or folded in folded_names:
                raise PackageError(f"duplicate or case-colliding package path: {name}")
            exact_names.add(name)
            folded_names.add(folded)
            path_object = PurePosixPath(name)
            if (not name or "\\" in name or path_object.is_absolute()
                    or any(part in {"", ".", ".."} for part in path_object.parts)):
                raise PackageError(f"unsafe package path: {name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise PackageError(f"symbolic links are forbidden: {name}")
            if info.flag_bits & 0x1:
                raise PackageError("encrypted package entries are forbidden")
            if info.is_dir():
                continue
            if info.file_size > MAX_FILE_SIZE:
                raise PackageError(f"package entry is too large: {name}")
            total_size += info.file_size
            if total_size > MAX_TOTAL_SIZE:
                raise PackageError("package expands beyond the allowed size")
            if (info.file_size > 1024 * 1024
                    and info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                raise PackageError(f"suspicious compression ratio: {name}")

        required = {"manifest.json", "manifest.sig"}
        if not required.issubset(exact_names):
            raise PackageError("package is missing manifest.json or manifest.sig")
        manifest_bytes = archive.read("manifest.json")
        signature_bytes = archive.read("manifest.sig")
        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PackageError("manifest.json is not valid unique-key UTF-8 JSON") from error
        if not isinstance(manifest, dict):
            raise PackageError("manifest.json must contain one object")
        package_schema = manifest.get("schema")
        if (type(package_schema) is not int
                or package_schema not in {PACKAGE_SCHEMA, DYNAMIC_PACKAGE_SCHEMA}
                or manifest.get("format") != PACKAGE_FORMAT):
            raise PackageError("unsupported UI Mod package format")
        if package_schema == PACKAGE_SCHEMA and "release_binding" in manifest:
            raise PackageError("legacy package cannot declare a release binding")
        binding_reference = None
        if package_schema == DYNAMIC_PACKAGE_SCHEMA:
            if "release_profile" in manifest:
                raise PackageError(
                    "dynamic package cannot fall back to a compiled release profile")
            raw_binding_reference = manifest.get("release_binding")
            if type(raw_binding_reference) is not str:
                raise PackageError("dynamic package must name one release binding")
            binding_reference = _safe_payload_path(raw_binding_reference)
            if (binding_reference.parts[:2] != ("payload", "bindings")
                    or not binding_reference.name.endswith(".binding.json")):
                raise PackageError("dynamic package release binding path is invalid")
        key_id = str(manifest.get("key_id", "")).strip()
        public_bytes = trusted_keys.get(key_id)
        if public_bytes is None:
            raise PackageError(f"package signing key is not trusted: {key_id or '<missing>'}")
        if len(public_bytes) != 32:
            raise PackageError(f"trusted Ed25519 public key has invalid length: {key_id}")
        try:
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                signature_bytes, manifest_bytes)
        except (InvalidSignature, ValueError) as error:
            raise PackageError("package signature verification failed") from error

        required_text = (
            "pack_id", "mod_version", "game_version", "manager_version_min",
            "official_client_sha256", "expected_client_sha256",
        )
        if any(not str(manifest.get(key, "")).strip() for key in required_text):
            raise PackageError("manifest compatibility metadata is incomplete")
        try:
            required_manager = version_tuple(package_requirement_version(
                str(manifest["manager_version_min"])))
            running_manager = version_tuple(MANAGER_VERSION)
        except ValueError as error:
            raise PackageError("manifest manager version is invalid") from error
        if required_manager > running_manager:
            raise PackageError(
                "package requires a newer Star Empire UI Mod Manager")
        for key in ("official_client_sha256", "expected_client_sha256"):
            value = str(manifest[key])
            if len(value) != 64 or any(char not in "0123456789ABCDEFabcdef"
                                       for char in value):
                raise PackageError(f"manifest {key} is not a SHA-256")
        payload_raw = manifest.get("payloads")
        if not isinstance(payload_raw, list) or not payload_raw:
            raise PackageError("manifest must declare at least one payload")
        declared: dict[str, PayloadEntry] = {}
        for raw in payload_raw:
            if not isinstance(raw, dict):
                raise PackageError("manifest payload entry must be an object")
            if set(raw) != {"path", "size", "sha256"}:
                raise PackageError("manifest payload entry has unknown or missing fields")
            safe_path = _safe_payload_path(str(raw["path"]))
            try:
                size = int(raw["size"])
            except (TypeError, ValueError) as error:
                raise PackageError(f"invalid payload size: {safe_path}") from error
            digest = str(raw["sha256"]).upper()
            if (size < 0 or size > MAX_FILE_SIZE or len(digest) != 64
                    or any(char not in "0123456789ABCDEF" for char in digest)):
                raise PackageError(f"invalid payload metadata: {safe_path}")
            folded = safe_path.as_posix().casefold()
            if folded in declared:
                raise PackageError(f"duplicate declared payload: {safe_path}")
            declared[folded] = PayloadEntry(safe_path, size, digest)

        actual_payload_names = {
            name.casefold() for name in exact_names if name.startswith("payload/")
            and not name.endswith("/")
        }
        if actual_payload_names != set(declared):
            raise PackageError("ZIP payload inventory does not exactly match manifest")
        allowed_names = required | {
            entry.path.as_posix() for entry in declared.values()
        }
        unexpected = exact_names.difference(allowed_names)
        if unexpected:
            raise PackageError(
                "package contains undeclared entries: " + ", ".join(sorted(unexpected)))

        payloads = tuple(sorted(declared.values(), key=lambda entry: entry.path.as_posix()))
        verified_payload_bytes: dict[str, bytes] = {}
        for entry in payloads:
            payload = archive.read(entry.path.as_posix())
            if len(payload) != entry.size or _sha256(payload) != entry.sha256:
                raise PackageError(f"payload hash or size mismatch: {entry.path}")
            if payload.startswith(b"MZ"):
                raise PackageError(f"executable payload is forbidden: {entry.path}")
            verified_payload_bytes[entry.path.as_posix()] = payload
            if entry.path.suffix.lower() == ".hook":
                try:
                    recipe = parse_hook_recipe(payload)
                except HookRecipeError as error:
                    raise PackageError(
                        f"invalid integration recipe: {entry.path}: {error}") from error
                if recipe.game_version != str(manifest["game_version"]):
                    raise PackageError(
                        f"integration recipe targets another game version: {entry.path}")

        release_binding = None
        release_profile = None
        if package_schema == DYNAMIC_PACKAGE_SCHEMA:
            binding_paths = tuple(
                entry.path for entry in payloads
                if entry.path.name.endswith(".binding.json"))
            recipe_paths = tuple(
                entry.path for entry in payloads
                if entry.path.suffix.lower() == ".hook")
            if (len(binding_paths) != 1 or binding_paths[0] != binding_reference
                    or len(recipe_paths) != 1):
                raise PackageError(
                    "dynamic package must contain exactly one bound recipe and binding")
            binding_payload = verified_payload_bytes[
                binding_reference.as_posix()]
            recipe_payload = verified_payload_bytes[recipe_paths[0].as_posix()]
            try:
                release_binding = parse_release_binding(binding_payload)
                release_profile = profile_from_signed_binding(
                    binding_payload, recipe_payload)
            except ReleaseProfileError as error:
                raise PackageError("dynamic release binding is invalid") from error
            expected_binding_path = PurePosixPath(
                "payload", "bindings",
                f"{release_profile.profile_id}.binding.json")
            expected_recipe_path = PurePosixPath(
                "payload", "recipes", release_binding.recipe_file)
            if (binding_reference != expected_binding_path
                    or recipe_paths[0] != expected_recipe_path):
                raise PackageError("dynamic package binding or recipe path is invalid")
            if (str(manifest["game_version"]) != release_profile.game_version
                    or str(manifest["official_client_sha256"]).upper()
                    != release_profile.official_client_sha256
                    or str(manifest["expected_client_sha256"]).upper()
                    != release_binding.expected_client_sha256):
                raise PackageError(
                    "dynamic package compatibility does not match its binding")
            expected_ui = {
                f"payload/{release_profile.payload_directory}/{name}"
                for name in release_profile.ui_source_files
            }
            expected_payloads = expected_ui | {
                expected_binding_path.as_posix(),
                expected_recipe_path.as_posix(),
            }
            actual_payloads = {entry.path.as_posix() for entry in payloads}
            if actual_payloads != expected_payloads:
                raise PackageError(
                    "dynamic package payload is outside its release policy")
            for name, expected_digest in release_profile.ui_source_sha256.items():
                source_path = (
                    f"payload/{release_profile.payload_directory}/{name}")
                if _sha256(verified_payload_bytes[source_path]) != expected_digest:
                    raise PackageError(
                        "dynamic package authored source does not match its binding: "
                        f"{name}")

        return VerifiedModPackage(
            source, manifest, _sha256(manifest_bytes), payloads,
            release_binding, release_profile)
