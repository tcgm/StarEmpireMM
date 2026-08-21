"""Build a verified modded client from local vanilla bytes and authored files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Callable, Iterable
import uuid

from .hook_recipe import (HookRecipeError, InsertFragmentOperation,
                          parse_hook_recipe)
from .manager_core import (CLIENT_EXE, CompatibilityPack, ManagerDataError,
                           read_game_version, sha256_file)
from .mod_package import PackageError, VerifiedModPackage
from .release_profiles import (ReleaseProfile, ReleaseProfileError,
                               profile_from_signed_binding,
                               release_profile,
                               validate_recipe_for_profile)


class CandidateBuildError(RuntimeError):
    """Raised when an authenticated package cannot produce an exact candidate."""


@dataclass(frozen=True)
class BuiltCandidate:
    """Evidence tying a candidate to one baseline and signed package."""

    path: Path
    sha256: str
    official_sha256: str
    pack_id: str
    pack_digest: str
    key_id: str
    module_names: tuple[str, ...]
    audit_path: Path
    compatibility_test: bool = False
    observed_version: str | None = None
    temporary_root: Path | None = None

    def verify_for(self, pack: CompatibilityPack) -> Path:
        candidate = self.path.resolve(strict=True)
        package_evidence_changed = (
            self.pack_id != pack.pack_id
            or not self.pack_digest
            or self.pack_digest != pack.pack_digest
            or self.key_id != pack.key_id
        )
        exact_hashes_changed = (
            not self.compatibility_test
            and (self.official_sha256 != pack.official_client_sha256
                 or self.sha256 != pack.expected_client_sha256)
        )
        if (package_evidence_changed or exact_hashes_changed
                or sha256_file(candidate) != self.sha256):
            raise CandidateBuildError(
                "built candidate no longer matches its verified package evidence")
        return candidate

    def cleanup(self) -> None:
        """Delete only this candidate's isolated Manager work directory."""
        if self.temporary_root is None:
            return
        root = self.temporary_root.resolve()
        if (self.path.parent.parent.resolve() != root
                or self.audit_path.parent.resolve() != root):
            raise CandidateBuildError(
                "refusing to clean candidate outside its recorded temporary folder")
        shutil.rmtree(root)


RepackFunction = Callable[[Path, Path, Path, Iterable[str] | None], None]


def _default_repack(input_exe: Path, source: Path, output_exe: Path,
                    module_names: Iterable[str] | None) -> None:
    from tools.repack_client import repack_client
    repack_client(input_exe, source, output_exe, module_names)


def _registered_modules() -> dict[str, Path]:
    from tools.repack_client import MODULE_SOURCE_PATHS
    return dict(MODULE_SOURCE_PATHS)


def _authored_module_name(relative: Path, source_package: str) -> str:
    if relative.parent != Path(".") or relative.suffix != ".py":
        raise CandidateBuildError(
            f"only top-level authored Python modules can be frozen: {relative}")
    return (source_package if relative.name == "__init__.py"
            else f"{source_package}.{relative.stem}")


