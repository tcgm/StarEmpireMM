"""External mod discovery and isolated registration for the game client."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable

from . import (LOADER_API_VERSION, LoaderDiagnostic, ModEventBus,
               ScopedModApi)


logger = logging.getLogger(__name__)


class ModLoaderError(RuntimeError):
    """Raised when the registry itself is unsafe or unreadable."""


@dataclass(frozen=True)
class GameIdentity:
    game_version: str
    client_sha256: str
    game_root: Path | None = None

    def __post_init__(self) -> None:
        version = str(self.game_version).strip()
        digest = str(self.client_sha256).upper()
        if (not version or len(version) > 64
                or any(character in version for character in "\r\n\x00")):
            raise ModLoaderError("running game version is invalid")
        if (len(digest) != 64
                or any(character not in "0123456789ABCDEF"
                       for character in digest)):
            raise ModLoaderError("running Client.exe hash is invalid")
        object.__setattr__(self, "game_version", version)
        object.__setattr__(self, "client_sha256", digest)
        if self.game_root is not None:
            object.__setattr__(
                self, "game_root", Path(self.game_root).expanduser().resolve())


def detect_game_identity(executable: Path | None = None) -> GameIdentity:
    """Identify the exact frozen game build beside its version file."""
    client_exe = Path(executable or sys.executable).expanduser().resolve()
    version_path = client_exe.parent / "version.txt"
    if not client_exe.is_file() or not version_path.is_file():
        raise ModLoaderError("running game identity files are unavailable")
    try:
        text = version_path.read_text(encoding="utf-8").strip()
        if text[:1] in {'"', "[", "{"}:
            decoded = json.loads(text)
            if type(decoded) is not str:
                raise ModLoaderError(
                    "running game version file is not one string")
            version = decoded
        else:
            version = text
        digest = hashlib.sha256(client_exe.read_bytes()).hexdigest().upper()
    except ModLoaderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModLoaderError("could not inspect the running game build") from error
    return GameIdentity(version, digest, client_exe.parent)


@dataclass(frozen=True)
class LoadedMod:
    mod_id: str
    version: str
    install_path: Path
    module_name: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModLoaderError(f"duplicate mod registry key: {key}")
        result[key] = value
    return result


class ExternalModLoader:
    """Load enabled external mods without letting one failure stop the game."""

    def __init__(
            self, registry_path: Path, *,
            import_module: Callable[[str], Any] = importlib.import_module,
            game_identity: GameIdentity | None = None,
            identity_loader: Callable[[], GameIdentity] = detect_game_identity,
    ) -> None:
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.state_root = self.registry_path.parent
        self.mods_root = self.state_root / "mods"
        self.config_root = self.state_root / "config"
        self.bus = ModEventBus()
        self._import_module = import_module
        self._loaded: list[LoadedMod] = []
        self._paths: list[str] = []
        self._module_prefixes: list[tuple[str, Path]] = []
        self._game_identity = game_identity
        self._identity_loader = identity_loader
        self._identity_checked = game_identity is not None
        self._identity_error: Exception | None = None

    @property
    def loaded(self) -> tuple[LoadedMod, ...]:
        return tuple(self._loaded)

    @property
    def diagnostics(self) -> tuple[LoaderDiagnostic, ...]:
        return self.bus.diagnostics

    def _registry(self) -> tuple[dict[str, Any], ...]:
        if not self.registry_path.is_file():
            return ()
        try:
            data = json.loads(
                self.registry_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModLoaderError(
                f"cannot read external mod registry: {self.registry_path}") from error
        if (type(data) is not dict
                or set(data) != {"schema", "mods", "load_order"}
                or data["schema"] not in {1, 2}
                or type(data["mods"]) is not list
                or type(data["load_order"]) is not list):
            raise ModLoaderError("external mod registry is malformed")
        by_id: dict[str, dict[str, Any]] = {}
        for stored in data["mods"]:
            required = {
                    "manifest", "install_path", "package_sha256", "enabled",
                    "source", "installed_at"}
            if data["schema"] == 2:
                required.add("force_load")
            if type(stored) is not dict or set(stored) != required:
                raise ModLoaderError("external mod registry entry is malformed")
            raw = dict(stored)
            if data["schema"] == 1:
                raw["force_load"] = False
            if type(raw["force_load"]) is not bool:
                raise ModLoaderError("external mod force-load setting is malformed")
            manifest = raw["manifest"]
            if (type(manifest) is not dict
                    or type(manifest.get("mod_id")) is not str):
                raise ModLoaderError("external mod manifest identity is missing")
            mod_id = manifest["mod_id"]
            if mod_id in by_id:
                raise ModLoaderError("external mod registry contains duplicate IDs")
            by_id[mod_id] = raw
        order = data["load_order"]
        if (any(type(item) is not str for item in order)
                or len(set(order)) != len(order) or set(order) != set(by_id)):
            raise ModLoaderError("external mod load order is incomplete")
        return tuple(by_id[mod_id] for mod_id in order if by_id[mod_id]["enabled"])

    def _managed_install_path(self, raw: Any) -> Path:
        if type(raw) is not str:
            raise ModLoaderError("external mod install path is invalid")
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(self.mods_root.resolve())
        except ValueError as error:
            raise ModLoaderError(
                f"external mod path escapes the managed root: {path}") from error
        return path

    @staticmethod
    def _entrypoint(manifest: dict[str, Any]) -> tuple[str, str]:
        value = manifest.get("entrypoint")
        if type(value) is not str or value.count(":") != 1:
            raise ModLoaderError("external mod entrypoint is invalid")
        module_name, callable_name = value.split(":", 1)
        mod_id = manifest.get("mod_id", "")
        namespace = str(mod_id).replace("-", "_")
        if (module_name != namespace
                and not module_name.startswith(namespace + ".")):
            raise ModLoaderError(
                f"external mod entrypoint is outside its namespace: {value}")
        if not callable_name.isidentifier():
            raise ModLoaderError("external mod entrypoint callable is invalid")
        return module_name, callable_name

    @staticmethod
    def _digest(value: Any, label: str) -> str:
        if type(value) is not str:
            raise ModLoaderError(f"{label} is invalid")
        result = value.upper()
        if (len(result) != 64
                or any(char not in "0123456789ABCDEF" for char in result)):
            raise ModLoaderError(f"{label} is invalid")
        return result

    def _current_game_identity(self) -> GameIdentity:
        if not self._identity_checked:
            self._identity_checked = True
            try:
                self._game_identity = self._identity_loader()
            except Exception as error:
                self._identity_error = error
        if self._game_identity is None:
            detail = (
                str(self._identity_error).strip()
                if self._identity_error is not None else
                "running game identity is unavailable")
            raise ModLoaderError(
                f"cannot verify compatible game build: {detail}")
        return self._game_identity

    def _manager_version_bridge_allows(
            self, identity: GameIdentity,
            allowed_versions: set[str]) -> bool:
        """Verify one Manager-approved cross-version compatibility rebuild."""
        state_path = self.state_root / "install-state.json"
        if identity.game_root is None or not state_path.is_file():
            return False
        try:
            data = json.loads(
                state_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        expected = {
            "schema", "game_root", "original_client", "original_sha256",
            "installed_sha256", "pack_id", "created_at", "pack_digest",
            "key_id", "mod_version", "game_version",
            "compatibility_source_game_version",
        }
        if type(data) is not dict or set(data) != expected or data.get("schema") != 3:
            return False
        text_fields = (
            "game_root", "installed_sha256", "game_version",
            "compatibility_source_game_version",
        )
        if any(type(data.get(key)) is not str for key in text_fields):
            return False
        running_version = data["game_version"].strip()
        source_version = data["compatibility_source_game_version"].strip()
        if (not source_version or source_version == running_version
                or source_version not in allowed_versions
                or running_version != identity.game_version
                or any(character in source_version + running_version
                       for character in "\r\n\x00")):
            return False
        try:
            installed_sha256 = self._digest(
                data["installed_sha256"], "installed Client.exe hash")
            approved_root = Path(data["game_root"]).expanduser().resolve()
        except (ModLoaderError, OSError, RuntimeError, ValueError):
            return False
        return (installed_sha256 == identity.client_sha256
                and approved_root == identity.game_root)

    def _verify_game_compatibility(
            self, manifest: dict[str, Any], force_load: bool = False) -> None:
        raw = manifest.get("compatibility")
        if raw is None:
            # Loader-API-only mods remain portable. Mods that reach into game
            # host classes must opt into the exact-build contract.
            return
        if type(raw) is not dict:
            raise ModLoaderError("mod game compatibility metadata is malformed")
        mode = raw.get("mode")
        if mode == "game-version":
            if (set(raw) != {"mode", "game_versions"}
                    or type(raw.get("game_versions")) is not list):
                raise ModLoaderError(
                    "mod game compatibility metadata is malformed")
            versions = raw["game_versions"]
            if not versions:
                raise ModLoaderError("mod has no supported game versions")
            allowed_versions: set[str] = set()
            for item in versions:
                if type(item) is not str:
                    raise ModLoaderError(
                        "mod compatible game version is invalid")
                version = item.strip()
                if (not version or len(version) > 64
                        or any(character in version for character in "\r\n\x00")):
                    raise ModLoaderError(
                        "mod compatible game version is invalid")
                if version in allowed_versions:
                    raise ModLoaderError(
                        "mod compatible game versions contain duplicates")
                allowed_versions.add(version)
            identity = self._current_game_identity()
            if (identity.game_version not in allowed_versions
                    and not self._manager_version_bridge_allows(
                        identity, allowed_versions)):
                if force_load:
                    logger.warning(
                        "MOD_COMPATIBILITY_FORCE_LOAD mod_id=%s "
                        "running_game_version=%s supported_game_versions=%s",
                        manifest.get("mod_id", "unknown"),
                        identity.game_version,
                        ",".join(sorted(allowed_versions)))
                    return
                raise ModLoaderError(
                    "mod does not support running game version "
                    f"{identity.game_version}")
            return
        if (set(raw) != {"mode", "game_builds"}
                or mode != "exact-game-build"
                or type(raw.get("game_builds")) is not list):
            raise ModLoaderError("mod game compatibility metadata is malformed")
        builds = raw["game_builds"]
        if not builds:
            raise ModLoaderError("mod has no approved game builds")
        allowed: set[tuple[str, str]] = set()
        for item in builds:
            if (type(item) is not dict
                    or set(item) != {"game_version", "client_sha256"}
                    or type(item.get("game_version")) is not str):
                raise ModLoaderError(
                    "mod compatible game build entry is malformed")
            version = item["game_version"].strip()
            if (not version or len(version) > 64
                    or any(character in version for character in "\r\n\x00")):
                raise ModLoaderError(
                    "mod compatible game version is invalid")
            digest = self._digest(
                item.get("client_sha256"), "compatible Client.exe hash")
            allowed.add((version, digest))
        identity = self._current_game_identity()
        if (identity.game_version,
                identity.client_sha256) not in allowed:
            if force_load:
                logger.warning(
                    "MOD_COMPATIBILITY_FORCE_LOAD mod_id=%s "
                    "running_game_build=%s:%s",
                    manifest.get("mod_id", "unknown"),
                    identity.game_version, identity.client_sha256[:12])
                return
            raise ModLoaderError(
                "mod does not support running game build "
                f"{identity.game_version} "
                f"{identity.client_sha256[:12]}")

    def _verify_installed_files(
            self, install_path: Path, manifest: dict[str, Any],
            package_sha256: Any) -> None:
        receipt_path = install_path / ".semod-install.json"
        try:
            receipt = json.loads(
                receipt_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModLoaderError("installed mod integrity record is missing or invalid") from error
        if type(receipt) is not dict:
            raise ModLoaderError("installed mod integrity record is malformed")
        common_fields = {
            "schema", "package_sha256", "package_manifest_sha256",
            "payloads",
        }
        schema = receipt.get("schema")
        current_shape = schema == 2 and set(receipt) == common_fields
        legacy_shape = (
            schema == 1
            and set(receipt) == common_fields | {"key_id"}
            and type(receipt.get("key_id")) is str
            and bool(receipt["key_id"].strip())
        )
        # Schema 1 carried a key label, but the runtime never used it to make a
        # trust decision.  Accept it only as legacy receipt structure so games
        # patched with loader v4 can run new keyless external mods.
        if not current_shape and not legacy_shape:
            raise ModLoaderError("installed mod integrity record is malformed")
        if self._digest(receipt["package_sha256"], "package hash") != self._digest(
                package_sha256, "registry package hash"):
            raise ModLoaderError("installed mod integrity record belongs to another package")
        self._digest(receipt["package_manifest_sha256"], "package manifest hash")
        raw_payloads = receipt["payloads"]
        if type(raw_payloads) is not list or not raw_payloads:
            raise ModLoaderError("installed mod integrity inventory is malformed")
        payloads: dict[str, tuple[int, str]] = {}
        folded: set[str] = set()
        for raw in raw_payloads:
            if type(raw) is not dict or set(raw) != {"path", "size", "sha256"}:
                raise ModLoaderError("installed mod integrity entry is malformed")
            relative = raw["path"]
            size = raw["size"]
            if (type(relative) is not str or not relative or "\\" in relative
                    or relative.startswith("/") or "\x00" in relative
                    or any(part in {"", ".", ".."}
                           for part in relative.split("/"))
                    or type(size) is not int or size < 0):
                raise ModLoaderError("installed mod integrity path or size is invalid")
            key = relative.casefold()
            if key in folded:
                raise ModLoaderError("installed mod integrity inventory collides")
            folded.add(key)
            payloads[relative] = (
                size, self._digest(raw["sha256"], f"payload hash {relative}"))
        manifest_files = manifest.get("files")
        if (type(manifest_files) is not list
                or any(type(item) is not str for item in manifest_files)
                or set(payloads) != {"manifest.json", *manifest_files}):
            raise ModLoaderError("installed mod files differ from verified inventory")

        allowed = set(payloads) | {".semod-install.json"}
        actual: set[str] = set()
        for item in install_path.rglob("*"):
            relative = item.relative_to(install_path).as_posix()
            is_junction = getattr(item, "is_junction", lambda: False)
            if item.is_symlink() or is_junction():
                raise ModLoaderError(f"installed mod contains a link: {relative}")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ModLoaderError(f"installed mod contains a special file: {relative}")
            actual.add(relative)
        if actual != allowed:
            raise ModLoaderError("installed mod directory contains missing or extra files")
        for relative, (expected_size, expected_hash) in payloads.items():
            path = install_path / Path(*relative.split("/"))
            try:
                payload = path.read_bytes()
            except OSError as error:
                raise ModLoaderError(
                    f"installed mod payload cannot be read: {relative}") from error
            if (len(payload) != expected_size
                    or hashlib.sha256(payload).hexdigest().upper() != expected_hash):
                raise ModLoaderError(
                    f"installed mod payload failed integrity check: {relative}")

    def load_enabled(self) -> tuple[LoadedMod, ...]:
        if self._loaded:
            raise ModLoaderError("external mods are already loaded")
        for raw in self._registry():
            manifest = raw["manifest"]
            mod_id = str(manifest["mod_id"])
            try:
                version = str(manifest["version"])
                loader_api = manifest["loader_api"]
                if type(loader_api) is not int or loader_api > LOADER_API_VERSION:
                    raise ModLoaderError(
                        f"requires loader API {loader_api}; current is {LOADER_API_VERSION}")
                self._verify_game_compatibility(
                    manifest, force_load=raw["force_load"])
                install_path = self._managed_install_path(raw["install_path"])
                manifest_path = install_path / "manifest.json"
                try:
                    installed_manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8"),
                        object_pairs_hook=_unique_object)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ModLoaderError("installed manifest cannot be verified") from error
                if installed_manifest != manifest:
                    raise ModLoaderError("installed manifest differs from registry")
                self._verify_installed_files(
                    install_path, manifest, raw["package_sha256"])
                module_name, callable_name = self._entrypoint(manifest)
                source_root = (install_path / "mod").resolve()
                if not source_root.is_dir():
                    raise ModLoaderError("external mod source directory is missing")
                source_text = str(source_root)
                if source_text not in sys.path:
                    sys.path.insert(0, source_text)
                    self._paths.append(source_text)
                existing = sys.modules.get(module_name)
                if existing is not None:
                    existing_path = Path(
                        getattr(existing, "__file__", "")).resolve()
                    try:
                        existing_path.relative_to(source_root)
                    except (OSError, ValueError) as error:
                        raise ModLoaderError(
                            f"module namespace is already owned: {module_name}") from error
                previous_bytecode = sys.dont_write_bytecode
                sys.dont_write_bytecode = True
                try:
                    module = self._import_module(module_name)
                finally:
                    sys.dont_write_bytecode = previous_bytecode
                register = getattr(module, callable_name, None)
                if not callable(register):
                    raise ModLoaderError("external mod register entrypoint is not callable")
                api = ScopedModApi(
                    self.bus, mod_id, version, install_path, self.config_root)
                register(api)
                loaded = LoadedMod(mod_id, version, install_path, module_name)
                self._loaded.append(loaded)
                self._module_prefixes.append((module_name, source_root))
            except Exception as error:
                logger.exception("MOD_LOAD_FAILED mod_id=%s", mod_id)
                self.bus.remove_mod(mod_id)
                self.bus.add_diagnostic(mod_id, "load", str(error))
        self.bus.emit("loader.ready", self)
        return self.loaded

    def emit(self, event_name: str, *args, **kwargs) -> tuple[Any, ...]:
        return self.bus.emit(event_name, *args, **kwargs)

    def shutdown(self) -> None:
        self.bus.emit("loader.shutdown", self)
        for loaded in tuple(self._loaded):
            self.bus.remove_mod(loaded.mod_id)
        for prefix, source_root in reversed(self._module_prefixes):
            for name, module in tuple(sys.modules.items()):
                if name != prefix and not name.startswith(prefix + "."):
                    continue
                try:
                    module_path = Path(getattr(module, "__file__", "")).resolve()
                    module_path.relative_to(source_root)
                except (OSError, ValueError):
                    continue
                sys.modules.pop(name, None)
        for path in self._paths:
            while path in sys.path:
                sys.path.remove(path)
        self._loaded.clear()
        self._paths.clear()
        self._module_prefixes.clear()


__all__ = [
    "ExternalModLoader", "GameIdentity", "LoadedMod", "ModLoaderError",
    "detect_game_identity",
]
