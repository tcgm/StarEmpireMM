"""Closed release profiles for game-code-free public packages."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .authored_fragments import (
    CLIENT_ALPHA_TARGET_BOOTSTRAP_V1,
    CLIENT_ALPHA_TARGET_EVENT_V1,
    CLIENT_ALPHA_TARGET_INSTALL_V1,
    CLIENT_ALPHA_TARGET_OVERLAY_V1,
    CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1,
    CLIENT_ALPHA_TARGET_VITALS_EVENT_V1,
    CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1,
    CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1,
    CLIENT_FULL_UI_BEGIN_FRAME_V1,
    CLIENT_FULL_UI_BOOTSTRAP_V1,
    CLIENT_FULL_UI_EVENT_V1,
    CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4,
    CLIENT_FULL_UI_OVERLAY_V1,
    CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
    CLIENT_MOD_LOADER_BOOTSTRAP_V1,
    CLIENT_MOD_LOADER_EVENT_V1,
    CLIENT_MOD_LOADER_INSTALL_V1,
    CLIENT_MOD_LOADER_OVERLAY_V1,
    RENDER_MOD_LOADER_REGION_V1,
    resolve_active_authored_fragment,
)
from .hook_recipe import (HOST_MODULE_PATHS, HookRecipe, HookRecipeError,
                          InsertFragmentOperation, parse_hook_recipe)


TARGET_ALPHA_PROFILE_ID = "target-alpha-0.4.42-v1"
TARGET_VITALS_ALPHA_PROFILE_ID = "target-vitals-alpha-0.4.42-v1"
TARGET_VITALS_ALPHA_POLICY_ID = "target-vitals-alpha-v1"
FULL_UI_POLICY_ID = "full-ui-v1"
MOD_LOADER_POLICY_ID = "mod-loader-v1"
BINDING_SCHEMA = 2
LEGACY_BINDING_SCHEMA = 1
BINDING_FORMAT = "star-empire-ui-release-binding"
MAX_BINDING_BYTES = 128 * 1024


class ReleaseProfileError(ValueError):
    """Raised when package inputs exceed their reviewed public profile."""


def _frozen_module_names(
        source_files: tuple[str, ...],
        source_package: str = "star_empire_ui_mod",
) -> tuple[str, ...]:
    return tuple(
        source_package if name == "__init__.py"
        else f"{source_package}.{name[:-3]}"
        for name in source_files
    )


@dataclass(frozen=True)
class ReleasePolicy:
    """Version-neutral limits for one reviewed public feature set."""

    policy_id: str
    host_module: str
    host_path: str
    fragment_ids: tuple[str, ...]
    ui_source_files: tuple[str, ...]
    source_package: str = "star_empire_ui_mod"
    payload_directory: str = "ui_mod"
    additional_hosts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        hosts = self.hosts
        if (not self.policy_id or not hosts
                or hosts[0] != ("Client", "Client.py")
                or len({name for name, _path in hosts}) != len(hosts)
                or any(HOST_MODULE_PATHS.get(name) != path
                       for name, path in hosts)):
            raise ReleaseProfileError("release policy host boundary is invalid")
        if (not self.fragment_ids
                or len(set(self.fragment_ids)) != len(self.fragment_ids)):
            raise ReleaseProfileError("release policy fragments must be unique")
        if (not self.ui_source_files
                or self.ui_source_files[0] != "__init__.py"
                or len(set(self.ui_source_files)) != len(self.ui_source_files)
                or any(not name.endswith(".py") or "/" in name or "\\" in name
                       for name in self.ui_source_files)):
            raise ReleaseProfileError("release policy UI source inventory is invalid")
        fragment_hosts = {
            resolve_active_authored_fragment(fragment_id).module
            for fragment_id in self.fragment_ids
        }
        if fragment_hosts != {name for name, _path in hosts}:
            raise ReleaseProfileError(
                "release policy fragment host inventory is invalid")
        if (not self.source_package
                or any(not part.isidentifier()
                       for part in self.source_package.split("."))
                or not self.payload_directory.isidentifier()):
            raise ReleaseProfileError(
                "release policy authored source namespace is invalid")

    @property
    def frozen_module_names(self) -> tuple[str, ...]:
        return _frozen_module_names(
            self.ui_source_files, self.source_package)

    @property
    def hosts(self) -> tuple[tuple[str, str], ...]:
        return ((self.host_module, self.host_path), *self.additional_hosts)


@dataclass(frozen=True)
class ReleaseBinding:
    """Authenticated per-build data constrained by a compiled release policy."""

    policy: ReleasePolicy
    profile_id: str
    game_version: str
    official_client_sha256: str
    expected_client_sha256: str
    host_input_sha256: Mapping[str, str]
    host_output_sha256: Mapping[str, str]
    fragment_offsets: Mapping[str, int]
    ui_source_files: tuple[str, ...]
    ui_source_sha256: Mapping[str, str]
    recipe_file: str
    recipe_sha256: str


@dataclass(frozen=True)
class ReleaseProfile:
    profile_id: str
    game_version: str
    official_client_sha256: str
    host_input_sha256: Mapping[str, str]
    fragment_offsets: Mapping[str, int]
    ui_source_files: tuple[str, ...]
    source_package: str = "star_empire_ui_mod"
    payload_directory: str = "ui_mod"
    host_output_sha256: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))
    recipe_sha256: str = ""
    ui_source_sha256: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))

    @property
    def fragment_ids(self) -> frozenset[str]:
        return frozenset(self.fragment_offsets)

    @property
    def frozen_module_names(self) -> tuple[str, ...]:
        return _frozen_module_names(
            self.ui_source_files, self.source_package)


TARGET_ALPHA_0_4_42 = ReleaseProfile(
    profile_id=TARGET_ALPHA_PROFILE_ID,
    game_version="0.4.42",
    official_client_sha256=(
        "340D702AB960B01505C07E6C367259F3C543C7B275B9FD83E7C5FADC2AB6ED06"
    ),
    host_input_sha256=MappingProxyType({
        "Client": (
            "BAD3E4F33C1890A05053DADA68D57114358D79AC4BDB49690403FE1527B821B6"
        ),
    }),
    fragment_offsets=MappingProxyType({
        CLIENT_ALPHA_TARGET_BOOTSTRAP_V1: 657165,
        CLIENT_ALPHA_TARGET_EVENT_V1: 677867,
        CLIENT_ALPHA_TARGET_OVERLAY_V1: 1469901,
        CLIENT_ALPHA_TARGET_INSTALL_V1: 1647595,
    }),
    ui_source_files=(
        "__init__.py",
        "asset_runtime.py",
        "catalog.py",
        "config.py",
        "control_centre.py",
        "dock.py",
        "host_alpha_client.py",
        "host_method_bridge.py",
        "host_target.py",
        "kitty_assets.py",
        "layout.py",
        "legacy_panels.py",
        "nocturne_assets.py",
        "pygame_control_centre.py",
        "pygame_dock.py",
        "pygame_legacy_panels.py",
        "pygame_window.py",
        "runtime.py",
        "skin_assets.py",
        "theme.py",
        "windowing.py",
        "workspace.py",
    ),
    host_output_sha256=MappingProxyType({
        "Client": (
            "EF27A937269F580F5EA16372E18C8F6561AEBC4FC9F24AB3EAAC4AA84BD0C7E1"
        ),
    }),
    recipe_sha256=(
        "B9191973E0DF018CA61CB644C19ED0851204EF5CCF6ADCC34B347AEF1636CE18"
    ),
)

TARGET_VITALS_ALPHA_0_4_42 = ReleaseProfile(
    profile_id=TARGET_VITALS_ALPHA_PROFILE_ID,
    game_version="0.4.42",
    official_client_sha256=(
        "340D702AB960B01505C07E6C367259F3C543C7B275B9FD83E7C5FADC2AB6ED06"
    ),
    host_input_sha256=MappingProxyType({
        "Client": (
            "BAD3E4F33C1890A05053DADA68D57114358D79AC4BDB49690403FE1527B821B6"
        ),
    }),
    fragment_offsets=MappingProxyType({
        CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1: 657165,
        CLIENT_ALPHA_TARGET_VITALS_EVENT_V1: 677867,
        CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1: 1469901,
        CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1: 1647595,
    }),
    ui_source_files=(
        "__init__.py",
        "asset_runtime.py",
        "catalog.py",
        "config.py",
        "content.py",
        "control_centre.py",
        "dock.py",
        "host_alpha_client.py",
        "host_method_bridge.py",
        "host_target.py",
        "host_target_vitals_client.py",
        "host_vitals.py",
        "kitty_assets.py",
        "layout.py",
        "legacy_panels.py",
        "nocturne_assets.py",
        "pygame_content.py",
        "pygame_control_centre.py",
        "pygame_dock.py",
        "pygame_flight_hud.py",
        "pygame_legacy_panels.py",
        "pygame_window.py",
        "pygame_workspace.py",
        "runtime.py",
        "skin_assets.py",
        "theme.py",
        "windowing.py",
        "workspace.py",
    ),
    host_output_sha256=MappingProxyType({
        "Client": (
            "C276A76AE45C225ACF4484CD5C3997C3D1B6C5ACC8A98BE3C7E08FC8A704B627"
        ),
    }),
    recipe_sha256=(
        "967FC10EBA119D08DAC57E56DFB93A4FB174C8AE229BBEC87DACC287832A3DF3"
    ),
)


TARGET_VITALS_ALPHA_POLICY = ReleasePolicy(
    policy_id=TARGET_VITALS_ALPHA_POLICY_ID,
    host_module="Client",
    host_path="Client.py",
    fragment_ids=(
        CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1,
        CLIENT_ALPHA_TARGET_VITALS_EVENT_V1,
        CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1,
        CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1,
    ),
    ui_source_files=TARGET_VITALS_ALPHA_0_4_42.ui_source_files,
)

FULL_UI_POLICY = ReleasePolicy(
    policy_id=FULL_UI_POLICY_ID,
    host_module="Client",
    host_path="Client.py",
    fragment_ids=(
        CLIENT_FULL_UI_BOOTSTRAP_V1,
        CLIENT_FULL_UI_EVENT_V1,
        CLIENT_FULL_UI_BEGIN_FRAME_V1,
        CLIENT_FULL_UI_OVERLAY_V1,
        CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4,
    ),
    ui_source_files=(
        "__init__.py",
        "asset_runtime.py",
        "catalog.py",
        "config.py",
        "content.py",
        "control_centre.py",
        "dock.py",
        "host_captain.py",
        "host_chat.py",
        "host_client_content.py",
        "host_client_core.py",
        "host_client_input.py",
        "host_client_lifecycle.py",
        "host_client_render.py",
        "host_coalition.py",
        "host_coalition_client.py",
        "host_context_menu.py",
        "host_flight_controls.py",
        "host_friends.py",
        "host_full_install.py",
        "host_galaxy.py",
        "host_game_surfaces.py",
        "host_hangar_context.py",
        "host_item_search.py",
        "host_legacy_panels.py",
        "host_method_bridge.py",
        "host_minimap.py",
        "host_missions.py",
        "host_rc_bot_operations.py",
        "host_simple_modals.py",
        "host_squad.py",
        "host_stasis.py",
        "host_station.py",
        "host_station_dialogs.py",
        "host_target.py",
        "host_turret_control.py",
        "host_turret_pickers.py",
        "host_vitals.py",
        "kitty_assets.py",
        "layout.py",
        "legacy_panels.py",
        "nocturne_assets.py",
        "pygame_chat.py",
        "pygame_content.py",
        "pygame_control_centre.py",
        "pygame_dock.py",
        "pygame_flight_hud.py",
        "pygame_legacy_panels.py",
        "pygame_window.py",
        "pygame_workspace.py",
        "runtime.py",
        "skin_assets.py",
        "theme.py",
        "windowing.py",
        "workspace.py",
    ),
)

MOD_LOADER_POLICY = ReleasePolicy(
    policy_id=MOD_LOADER_POLICY_ID,
    host_module="Client",
    host_path="Client.py",
    fragment_ids=(
        CLIENT_MOD_LOADER_BOOTSTRAP_V1,
        CLIENT_MOD_LOADER_EVENT_V1,
        CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
        CLIENT_MOD_LOADER_OVERLAY_V1,
        CLIENT_MOD_LOADER_INSTALL_V1,
        RENDER_MOD_LOADER_REGION_V1,
    ),
    ui_source_files=(
        "__init__.py",
        "host_client.py",
        "runtime.py",
    ),
    source_package="star_empire_mod_loader",
    payload_directory="mod_loader",
    additional_hosts=(("render_mixin", "render_mixin.py"),),
)

RELEASE_POLICIES: Mapping[str, ReleasePolicy] = MappingProxyType({
    TARGET_VITALS_ALPHA_POLICY_ID: TARGET_VITALS_ALPHA_POLICY,
    FULL_UI_POLICY_ID: FULL_UI_POLICY,
    MOD_LOADER_POLICY_ID: MOD_LOADER_POLICY,
})

RELEASE_PROFILES: Mapping[str, ReleaseProfile] = MappingProxyType({
    TARGET_ALPHA_PROFILE_ID: TARGET_ALPHA_0_4_42,
    TARGET_VITALS_ALPHA_PROFILE_ID: TARGET_VITALS_ALPHA_0_4_42,
})


def release_profile(profile_id: str) -> ReleaseProfile:
    try:
        return RELEASE_PROFILES[str(profile_id)]
    except KeyError as error:
        raise ReleaseProfileError(
            f"unknown release profile: {profile_id}") from error


def release_policy(policy_id: str) -> ReleasePolicy:
    try:
        return RELEASE_POLICIES[str(policy_id)]
    except KeyError as error:
        raise ReleaseProfileError(
            f"unknown release policy: {policy_id}") from error


def _binding_unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseProfileError(f"duplicate release-binding key: {key}")
        result[key] = value
    return result


def _binding_digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ReleaseProfileError(f"{label} must be a SHA-256 value")
    digest = value.upper()
    if (len(digest) != 64
            or any(character not in "0123456789ABCDEF" for character in digest)):
        raise ReleaseProfileError(f"{label} must be a SHA-256 value")
    return digest


def _binding_profile_id(policy: ReleasePolicy, game_version: str,
                        official_client_sha256: str) -> str:
    if (not game_version
            or any(character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_"
                   for character in game_version)):
        raise ReleaseProfileError("release-binding game version is invalid")
    base = (policy.policy_id[:-3]
            if policy.policy_id.endswith("-v1") else policy.policy_id)
    return f"{base}-{game_version}-{official_client_sha256[:12].lower()}-v1"


def parse_release_binding(payload: bytes) -> ReleaseBinding:
    """Validate exact source-free binding bytes after package authentication."""
    if not payload or len(payload) > MAX_BINDING_BYTES:
        raise ReleaseProfileError("release binding size is outside limits")
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_binding_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseProfileError(
            "release binding is not unique-key UTF-8 JSON") from error
    common_fields = {
        "schema", "format", "policy_id", "profile_id", "game_version",
        "official_client_sha256", "expected_client_sha256",
        "host_input_sha256", "host_output_sha256", "fragment_offsets",
        "ui_source_files", "recipe_file", "recipe_sha256",
    }
    if not isinstance(data, dict):
        raise ReleaseProfileError("release binding must be a JSON object")
    schema = data.get("schema")
    if (type(schema) is not int
            or schema not in (LEGACY_BINDING_SCHEMA, BINDING_SCHEMA)):
        raise ReleaseProfileError("unsupported release-binding format")
    required = (common_fields | {"ui_source_sha256"}
                if schema == BINDING_SCHEMA else common_fields)
    if not isinstance(data, dict) or set(data) != required:
        raise ReleaseProfileError(
            "release binding has unknown or missing top-level fields")
    if (type(data["format"]) is not str
            or data["format"] != BINDING_FORMAT):
        raise ReleaseProfileError("unsupported release-binding format")
    if type(data["policy_id"]) is not str:
        raise ReleaseProfileError("release-binding policy ID must be a string")
    policy = release_policy(data["policy_id"])
    if type(data["game_version"]) is not str:
        raise ReleaseProfileError("release-binding game version must be a string")
    game_version = data["game_version"].strip()
    official_client_digest = _binding_digest(
        data["official_client_sha256"], "release-binding official client")
    expected_profile_id = _binding_profile_id(
        policy, game_version, official_client_digest)
    if type(data["profile_id"]) is not str or data["profile_id"] != expected_profile_id:
        raise ReleaseProfileError("release-binding profile ID is not canonical")

    host_input = data["host_input_sha256"]
    host_output = data["host_output_sha256"]
    host_names = tuple(name for name, _path in policy.hosts)
    if (not isinstance(host_input, dict) or set(host_input) != set(host_names)
            or not isinstance(host_output, dict)
            or set(host_output) != set(host_names)):
        raise ReleaseProfileError("release-binding host inventory is invalid")
    ordered_host_input = MappingProxyType({
        name: _binding_digest(
            host_input[name], f"release-binding host input {name}")
        for name in host_names
    })
    ordered_host_output = MappingProxyType({
        name: _binding_digest(
            host_output[name], f"release-binding host output {name}")
        for name in host_names
    })

    offsets = data["fragment_offsets"]
    if not isinstance(offsets, dict) or set(offsets) != set(policy.fragment_ids):
        raise ReleaseProfileError("release-binding fragment inventory is invalid")
    if any(type(value) is not int or value < 0 for value in offsets.values()):
        raise ReleaseProfileError("release-binding fragment offset is invalid")
    for host_name in host_names:
        host_offsets = [
            offsets[fragment_id] for fragment_id in policy.fragment_ids
            if resolve_active_authored_fragment(fragment_id).module == host_name
        ]
        if len(set(host_offsets)) != len(host_offsets):
            raise ReleaseProfileError(
                "release-binding fragment offsets are duplicated")
    ordered_offsets = MappingProxyType({
        fragment_id: offsets[fragment_id] for fragment_id in policy.fragment_ids
    })

    ui_source_files = data["ui_source_files"]
    if (not isinstance(ui_source_files, list)
            or any(type(item) is not str for item in ui_source_files)
            or tuple(ui_source_files) != policy.ui_source_files):
        raise ReleaseProfileError("release-binding UI inventory is outside policy")
    if schema == BINDING_SCHEMA:
        ui_source_hashes = data["ui_source_sha256"]
        if (not isinstance(ui_source_hashes, dict)
                or set(ui_source_hashes) != set(policy.ui_source_files)):
            raise ReleaseProfileError(
                "release-binding UI hash inventory is outside policy")
        ordered_ui_source_hashes = MappingProxyType({
            name: _binding_digest(
                ui_source_hashes[name],
                f"release-binding UI source {name}")
            for name in policy.ui_source_files
        })
    else:
        ordered_ui_source_hashes = MappingProxyType({})
    recipe_file = data["recipe_file"]
    if (type(recipe_file) is not str
            or recipe_file != f"{expected_profile_id}.hook"):
        raise ReleaseProfileError("release-binding recipe filename is invalid")

    return ReleaseBinding(
        policy=policy,
        profile_id=expected_profile_id,
        game_version=game_version,
        official_client_sha256=official_client_digest,
        expected_client_sha256=_binding_digest(
            data["expected_client_sha256"], "release-binding expected client"),
        host_input_sha256=ordered_host_input,
        host_output_sha256=ordered_host_output,
        fragment_offsets=ordered_offsets,
        ui_source_files=tuple(ui_source_files),
        ui_source_sha256=ordered_ui_source_hashes,
        recipe_file=recipe_file,
        recipe_sha256=_binding_digest(
            data["recipe_sha256"], "release-binding recipe"),
    )


def profile_from_signed_binding(
        binding_payload: bytes, recipe_payload: bytes) -> ReleaseProfile:
    """Derive a closed profile from package-authenticated binding and recipe."""
    binding = parse_release_binding(binding_payload)
    if hashlib.sha256(recipe_payload).hexdigest().upper() != binding.recipe_sha256:
        raise ReleaseProfileError("release-binding recipe hash does not match")
    try:
        recipe = parse_hook_recipe(recipe_payload)
    except HookRecipeError as error:
        raise ReleaseProfileError("release-binding recipe is invalid") from error
    profile = ReleaseProfile(
        profile_id=binding.profile_id,
        game_version=binding.game_version,
        official_client_sha256=binding.official_client_sha256,
        host_input_sha256=binding.host_input_sha256,
        fragment_offsets=binding.fragment_offsets,
        ui_source_files=binding.ui_source_files,
        source_package=binding.policy.source_package,
        payload_directory=binding.policy.payload_directory,
        host_output_sha256=binding.host_output_sha256,
        recipe_sha256=binding.recipe_sha256,
        ui_source_sha256=binding.ui_source_sha256,
    )
    validate_recipe_for_profile(recipe, profile)
    return profile


def validate_recipe_for_profile(
        recipe: HookRecipe,
        profile: ReleaseProfile = TARGET_ALPHA_0_4_42) -> None:
    """Require the exact reviewed module, fragments and official offsets."""
    if recipe.game_version != profile.game_version:
        raise ReleaseProfileError("recipe game version is outside release profile")
    expected_modules = tuple(profile.host_input_sha256)
    if tuple(module.module for module in recipe.modules) != expected_modules:
        raise ReleaseProfileError(
            "release recipe host inventory is outside release profile")
    actual: dict[str, int] = {}
    for module in recipe.modules:
        if (module.path != HOST_MODULE_PATHS.get(module.module)
                or module.input_sha256
                != profile.host_input_sha256[module.module]):
            raise ReleaseProfileError(
                "recipe host baseline is outside release profile")
        expected_output = profile.host_output_sha256.get(module.module)
        if expected_output and module.output_sha256 != expected_output:
            raise ReleaseProfileError(
                "recipe host output is outside release profile")
        for operation in module.operations:
            if not isinstance(operation, InsertFragmentOperation):
                raise ReleaseProfileError(
                    "release recipe may only insert fragments")
            if operation.fragment_id in actual:
                raise ReleaseProfileError("release fragment is duplicated")
            actual[operation.fragment_id] = operation.offset
    if actual != dict(profile.fragment_offsets):
        raise ReleaseProfileError(
            "recipe fragments or official offsets are outside release profile")