def _verified_release_profile(
        package: VerifiedModPackage,
        compatibility: CompatibilityPack) -> ReleaseProfile | None:
    """Resolve the closed public profile for a real authenticated package.

    Small in-memory test doubles used by the generic builder tests are not
    ``VerifiedModPackage`` instances. The Manager's production path always is.
    """
    if not isinstance(package, VerifiedModPackage):
        return None
    dynamic = (package.manifest.get("schema") == 2
               or "release_binding" in package.manifest)
    if dynamic:
        if package.release_binding is None or package.release_profile is None:
            raise CandidateBuildError(
                "verified dynamic package has no authenticated release binding")
        profile = package.release_profile
        binding = package.release_binding
        if (binding.profile_id != profile.profile_id
                or binding.game_version != profile.game_version
                or binding.official_client_sha256 != profile.official_client_sha256
                or dict(binding.host_input_sha256)
                != dict(profile.host_input_sha256)
                or dict(binding.host_output_sha256)
                != dict(profile.host_output_sha256)
                or dict(binding.fragment_offsets)
                != dict(profile.fragment_offsets)
                or binding.ui_source_files != profile.ui_source_files
                or binding.recipe_sha256 != profile.recipe_sha256
                or compatibility.expected_client_sha256
                != binding.expected_client_sha256):
            raise CandidateBuildError(
                "authenticated release binding and profile disagree")
    else:
        profile_id = str(package.manifest.get("release_profile", "")).strip()
        if not profile_id:
            raise CandidateBuildError(
                "verified package does not name a release profile")
        try:
            profile = release_profile(profile_id)
        except ReleaseProfileError as error:
            raise CandidateBuildError(str(error)) from error
    if (compatibility.game_version != profile.game_version
            or compatibility.official_client_sha256
            != profile.official_client_sha256):
        raise CandidateBuildError(
            "verified package compatibility does not match its release profile")

    expected_ui = {
        f"payload/{profile.payload_directory}/{name}"
        for name in profile.ui_source_files
    }
    actual_ui = {
        entry.path.as_posix() for entry in package.payloads
        if entry.path.parts[:2] == (
            "payload", profile.payload_directory)
    }
    recipes = tuple(
        entry for entry in package.payloads
        if entry.path.suffix.lower() == ".hook"
    )
    actual_all = {entry.path.as_posix() for entry in package.payloads}
    allowed_all = expected_ui | {
        entry.path.as_posix() for entry in recipes
    }
    if dynamic:
        binding_paths = tuple(
            entry.path.as_posix() for entry in package.payloads
            if entry.path.name.endswith(".binding.json"))
        expected_binding_path = str(package.manifest.get("release_binding", ""))
        expected_recipe_path = (
            f"payload/recipes/{package.release_binding.recipe_file}")
        if (binding_paths != (expected_binding_path,)
                or len(recipes) != 1
                or recipes[0].path.as_posix() != expected_recipe_path):
            raise CandidateBuildError(
                "verified package binding inventory is invalid")
        allowed_all.add(expected_binding_path)
    if (len(recipes) != 1 or actual_ui != expected_ui
            or actual_all != allowed_all):
        raise CandidateBuildError(
            "verified package payload is outside its release profile")
    return profile


