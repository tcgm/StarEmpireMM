"""Build a deterministic, signed, game-code-free loader package."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from installer.hook_recipe import HookRecipeError, parse_hook_recipe
from installer.mod_package import PackageError, verify_mod_package
from installer.release_profiles import (
    ReleaseProfileError,
    parse_release_binding,
    profile_from_signed_binding,
    release_profile,
    validate_recipe_for_profile,
)
from installer.trusted_keys import BUILTIN_TRUSTED_KEYS
from installer.version import MANAGER_VERSION


_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class PackageBuildError(RuntimeError):
    """Raised before an incomplete or unsafe release package can be emitted."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _digest(value: str, label: str) -> str:
    value = str(value).upper()
    if (len(value) != 64
            or any(character not in "0123456789ABCDEF" for character in value)):
        raise PackageBuildError(f"{label} must be a SHA-256 value")
    return value


def _public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _fingerprint(public_key: bytes) -> str:
    return f"SHA256:{hashlib.sha256(public_key).hexdigest().upper()}"


def _trusted_signer(*, private_key: Ed25519PrivateKey, key_id: str,
                    trusted_keys: Mapping[str, bytes]) -> bytes:
    """Bind a private signer to the public key deployed for ``key_id``."""
    signer_public_key = _public_key_bytes(private_key)
    trusted_public_key = trusted_keys.get(key_id)
    if trusted_public_key is None:
        raise PackageBuildError(
            f"signing key ID is not deployed as trusted: {key_id}; "
            f"signer public-key fingerprint={_fingerprint(signer_public_key)}")
    if not isinstance(trusted_public_key, bytes) or len(trusted_public_key) != 32:
        raise PackageBuildError(
            f"deployed trusted Ed25519 public key is invalid for key ID: {key_id}")
    if signer_public_key != trusted_public_key:
        raise PackageBuildError(
            f"private signing key does not match deployed trusted key ID: {key_id}; "
            f"signer public-key fingerprint={_fingerprint(signer_public_key)}; "
            f"trusted public-key fingerprint={_fingerprint(trusted_public_key)}")
    return trusted_public_key


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def build_mod_package(*, ui_source: Path, recipe_path: Path,
                      private_key: Ed25519PrivateKey, key_id: str,
                      trusted_keys: Mapping[str, bytes],
                      pack_id: str, mod_version: str, game_version: str,
                      official_client_sha256: str,
                      expected_client_sha256: str,
                      release_profile_id: str | None = None,
                      release_binding_path: Path | None = None,
                      output: Path) -> Path:
    """Create and self-verify one release package without game-owned source."""
    raw_ui_source = Path(ui_source).expanduser()
    if _is_reparse_point(raw_ui_source):
        raise PackageBuildError("ui_source cannot be a symlink or reparse point")
    ui_source = raw_ui_source.resolve(strict=True)
    recipe_path = Path(recipe_path).resolve(strict=True)
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() not in {".seloader", ".seuimod"}:
        raise PackageBuildError(
            "output must use .seloader "
            "(.seuimod remains available for legacy releases)")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite package: {output}")
    key_id = key_id.strip()
    if not key_id or not pack_id.strip() or not mod_version.strip():
        raise PackageBuildError("key ID, pack ID, and mod version are required")
    trusted_public_key = _trusted_signer(
        private_key=private_key, key_id=key_id, trusted_keys=trusted_keys)
    if release_profile_id is not None and release_binding_path is not None:
        raise PackageBuildError(
            "select either a compiled release profile or a signed release binding")
    try:
        recipe_bytes = recipe_path.read_bytes()
        recipe = parse_hook_recipe(recipe_bytes)
    except (OSError, HookRecipeError) as error:
        raise PackageBuildError("integration recipe is invalid") from error
    if recipe.game_version != game_version:
        raise PackageBuildError("integration recipe game version does not match package")

    profile = None
    binding = None
    binding_bytes = None
    if release_profile_id is not None:
        try:
            profile = release_profile(release_profile_id)
            validate_recipe_for_profile(recipe, profile)
        except ReleaseProfileError as error:
            raise PackageBuildError(
                "integration recipe is outside the selected release profile"
            ) from error
        if profile.game_version != game_version:
            raise PackageBuildError(
                "release profile game version does not match package")
        if (_digest(official_client_sha256, "official client hash")
                != profile.official_client_sha256):
            raise PackageBuildError(
                "official client hash does not match release profile")
        if profile.recipe_sha256 and _sha(recipe_bytes) != profile.recipe_sha256:
            raise PackageBuildError(
                "integration recipe bytes do not match release profile")
    elif release_binding_path is not None:
        try:
            binding_path = Path(release_binding_path).resolve(strict=True)
            binding_bytes = binding_path.read_bytes()
            binding = parse_release_binding(binding_bytes)
            if not binding.ui_source_sha256:
                raise PackageBuildError(
                    "legacy release bindings cannot publish new packages")
            profile = profile_from_signed_binding(binding_bytes, recipe_bytes)
        except PackageBuildError:
            raise
        except (OSError, ReleaseProfileError) as error:
            raise PackageBuildError("release binding is invalid") from error
        if recipe_path.name != binding.recipe_file:
            raise PackageBuildError(
                "integration recipe filename does not match release binding")
        if binding.game_version != game_version:
            raise PackageBuildError(
                "release binding game version does not match package")
        if (_digest(official_client_sha256, "official client hash")
                != binding.official_client_sha256):
            raise PackageBuildError(
                "official client hash does not match release binding")
        if (_digest(expected_client_sha256, "expected client hash")
                != binding.expected_client_sha256):
            raise PackageBuildError(
                "expected client hash does not match release binding")

    recipe_payload_path = (
        f"payload/recipes/{binding.recipe_file}"
        if binding is not None else "payload/recipes/integration.hook"
    )
    payloads: dict[str, bytes] = {recipe_payload_path: recipe_bytes}
    if binding is not None and binding_bytes is not None:
        payloads[
            f"payload/bindings/{binding.profile_id}.binding.json"
        ] = binding_bytes
    if profile is None:
        sources = tuple(
            source for source in sorted(ui_source.iterdir())
            if source.is_file() and source.suffix == ".py"
        )
    else:
        sources = tuple(ui_source / name for name in profile.ui_source_files)
        missing = tuple(source.name for source in sources
                        if not source.is_file())
        if missing:
            raise PackageBuildError(
                "release profile authored source is missing: "
                + ", ".join(missing))
    for source in sources:
        if (_is_reparse_point(source)
                or source.resolve(strict=True).parent != ui_source):
            raise PackageBuildError(
                f"authored source must be a direct, non-linked ui_mod file: {source.name}")
        source_bytes = source.read_bytes()
        if (binding is not None and binding.ui_source_sha256
                and _sha(source_bytes) != binding.ui_source_sha256[source.name]):
            raise PackageBuildError(
                "authored source bytes do not match release binding: "
                f"{source.name}")
        payload_directory = (
            profile.payload_directory if profile is not None else "ui_mod")
        payloads[
            f"payload/{payload_directory}/{source.name}"] = source_bytes
    payload_directory = (
        profile.payload_directory if profile is not None else "ui_mod")
    if f"payload/{payload_directory}/__init__.py" not in payloads:
        raise PackageBuildError("ui_source must contain __init__.py")

    manifest = {
        "schema": 2 if binding is not None else 1,
        "format": "star-empire-ui-mod",
        "key_id": key_id,
        "pack_id": pack_id.strip(),
        "mod_version": mod_version.strip(),
        "game_version": game_version.strip(),
        "manager_version_min": MANAGER_VERSION,
        "official_client_sha256": _digest(
            official_client_sha256, "official client hash"),
        "expected_client_sha256": _digest(
            expected_client_sha256, "expected client hash"),
        "payloads": [
            {"path": name, "size": len(payload), "sha256": _sha(payload)}
            for name, payload in sorted(payloads.items())
        ],
    }
    if binding is not None:
        manifest["release_binding"] = (
            f"payload/bindings/{binding.profile_id}.binding.json")
    elif profile is not None:
        manifest["release_profile"] = profile.profile_id
    manifest_bytes = (json.dumps(
        manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    signature = private_key.sign(manifest_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(
        f".{output.stem}.partial{output.suffix.lower()}")
    if pending.exists():
        raise FileExistsError(f"unfinished package already exists: {pending}")
    try:
        with zipfile.ZipFile(pending, "x") as archive:
            _write_entry(archive, "manifest.json", manifest_bytes)
            _write_entry(archive, "manifest.sig", signature)
            for name, payload in sorted(payloads.items()):
                _write_entry(archive, name, payload)
        verified = verify_mod_package(pending, {key_id: trusted_public_key})
        if (verified.manifest_sha256 != _sha(manifest_bytes)
                or verified.compatibility.expected_client_sha256
                != manifest["expected_client_sha256"]):
            raise PackageBuildError("self-verification returned unexpected metadata")
        if binding is not None:
            if (verified.release_binding is None
                    or verified.release_profile is None
                    or verified.release_binding.profile_id != binding.profile_id
                    or verified.release_profile.profile_id != profile.profile_id):
                raise PackageBuildError(
                    "self-verification did not authenticate the release binding")
        try:
            os.link(pending, output)
        except FileExistsError as error:
            raise FileExistsError(
                f"refusing to overwrite package created concurrently: {output}"
            ) from error
        pending.unlink(missing_ok=True)
        return output
    except Exception:
        pending.unlink(missing_ok=True)
        raise


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    payload = Path(path).expanduser().read_bytes()
    try:
        key = serialization.load_pem_private_key(payload, password=None)
    except (TypeError, ValueError):
        if len(payload) != 32:
            raise PackageBuildError(
                "private key must be an unencrypted Ed25519 PEM or 32 raw bytes")
        key = Ed25519PrivateKey.from_private_bytes(payload)
    if not isinstance(key, Ed25519PrivateKey):
        raise PackageBuildError("private key is not Ed25519")
    return key


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authored-source", "--ui-source", dest="ui_source",
        type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--mod-version", required=True)
    parser.add_argument("--game-version", required=True)
    parser.add_argument("--official-client-sha256", required=True)
    parser.add_argument("--expected-client-sha256", required=True)
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument("--release-profile")
    release.add_argument("--release-binding", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    output = build_mod_package(
        ui_source=arguments.ui_source,
        recipe_path=arguments.recipe,
        private_key=_load_private_key(arguments.private_key),
        key_id=arguments.key_id,
        trusted_keys=BUILTIN_TRUSTED_KEYS,
        pack_id=arguments.pack_id,
        mod_version=arguments.mod_version,
        game_version=arguments.game_version,
        official_client_sha256=arguments.official_client_sha256,
        expected_client_sha256=arguments.expected_client_sha256,
        release_profile_id=arguments.release_profile,
        release_binding_path=arguments.release_binding,
        output=arguments.output,
    )
    print(f"Built and verified {output}")


if __name__ == "__main__":
    main()