def build_candidate(package: VerifiedModPackage, game_root: Path,
                    work_root: Path, *,
                    baseline_client: Path | None = None,
                    compatibility_test: bool = False,
                    repack: RepackFunction = _default_repack) -> BuiltCandidate:
    """Build in isolation; never mutate the selected installation."""
    game_root = Path(game_root).expanduser().resolve()
    live_client = game_root / CLIENT_EXE
    client = (Path(baseline_client).expanduser().resolve()
              if baseline_client is not None else live_client)
    compatibility = package.compatibility
    profile = _verified_release_profile(package, compatibility)
    if not live_client.is_file() or not client.is_file():
        raise CandidateBuildError("selected game does not contain Client.exe")
    baseline_sha256 = sha256_file(client)
    if (not compatibility_test
            and baseline_sha256 != compatibility.official_client_sha256):
        raise CandidateBuildError(
            "Client.exe does not match the signed package's official baseline")
    version_path = game_root / "version.txt"
    try:
        game_version = read_game_version(version_path)
    except ManagerDataError as error:
        raise CandidateBuildError(
            "game version file could not be verified") from error
    if (not compatibility_test and game_version is not None
            and game_version != compatibility.game_version):
        raise CandidateBuildError("game version does not match the signed package")

    local_recipe_bytes: bytes | None = None
    local_offsets: dict[str, int] | None = None
    if compatibility_test:
        if (not isinstance(package, VerifiedModPackage)
                or package.manifest.get("schema") != 2
                or package.release_binding is None
                or profile is None):
            raise CandidateBuildError(
                "compatibility testing requires an authenticated schema-2 package")
        policy = package.release_binding.policy
        try:
            from tools.build_version_binding import (
                UI_MARKERS, VersionBindingError, _apply_policy_fragments,
                _build_recipe, locate_policy_offsets,
                verify_clean_client_archive, verify_clean_module_archive,
            )
            sources = {
                name: (game_root / "_internal" / path).read_bytes()
                for name, path in policy.hosts
            }
            if any(marker in sources["Client"] for marker in UI_MARKERS):
                raise VersionBindingError(
                    "Client.py already contains UI Mod integration markers")
            verify_clean_client_archive(client, sources["Client"])
            for module_name, module_path in policy.hosts[1:]:
                verify_clean_module_archive(
                    client, module_name, module_path, sources[module_name])
            local_offsets = locate_policy_offsets(sources, policy)
            staged_sources = _apply_policy_fragments(
                sources, policy, local_offsets)
            for module_name, module_path in policy.hosts:
                compile(staged_sources[module_name].decode("utf-8"),
                        module_path, "exec", optimize=0, dont_inherit=True)
            local_recipe_bytes = _build_recipe(
                game_version or "unknown", sources, staged_sources,
                policy, local_offsets)
        except (OSError, UnicodeError, SyntaxError, VersionBindingError,
                HookRecipeError) as error:
            raise CandidateBuildError(
                f"compatibility test could not safely adapt this game build: {error}") from error

    work_root = Path(work_root).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    transaction_root = work_root / uuid.uuid4().hex
    transaction_root.mkdir()
    try:
        payload_root = package.extract_payload(transaction_root / "payload")
        recipes = [entry for entry in package.payloads
                   if entry.path.suffix.lower() == ".hook"]
        if len(recipes) != 1:
            raise CandidateBuildError(
                "signed package must contain exactly one integration recipe")
        recipe_path = payload_root / Path(*recipes[0].path.parts[1:])
        try:
            recipe_bytes = recipe_path.read_bytes()
            recipe = parse_hook_recipe(recipe_bytes)
        except (OSError, HookRecipeError) as error:
            raise CandidateBuildError("integration recipe failed verification") from error
        if recipe.game_version != compatibility.game_version:
            raise CandidateBuildError("integration recipe targets another game version")
        if "Client" not in {module.module for module in recipe.modules}:
            raise CandidateBuildError("integration recipe must produce Client.py")
        if profile is not None:
            try:
                validate_recipe_for_profile(recipe, profile)
                if package.release_binding is not None:
                    binding_entries = [
                        entry for entry in package.payloads
                        if entry.path.name.endswith(".binding.json")]
                    if len(binding_entries) != 1:
                        raise ReleaseProfileError(
                            "dynamic package binding inventory changed")
                    binding_path = payload_root / Path(
                        *binding_entries[0].path.parts[1:])
                    derived = profile_from_signed_binding(
                        binding_path.read_bytes(), recipe_bytes)
                    if (derived.profile_id != profile.profile_id
                            or dict(derived.fragment_offsets)
                            != dict(profile.fragment_offsets)
                            or derived.recipe_sha256 != profile.recipe_sha256):
                        raise ReleaseProfileError(
                            "dynamic package binding changed after verification")
            except ReleaseProfileError as error:
                raise CandidateBuildError(
                    "integration recipe is outside its release profile") from error

        signed_recipe_bytes = recipe_bytes
        if compatibility_test:
            if local_recipe_bytes is None:
                raise CandidateBuildError(
                    "compatibility test did not produce a local integration recipe")
            try:
                recipe_bytes = local_recipe_bytes
                recipe = parse_hook_recipe(recipe_bytes)
            except HookRecipeError as error:
                raise CandidateBuildError(
                    "compatibility test recipe failed verification") from error

        stage_internal = transaction_root / "stage" / "_internal"
        try:
            recipe.apply_to(game_root / "_internal", stage_internal)
        except HookRecipeError as error:
            raise CandidateBuildError(str(error)) from error

        payload_directory = (
            profile.payload_directory if profile is not None else "ui_mod")
        source_package = (
            profile.source_package if profile is not None
            else "star_empire_ui_mod")
        authored_source = payload_root / payload_directory
        if not (authored_source / "__init__.py").is_file():
            raise CandidateBuildError(
                f"package is missing {payload_directory}/__init__.py")
        authored_target = stage_internal / Path(*source_package.split("."))
        authored_target.mkdir()
        module_names = [module.module for module in recipe.modules]
        registered = _registered_modules()
        sources = (
            tuple(authored_source / name for name in profile.ui_source_files)
            if profile is not None
            else tuple(sorted(authored_source.rglob("*")))
        )
        for source in sources:
            if not source.is_file():
                if profile is not None:
                    raise CandidateBuildError(
                        f"profiled authored module is missing: {source.name}")
                continue
            relative = source.relative_to(authored_source)
            module_name = _authored_module_name(relative, source_package)
            if module_name not in registered:
                raise CandidateBuildError(
                    f"authored module is not registered with the repacker: {module_name}")
            destination = authored_target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            module_names.append(module_name)
        selected = tuple(dict.fromkeys(module_names))
        if any(name not in registered for name in selected):
            raise CandidateBuildError("recipe selected an unregistered frozen module")
        if (profile is not None
                and selected != (
                    *tuple(profile.host_input_sha256),
                    *profile.frozen_module_names)):
            raise CandidateBuildError(
                "frozen module selection is outside the release profile")

        candidate = transaction_root / "candidate" / CLIENT_EXE
        candidate.parent.mkdir()
        repack(client, stage_internal / "Client.py", candidate, selected)
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise CandidateBuildError("repacker did not produce a candidate Client.exe")
        candidate_sha256 = sha256_file(candidate)
        if (not compatibility_test
                and candidate_sha256 != compatibility.expected_client_sha256):
            raise CandidateBuildError(
                "rebuilt Client.exe does not match the signed expected SHA-256")

        audit = transaction_root / "build-audit.json"
        audit_payload = {
            "schema": 1,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "game_root": str(game_root),
            "baseline_client": str(client),
            "official_client_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
            "pack_id": compatibility.pack_id,
            "pack_digest": compatibility.pack_digest,
            "key_id": compatibility.key_id,
            "compatibility_test": compatibility_test,
            "observed_game_version": game_version,
            "signed_official_client_sha256": compatibility.official_client_sha256,
            "signed_expected_client_sha256": compatibility.expected_client_sha256,
            "local_fragment_offsets": local_offsets,
            "release_profile": (
                profile.profile_id if profile is not None else None),
            "recipe_sha256": hashlib.sha256(
                recipe_bytes).hexdigest().upper(),
            "signed_recipe_sha256": hashlib.sha256(
                signed_recipe_bytes).hexdigest().upper(),
            "authored_fragments": sorted((
                {
                    "fragment_id": operation.fragment_id,
                    "sha256": operation.fragment_sha256,
                    "module": module.module,
                }
                for module in recipe.modules
                for operation in module.operations
                if isinstance(operation, InsertFragmentOperation)
            ), key=lambda item: (item["module"], item["fragment_id"])),
            "modules": list(selected),
        }
        pending = audit.with_name(audit.name + ".pending")
        pending.write_text(
            json.dumps(audit_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        pending.replace(audit)
        return BuiltCandidate(
            candidate, candidate_sha256, baseline_sha256,
            compatibility.pack_id, compatibility.pack_digest,
            compatibility.key_id, selected, audit,
            compatibility_test, game_version, transaction_root)
    except Exception:
        shutil.rmtree(transaction_root, ignore_errors=True)
        raise
