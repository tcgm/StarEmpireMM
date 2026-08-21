"""Safely rebuild the UI Mod's frozen modules in a PyInstaller executable.

Star Empire's ``Client.exe`` is a PyInstaller one-directory launcher.  The
runtime entry point is stored in its CArchive, so changing ``_internal/Client.py``
alone does not update the executable.  ``protocol.py`` also imports ``Client``
at runtime, which resolves to a second copy stored in the embedded PYZ.  This
tool preserves the original bootloader and every archived dependency while
replacing the compiled UI Mod modules from the supplied source tree.

It deliberately writes a new output executable; the caller is responsible for
backing up and replacing the existing executable only after verification.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import marshal
import struct
import sys
import zlib
from pathlib import Path
from types import CodeType
from typing import Iterable, Mapping

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter


CLIENT_ENTRY = "Client"
PYZ_ENTRY = "PYZ.pyz"
PYZ_MAGIC = b"PYZ\0"
PYZ_MODULE = 0
PYZ_PACKAGE = 1
PYZ_NSPKG = 3
REQUIRED_SOURCE_MARKERS: dict[str, tuple[str, ...]] = {
    "Client": (),
    # The generic Target and Inventory renderer lives in PYZ rather than the
    # outer CArchive.  Repack it with Client or the visibility state is saved
    # but an old frozen renderer continues drawing those panels.
    "render_mixin": (
        "def _draw_panel",
    ),
    # Ship Cargo is rendered by a dedicated mixin, including its transferable
    # item rows and child Toss popover. Keep it in the explicit UI boundary so
    # loose-source changes cannot silently differ from the frozen client.
    "hangar_inventory": (
        "class HangarInventoryMixin",
        "def _hinv_draw_ctx_menu",
    ),
    # Galaxy Map primitives are GPU-native, so their z-order fix must update
    # the frozen renderer as well as the loose source beside the executable.
    "gl_renderer": (
        "def present_world",
        "canvas_above",
        "map_connectors",
        "map_labels",
    ),
    # Item Locator is a global game-owned modal in its own mixin. Register it
    # explicitly so a private UI shell change cannot be left loose while the
    # frozen client keeps executing the previous copy.
    "item_search": (
        "class ItemSearchMixin",
        "def _draw_item_search",
    ),
    "star_empire_ui_mod.content": (
        "class ContentMetrics",
        "class ContentRow",
    ),
    "star_empire_ui_mod.pygame_content": (
        "class PygameContentRenderer",
        "def draw_tabs",
    ),
    "star_empire_ui_mod": (
        "Reusable, configurable UI foundation",
    ),
    "star_empire_ui_mod.catalog": (
        "class MigrationStage",
        "WINDOW_CATALOG",
    ),
    "star_empire_ui_mod.config": (
        "class UiSettings",
    ),
    "star_empire_ui_mod.control_centre": (
        "class ControlCentre",
        "migration = entry_for",
    ),
    "star_empire_ui_mod.dock": (
        "class DockSlot",
        "DOCK_THICKNESS",
    ),
    "star_empire_ui_mod.layout": (
        "class WindowLayout",
        "class LayoutProfile",
    ),
    "star_empire_ui_mod.legacy_panels": (
        "def import_legacy_flight_panels",
        "def export_flight_panels",
    ),
    "star_empire_ui_mod.host_method_bridge": (
        "class HostBridgeError",
        "def install_bridge_methods",
        "def install_bridge_bundle",
    ),
    "star_empire_ui_mod.host_alpha_client": (
        "class AlphaClientBridge",
        "def _initialize_ui_mod_target_alpha",
        "def _handle_ui_mod_target_alpha_event",
        "def _draw_ui_mod_target_alpha",
    ),
    "star_empire_ui_mod.host_chat": (
        "class ChatRendererBridge",
        "def render_chat_window",
        "def _clear_chat_geometry",
    ),
    "star_empire_ui_mod.host_client_core": (
        "class FullClientCoreBridge",
        "def disable_full_ui",
        "def initialize_full_ui",
    ),
    "star_empire_ui_mod.host_client_content": (
        "class FullClientContentBridge",
        "def route_full_ui_content_event",
        "def mask_non_owner_geometry",
    ),
    "star_empire_ui_mod.host_client_input": (
        "class FullClientInputBridge",
        "def route_full_ui_input",
    ),
    "star_empire_ui_mod.host_client_lifecycle": (
        "class GameAuthorityBinding",
        "def sync_game_authority_windows",
        "GAME_AUTHORITY_BINDINGS",
    ),
    "star_empire_ui_mod.host_client_render": (
        "class FullClientRenderBridge",
        "class FullUiDrawScheduler",
        "def flush_full_ui_frame",
    ),
    "star_empire_ui_mod.host_full_install": (
        "def install_full_framework",
        "_ALIASES",
    ),
    "star_empire_ui_mod.host_flight_controls": (
        "class FlightControlsRendererBridge",
        "def render_quick_actions_window",
        "QUICK_ACTIONS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_friends": (
        "class FriendsRendererBridge",
        "def render_friends_window",
        "FRIENDS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_legacy_panels": (
        "class LegacyPanelRendererBridge",
        "def render_legacy_panel",
        "def _translate_hit_mapping",
    ),
    "star_empire_ui_mod.host_minimap": (
        "class MinimapRendererBridge",
        "def render_minimap_window",
        "def clear_minimap_geometry",
    ),
    "star_empire_ui_mod.host_missions": (
        "class MissionRendererBridge",
        "class MissionDetailsRendererBridge",
        "def render_mission_log_window",
        "def render_mission_details_window",
        "MISSION_LOG_WINDOW_ID",
        "MISSION_DETAILS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_captain": (
        "class CaptainRendererBridge",
        "def render_captain_window",
        "def register_captain_window",
        "def clear_captain_geometry",
        "def _draw_stats_panel",
        "CAPTAIN_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_game_surfaces": (
        "class GameSurfacesRendererBridge",
        "def render_game_surfaces",
        "def clear_game_surface_geometry",
        "def _draw_esc_menu",
        "GAME_MENU_WINDOW_ID",
        "OPTIONS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_galaxy": (
        "class GalaxyRendererBridge",
        "class GalaxyOverlayProxy",
        "def render_galaxy_window",
        "def register_galaxy_window",
        "def prepare_galaxy_foreground",
        "def clear_galaxy_geometry",
        "def _draw_galaxy_map",
        "GALAXY_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_item_search": (
        "class ItemLocatorRendererBridge",
        "def render_item_locator",
        "def clear_item_locator_geometry",
        "def _draw_item_search",
        "ITEM_LOCATOR_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_hangar_context": (
        "class HangarContextRendererBridge",
        "def render_station_hangar_context",
        "def render_ship_inventory_context",
        "def _draw_hangar_ctx_menu",
        "def _hinv_draw_ctx_menu",
    ),
    "star_empire_ui_mod.host_rc_bot_operations": (
        "class RcBotOperationsRendererBridge",
        "def render_rc_bot_operations",
        "def register_rc_bot_operations",
        "def clear_rc_bot_operations_geometry",
        "def _draw_rc_bot_haul_panel",
        "RC_BOT_OPERATIONS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_simple_modals": (
        "class FixedModalSpec",
        "class SimpleModalRendererBridge",
        "def render_fixed_modal",
        "def _draw_ps_transfer_dialog",
        "def _draw_ship_picker",
        "def _draw_ps_trade_manager",
        "def _draw_ps_prefab_dialog",
        "def _draw_ps_shuttle_dialog",
        "def _draw_asset_transfer_panel",
        "def _draw_hangar_sell_confirm",
        "def _draw_ps_income_chart",
        "def _draw_ps_destroy_confirm",
        "def _draw_change_name_dialog",
        "FIXED_MODAL_SPECS",
    ),
    "star_empire_ui_mod.host_target": (
        "class TargetRendererBridge",
        "def render_target_panel",
        "def _draw_panel",
    ),
    "star_empire_ui_mod.host_stasis": (
        "class StasisRendererBridge",
        "def render_stasis_window",
        "def _draw_stasis_gui",
        "STASIS_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_squad": (
        "class SquadClientBridge",
        "def sync_squad_windows",
        "def draw_squad_main",
        "def draw_squad_invite",
        "def register_squad_windows",
    ),
    "star_empire_ui_mod.host_station": (
        "class StationRendererBridge",
        "def sync_station_window",
        "def render_station_window",
        "def register_station_window",
        "register_station_dialogs",
        "suppress_inline_station_dialogs",
        "STATION_WINDOW_ID",
    ),
    "star_empire_ui_mod.host_station_dialogs": (
        "STATION_DIALOG_FIELDS",
        "def suppress_inline_station_dialogs",
        "def restore_inline_station_dialogs",
        "def render_station_dialog",
        "def register_station_dialogs",
    ),
    "star_empire_ui_mod.host_turret_pickers": (
        "class TurretPickerRendererBridge",
        "def render_turret_pickers",
        "def _draw_turret_pickers",
        "WEAPON_PICKER_ID",
        "UPGRADE_PICKER_ID",
    ),
    "star_empire_ui_mod.host_turret_control": (
        "class TurretControlRendererBridge",
        "def render_turret_control",
        "def _draw_turret_control_panel",
        "TURRET_CONTROL_ID",
    ),
    "star_empire_ui_mod.host_target_vitals_client": (
        "class TargetVitalsClientBridge",
        "def initialize_target_vitals_alpha",
        "def handle_target_vitals_alpha_event",
        "def draw_target_vitals_alpha",
    ),
    "star_empire_ui_mod.host_vitals": (
        "class VitalsRendererBridge",
        "def capture_vitals_snapshot",
        "def render_vitals_window",
        "def _draw_hud_top_panel",
    ),
    "star_empire_ui_mod.host_context_menu": (
        "class SharedContextMenuBridge",
        "def _draw_shared_context_menu",
    ),
    "star_empire_ui_mod.host_coalition": (
        "class CoalitionRendererBridge",
        "def _draw_coalition_panel",
        "def _coalition_shared_content_context",
        "def _coalition_framework_rank_data",
        "def _draw_coalition_framework_online",
        "def _draw_coalition_framework_main",
        "def _draw_coalition_framework_windows",
        "def _draw_coalition_framework_permissions",
        "def _draw_coalition_framework_permission_editor",
        "def _draw_coalition_framework_skills",
        "def _draw_coalition_framework_colour",
        "def _draw_coalition_framework_wars",
        "def _draw_coalition_framework_map_sync",
        "def _draw_coalition_framework_overlays",
        "def _draw_coalition_framework_modal",
        "def _draw_coalition_framework_popovers",
    ),
    "star_empire_ui_mod.host_coalition_client": (
        "class CoalitionClientBridge",
        "def _initialize_ui_mod_coalition_state",
        "def _coalition_window_ids",
        "def _queue_coalition_window_visibility",
        "def _clear_coalition_surface_geometry",
        "def _apply_coalition_window_visibility",
        "def _set_coalition_window_visible",
        "def _sync_coalition_window_states",
    ),
    "star_empire_ui_mod.pygame_chat": (
        "class PygameChatWindow",
        "theme: Any = DEFAULT_THEME",
    ),
    "star_empire_ui_mod.pygame_control_centre": (
        "class PygameControlCentre",
        "MIGRATION_BADGES",
    ),
    "star_empire_ui_mod.pygame_dock": (
        "class PygameDock",
        "DOCK_SETTINGS_SLOT",
    ),
    "star_empire_ui_mod.pygame_flight_hud": (
        "class PygameFlightHudWindows",
        "class FlightHudFrame",
        "transactional: bool = False",
        "theme: Any = DEFAULT_THEME",
    ),
    "star_empire_ui_mod.pygame_legacy_panels": (
        "class PygameLegacyPanels",
        "theme: Any = DEFAULT_THEME",
    ),
    "star_empire_ui_mod.pygame_window": (
        "class StandardWindowHost",
        "class WindowLayerBuffer",
        "class WindowLayerCompositor",
    ),
    "star_empire_ui_mod.pygame_workspace": (
        "def collect_live_drawn_windows",
        "def topmost_window_at",
        "def route_workspace_chrome_event",
    ),
    "star_empire_ui_mod.asset_runtime": (
        "class AssetRuntime",
        "ASSETS = AssetRuntime()",
    ),
    "star_empire_ui_mod.nocturne_assets": (
        "NOCTURNE_SMOKE_COMMAND_ASSET_SHA256",
        "def asset",
    ),
    "star_empire_ui_mod.kitty_assets": (
        "KITTY_COMMAND_ASSET_SHA256",
        "def asset",
    ),
    # Asset-backed skins are Python modules containing verified PNG bytes, so
    # they remain available after the frozen client is moved or updated.
    "star_empire_ui_mod.skin_assets": (
        "GLOSSY_BLACK_TECHNICAL_ASSETS",
        "CONCEPT_BLACK_COMMAND_ATLAS",
        "SOURCE_ZIP_SHA256",
    ),
    "star_empire_ui_mod.runtime": (
        "class UiModRuntime",
        "def active_theme",
    ),
    "star_empire_ui_mod.theme": (
        "class Theme",
        "DEFAULT_THEME",
    ),
    "star_empire_ui_mod.windowing": (
        "class WindowRegistry",
        "def chrome_for",
    ),
    "star_empire_ui_mod.workspace": (
        "class Workspace",
        "active_window_id",
    ),
    "star_empire_mod_loader": (
        "LOADER_API_VERSION",
        "class ModEventBus",
        "class ScopedModApi",
    ),
    "star_empire_mod_loader.host_client": (
        "class ModLoaderClientBridge",
        "def install_mod_loader_bridge",
        "def initialize_mod_loader",
    ),
    "star_empire_mod_loader.runtime": (
        "class ExternalModLoader",
        "def load_enabled",
        "def shutdown",
    ),
}

REQUIRED_SOURCE_MARKER_ALTERNATIVES: dict[
    str, tuple[tuple[str, ...], ...]
] = {
    "Client": (
        (
            "install_bridge_bundle",
            "_initialize_ui_mod_target_alpha",
            "_handle_ui_mod_target_alpha_event",
            "_draw_ui_mod_target_alpha",
        ),
        (
            "install_bridge_bundle",
            "_initialize_ui_mod_target_vitals_alpha",
            "_handle_ui_mod_target_vitals_alpha_event",
            "_draw_ui_mod_target_vitals_alpha",
        ),
        (
            "install_full_framework",
            "_initialize_ui_mod_full",
            "_handle_ui_mod_full_input",
            "_ui_mod_begin_full_draw_frame",
            "_draw_ui_mod_full_frame",
        ),
        (
            "install_mod_loader_bridge",
            "_initialize_star_empire_mod_loader",
            "_handle_star_empire_mod_event",
            "_begin_star_empire_mod_frame",
            "_draw_star_empire_mod_overlay",
        ),
    ),
    "render_mixin": (
        ("ui_mod_visible",),
        ("_draw_star_empire_mod_region",),
    ),
}

MODULE_SOURCE_PATHS: dict[str, Path] = {
    "Client": Path("Client.py"),
    "render_mixin": Path("render_mixin.py"),
    "hangar_inventory": Path("hangar_inventory.py"),
    "gl_renderer": Path("gl_renderer.py"),
    "item_search": Path("item_search.py"),
    "star_empire_mod_loader": Path(
        "star_empire_mod_loader/__init__.py"),
    "star_empire_mod_loader.host_client": Path(
        "star_empire_mod_loader/host_client.py"),
    "star_empire_mod_loader.runtime": Path(
        "star_empire_mod_loader/runtime.py"),
    "star_empire_ui_mod.content": Path("star_empire_ui_mod/content.py"),
    "star_empire_ui_mod.pygame_content": Path(
        "star_empire_ui_mod/pygame_content.py"),
    "star_empire_ui_mod": Path("star_empire_ui_mod/__init__.py"),
    "star_empire_ui_mod.catalog": Path("star_empire_ui_mod/catalog.py"),
    "star_empire_ui_mod.config": Path("star_empire_ui_mod/config.py"),
    "star_empire_ui_mod.control_centre": Path(
        "star_empire_ui_mod/control_centre.py"),
    "star_empire_ui_mod.dock": Path("star_empire_ui_mod/dock.py"),
    "star_empire_ui_mod.layout": Path("star_empire_ui_mod/layout.py"),
    "star_empire_ui_mod.legacy_panels": Path(
        "star_empire_ui_mod/legacy_panels.py"),
    "star_empire_ui_mod.host_method_bridge": Path(
        "star_empire_ui_mod/host_method_bridge.py"),
    "star_empire_ui_mod.host_alpha_client": Path(
        "star_empire_ui_mod/host_alpha_client.py"),
    "star_empire_ui_mod.host_chat": Path(
        "star_empire_ui_mod/host_chat.py"),
    "star_empire_ui_mod.host_client_core": Path(
        "star_empire_ui_mod/host_client_core.py"),
    "star_empire_ui_mod.host_client_content": Path(
        "star_empire_ui_mod/host_client_content.py"),
    "star_empire_ui_mod.host_client_input": Path(
        "star_empire_ui_mod/host_client_input.py"),
    "star_empire_ui_mod.host_client_lifecycle": Path(
        "star_empire_ui_mod/host_client_lifecycle.py"),
    "star_empire_ui_mod.host_client_render": Path(
        "star_empire_ui_mod/host_client_render.py"),
    "star_empire_ui_mod.host_full_install": Path(
        "star_empire_ui_mod/host_full_install.py"),
    "star_empire_ui_mod.host_flight_controls": Path(
        "star_empire_ui_mod/host_flight_controls.py"),
    "star_empire_ui_mod.host_friends": Path(
        "star_empire_ui_mod/host_friends.py"),
    "star_empire_ui_mod.host_legacy_panels": Path(
        "star_empire_ui_mod/host_legacy_panels.py"),
    "star_empire_ui_mod.host_minimap": Path(
        "star_empire_ui_mod/host_minimap.py"),
    "star_empire_ui_mod.host_missions": Path(
        "star_empire_ui_mod/host_missions.py"),
    "star_empire_ui_mod.host_captain": Path(
        "star_empire_ui_mod/host_captain.py"),
    "star_empire_ui_mod.host_game_surfaces": Path(
        "star_empire_ui_mod/host_game_surfaces.py"),
    "star_empire_ui_mod.host_galaxy": Path(
        "star_empire_ui_mod/host_galaxy.py"),
    "star_empire_ui_mod.host_item_search": Path(
        "star_empire_ui_mod/host_item_search.py"),
    "star_empire_ui_mod.host_hangar_context": Path(
        "star_empire_ui_mod/host_hangar_context.py"),
    "star_empire_ui_mod.host_rc_bot_operations": Path(
        "star_empire_ui_mod/host_rc_bot_operations.py"),
    "star_empire_ui_mod.host_simple_modals": Path(
        "star_empire_ui_mod/host_simple_modals.py"),
    "star_empire_ui_mod.host_target": Path(
        "star_empire_ui_mod/host_target.py"),
    "star_empire_ui_mod.host_stasis": Path(
        "star_empire_ui_mod/host_stasis.py"),
    "star_empire_ui_mod.host_squad": Path(
        "star_empire_ui_mod/host_squad.py"),
    "star_empire_ui_mod.host_station": Path(
        "star_empire_ui_mod/host_station.py"),
    "star_empire_ui_mod.host_station_dialogs": Path(
        "star_empire_ui_mod/host_station_dialogs.py"),
    "star_empire_ui_mod.host_turret_pickers": Path(
        "star_empire_ui_mod/host_turret_pickers.py"),
    "star_empire_ui_mod.host_turret_control": Path(
        "star_empire_ui_mod/host_turret_control.py"),
    "star_empire_ui_mod.host_target_vitals_client": Path(
        "star_empire_ui_mod/host_target_vitals_client.py"),
    "star_empire_ui_mod.host_vitals": Path(
        "star_empire_ui_mod/host_vitals.py"),
    "star_empire_ui_mod.host_context_menu": Path(
        "star_empire_ui_mod/host_context_menu.py"),
    "star_empire_ui_mod.host_coalition": Path(
        "star_empire_ui_mod/host_coalition.py"),
    "star_empire_ui_mod.host_coalition_client": Path(
        "star_empire_ui_mod/host_coalition_client.py"),
    "star_empire_ui_mod.pygame_chat": Path(
        "star_empire_ui_mod/pygame_chat.py"),
    "star_empire_ui_mod.pygame_control_centre": Path(
        "star_empire_ui_mod/pygame_control_centre.py"),
    "star_empire_ui_mod.pygame_dock": Path(
        "star_empire_ui_mod/pygame_dock.py"),
    "star_empire_ui_mod.pygame_flight_hud": Path(
        "star_empire_ui_mod/pygame_flight_hud.py"),
    "star_empire_ui_mod.pygame_legacy_panels": Path(
        "star_empire_ui_mod/pygame_legacy_panels.py"),
    "star_empire_ui_mod.pygame_window": Path(
        "star_empire_ui_mod/pygame_window.py"),
    "star_empire_ui_mod.pygame_workspace": Path(
        "star_empire_ui_mod/pygame_workspace.py"),
    "star_empire_ui_mod.asset_runtime": Path(
        "star_empire_ui_mod/asset_runtime.py"),
    "star_empire_ui_mod.nocturne_assets": Path(
        "star_empire_ui_mod/nocturne_assets.py"),
    "star_empire_ui_mod.kitty_assets": Path(
        "star_empire_ui_mod/kitty_assets.py"),
    "star_empire_ui_mod.skin_assets": Path(
        "star_empire_ui_mod/skin_assets.py"),
    "star_empire_ui_mod.runtime": Path("star_empire_ui_mod/runtime.py"),
    "star_empire_ui_mod.theme": Path("star_empire_ui_mod/theme.py"),
    "star_empire_ui_mod.windowing": Path("star_empire_ui_mod/windowing.py"),
    "star_empire_ui_mod.workspace": Path("star_empire_ui_mod/workspace.py"),
}


def _replacement_typecode(module_name: str) -> int:
    """Return the PYZ entry type required by a replacement source module."""
    source_path = MODULE_SOURCE_PATHS.get(module_name)
    if source_path is not None and source_path.name == "__init__.py":
        return PYZ_PACKAGE
    return PYZ_MODULE


def _replacement_modules(module_names: Iterable[str] | None) -> tuple[str, ...]:
    """Return the validated frozen-module set for one repack.

    Omitting ``module_names`` deliberately preserves the historical full UI
    replacement. A selected repack is for isolated package changes, such as a
    theme, when unrelated loose game sources do not match the frozen client.
    """
    if module_names is None:
        return tuple(REQUIRED_SOURCE_MARKERS)
    selected = tuple(dict.fromkeys(str(name) for name in module_names))
    if not selected:
        raise ValueError("at least one replacement module is required")
    unknown = tuple(name for name in selected if name not in REQUIRED_SOURCE_MARKERS)
    if unknown:
        raise ValueError("unknown replacement module(s): " + ", ".join(unknown))
    return selected


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_cookie(path: Path) -> tuple[bytes, int, int, int, int, bytes]:
    """Read the CArchive cookie stored at the physical end of an executable."""
    with path.open("rb") as stream:
        stream.seek(-CArchiveWriter._COOKIE_LENGTH, 2)
        raw = stream.read(CArchiveWriter._COOKIE_LENGTH)
    return struct.unpack(CArchiveWriter._COOKIE_FORMAT, raw)


def _ordered_carchive_entries(path: Path, archive_start: int,
                              toc_offset: int,
                              toc_length: int) -> tuple[tuple[str, str], ...]:
    """Return the original physical TOC order, including option entries."""
    if archive_start < 0 or toc_offset < 0 or toc_length < 0:
        raise ValueError("invalid CArchive table bounds")
    with Path(path).open("rb") as stream:
        stream.seek(archive_start + toc_offset)
        payload = stream.read(toc_length)
    if len(payload) != toc_length:
        raise ValueError("truncated CArchive table")

    entries: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(payload):
        header_end = cursor + CArchiveReader._TOC_ENTRY_LENGTH
        if header_end > len(payload):
            raise ValueError("truncated CArchive table entry")
        entry_length, _offset, _stored, _raw, _compressed, typecode = (
            struct.unpack(
                CArchiveReader._TOC_ENTRY_FORMAT,
                payload[cursor:header_end],
            )
        )
        if entry_length < CArchiveReader._TOC_ENTRY_LENGTH:
            raise ValueError("invalid CArchive table entry length")
        entry_end = cursor + entry_length
        if entry_end > len(payload):
            raise ValueError("CArchive table entry exceeds its bounds")
        name = payload[header_end:entry_end].rstrip(b"\0").decode("utf-8")
        entries.append((name, typecode.decode("ascii")))
        cursor = entry_end
    if cursor != len(payload):
        raise ValueError("CArchive table length mismatch")
    return tuple(entries)


def _raw_carchive_entry(reader: CArchiveReader, name: str) -> bytes:
    """Return one outer CArchive entry exactly as stored on disk."""
    try:
        offset, length, _raw_length, _compressed, _typecode = reader.toc[name]
    except KeyError as error:
        raise KeyError(f"No CArchive entry named {name!r}") from error
    if offset < 0 or length < 0:
        raise ValueError(f"invalid CArchive bounds for {name}")
    with Path(reader._filename).open("rb") as stream:
        stream.seek(reader._start_offset + offset)
        payload = stream.read(length)
    if len(payload) != length:
        raise ValueError(f"truncated CArchive entry: {name}")
    return payload


def _copy_raw_carchive_entry(stream, reader: CArchiveReader, name: str,
                             archive_start: int) -> tuple:
    """Copy an untouched outer entry without decompressing or recompressing."""
    _offset, length, raw_length, compressed, typecode = reader.toc[name]
    data_offset = stream.tell()
    payload = _raw_carchive_entry(reader, name)
    stream.write(payload)
    return (
        data_offset - archive_start,
        length,
        raw_length,
        int(compressed),
        typecode,
        name,
    )


def _compiled_module(source: Path, module_name: str) -> CodeType:
    text = source.read_text(encoding="utf-8")
    required = REQUIRED_SOURCE_MARKERS.get(module_name, ())
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise ValueError(
            f"{module_name} source is missing required UI Mod markers: "
            + ", ".join(missing))
    alternatives = REQUIRED_SOURCE_MARKER_ALTERNATIVES.get(module_name, ())
    if alternatives and not any(
            all(marker in text for marker in profile)
            for profile in alternatives):
        missing_profiles = (
            ", ".join(marker for marker in profile if marker not in text)
            for profile in alternatives
        )
        raise ValueError(
            f"{module_name} source is missing a complete required UI Mod "
            "marker profile; missing alternatives: "
            + " OR ".join(missing_profiles))
    # Compile exactly as the source module itself requests. Without
    # dont_inherit=True this tool's own ``from __future__ import annotations``
    # silently leaks into Client/render_mixin/gl_renderer and makes the frozen
    # bytecode differ from a normal source/PyInstaller compilation.
    return compile(
        text, f"{module_name}.py", "exec", optimize=0, dont_inherit=True)


def _rebuild_pyz(payload: bytes, toc: dict[str, tuple[int, int, int]],
                 replacements: Mapping[str, bytes]) -> bytes:
    """Return *payload* with only the requested PYZ modules replaced.

    Module blobs are copied in their already-compressed form.  Recompressing
    every module would make the archive needlessly different and would weaken
    verification that this repack touched only Client.
    """
    if payload[:4] != PYZ_MAGIC:
        raise ValueError("embedded PYZ has an invalid magic header")
    data_offsets = [offset for typecode, offset, length in toc.values()
                    if typecode != PYZ_NSPKG and length]
    if not data_offsets:
        raise ValueError("embedded PYZ contains no module payloads")
    header_length = min(data_offsets)
    if header_length < 12:
        raise ValueError("embedded PYZ header is truncated")

    output = io.BytesIO()
    output.write(payload[:header_length])
    rebuilt_toc = {}
    for name, (typecode, offset, length) in toc.items():
        new_offset = output.tell()
        if typecode == PYZ_NSPKG:
            blob = b""
        elif name in replacements:
            blob = zlib.compress(replacements[name])
            typecode = _replacement_typecode(name)
        else:
            end = offset + length
            if offset < header_length or end > len(payload):
                raise ValueError(f"embedded PYZ entry is out of bounds: {name}")
            blob = payload[offset:end]
        output.write(blob)
        rebuilt_toc[name] = (typecode, new_offset, len(blob))

    # New UI framework modules do not exist in an unmodified game PYZ. Append
    # only explicitly requested additions with the source module's proper PYZ
    # type; package __init__ modules must not be stored as ordinary modules.
    for name in sorted(set(replacements).difference(toc)):
        new_offset = output.tell()
        blob = zlib.compress(replacements[name])
        output.write(blob)
        rebuilt_toc[name] = (
            _replacement_typecode(name), new_offset, len(blob))

    toc_offset = output.tell()
    output.write(marshal.dumps(rebuilt_toc))
    rebuilt = bytearray(output.getvalue())
    rebuilt[8:12] = struct.pack("!i", toc_offset)
    return bytes(rebuilt)


def repack_client(input_exe: Path, source: Path, output_exe: Path,
                  module_names: Iterable[str] | None = None) -> None:
    """Write a verified executable with the selected frozen UI modules replaced."""
    input_exe = input_exe.resolve(strict=True)
    source = source.resolve(strict=True)
    output_exe = output_exe.resolve()
    selected_modules = _replacement_modules(module_names)
    if input_exe == output_exe:
        raise ValueError("output must be a new path; do not overwrite the input executable")
    if output_exe.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_exe}")

    original = CArchiveReader(str(input_exe))
    if CLIENT_ENTRY not in original.toc:
        raise ValueError("input executable does not contain a frozen Client entry")
    _magic, _archive_length, toc_offset, toc_length, pyvers, pylib_name = _read_cookie(input_exe)
    ordered_entries = _ordered_carchive_entries(
        input_exe, original._start_offset, toc_offset, toc_length)
    current_pyvers = sys.version_info.major * 100 + sys.version_info.minor
    if pyvers != current_pyvers:
        raise RuntimeError(
            f"archive requires Python {pyvers}, but this tool is Python {current_pyvers}")

    replacements: dict[str, bytes] = {}
    for module_name in selected_modules:
        try:
            relative_source = MODULE_SOURCE_PATHS[module_name]
        except KeyError as error:
            raise RuntimeError(
                f"no source path is registered for {module_name}") from error
        module_source = source.parent / relative_source
        if not module_source.is_file():
            raise FileNotFoundError(
                f"required frozen module source is missing: {module_source}")
        replacements[module_name] = marshal.dumps(
            _compiled_module(module_source, module_name))
    replacement = replacements.get(CLIENT_ENTRY)
    if PYZ_ENTRY not in original.toc:
        raise ValueError("input executable does not contain an embedded PYZ")
    original_pyz = original.open_embedded_archive(PYZ_ENTRY)
    replacement_pyz = _rebuild_pyz(
        original.extract(PYZ_ENTRY), original_pyz.toc, replacements)
    pending = output_exe.with_name(output_exe.name + ".pending")
    if pending.exists():
        raise FileExistsError(f"refusing to overwrite unfinished output: {pending}")

    writer = object.__new__(CArchiveWriter)
    prefix = input_exe.read_bytes()[:original._start_offset]
    try:
        with pending.open("wb") as stream:
            stream.write(prefix)
            archive_start = stream.tell()
            toc = []
            for name, ordered_typecode in ordered_entries:
                if ordered_typecode == "o":
                    entry = writer._write_blob(stream, b"", name, "o")
                    toc.append((entry[0] - archive_start, *entry[1:]))
                    continue
                try:
                    (_offset, _length, _uncompressed, compressed,
                     typecode) = original.toc[name]
                except KeyError as error:
                    raise RuntimeError(
                        f"ordered CArchive entry is missing: {name}") from error
                if typecode != ordered_typecode:
                    raise RuntimeError(
                        f"ordered CArchive type differs for {name}")
                if name == CLIENT_ENTRY and replacement is not None:
                    payload = replacement
                elif name == PYZ_ENTRY:
                    payload = replacement_pyz
                else:
                    toc.append(_copy_raw_carchive_entry(
                        stream, original, name, archive_start))
                    continue
                entry = writer._write_blob(stream, payload, name, typecode,
                                           compress=bool(compressed))
                toc.append((entry[0] - archive_start, *entry[1:]))
            toc_offset = stream.tell() - archive_start
            toc_data = CArchiveWriter._serialize_toc(toc)
            stream.write(toc_data)
            archive_length = toc_offset + len(toc_data) + CArchiveWriter._COOKIE_LENGTH
            stream.write(struct.pack(
                CArchiveWriter._COOKIE_FORMAT,
                CArchiveWriter._COOKIE_MAGIC_PATTERN,
                archive_length,
                toc_offset,
                len(toc_data),
                pyvers,
                pylib_name,
            ))

        rebuilt = CArchiveReader(str(pending))
        if rebuilt.options != original.options or tuple(rebuilt.toc) != tuple(original.toc):
            raise RuntimeError("rebuilt archive table does not match the original")
        (_rebuilt_magic, _rebuilt_length, rebuilt_toc_offset,
         rebuilt_toc_length, _rebuilt_pyvers,
         _rebuilt_pylib) = _read_cookie(pending)
        rebuilt_order = _ordered_carchive_entries(
            pending, rebuilt._start_offset,
            rebuilt_toc_offset, rebuilt_toc_length)
        if rebuilt_order != ordered_entries:
            raise RuntimeError("rebuilt CArchive entry order changed")
        if replacement is not None:
            client_code = marshal.loads(rebuilt.extract(CLIENT_ENTRY))
            if not isinstance(client_code, CodeType):
                raise RuntimeError("rebuilt Client entry is not a Python code object")
            if _sha256(rebuilt.extract(CLIENT_ENTRY)) != _sha256(replacement):
                raise RuntimeError("rebuilt Client entry differs from the compiled source")
        elif _sha256(rebuilt.extract(CLIENT_ENTRY)) != _sha256(original.extract(CLIENT_ENTRY)):
            raise RuntimeError("unexpected Client change in selected repack")
        rebuilt_pyz = rebuilt.open_embedded_archive(PYZ_ENTRY)
        added_modules = tuple(sorted(
            set(replacements).difference(original_pyz.toc)))
        expected_pyz_names = tuple(original_pyz.toc) + added_modules
        if tuple(rebuilt_pyz.toc) != expected_pyz_names:
            raise RuntimeError("rebuilt PYZ table does not match the original")
        for module_name, replacement_payload in replacements.items():
            if (_sha256(rebuilt_pyz.extract(module_name, raw=True))
                    != _sha256(replacement_payload)):
                raise RuntimeError(
                    f"rebuilt PYZ {module_name} differs from the compiled source")
            expected_typecode = _replacement_typecode(module_name)
            if rebuilt_pyz.toc[module_name][0] != expected_typecode:
                raise RuntimeError(
                    f"rebuilt PYZ {module_name} has the wrong module type")
        for name in original_pyz.toc:
            if name in replacements:
                continue
            if rebuilt_pyz.toc[name][0] != original_pyz.toc[name][0]:
                raise RuntimeError(f"unexpected PYZ type change in {name}")
            before = original_pyz.extract(name, raw=True)
            after = rebuilt_pyz.extract(name, raw=True)
            if before != after:
                raise RuntimeError(f"unexpected PYZ change in {name}")
        for name in original.toc:
            if name in (CLIENT_ENTRY, PYZ_ENTRY):
                continue
            if _raw_carchive_entry(rebuilt, name) != _raw_carchive_entry(
                    original, name):
                raise RuntimeError(
                    f"unexpected stored CArchive change in {name}")
        pending.replace(output_exe)
    except Exception:
        pending.unlink(missing_ok=True)
        raise


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path,
                        help="existing PyInstaller Client.exe")
    parser.add_argument("--source", required=True, type=Path,
                        help="edited Client.py to compile into the executable")
    parser.add_argument("--output", required=True, type=Path,
                        help="new executable path; must not already exist")
    parser.add_argument("--modules", nargs="+", choices=tuple(REQUIRED_SOURCE_MARKERS),
                        help="optional explicit frozen modules to replace; default is all")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _arguments()
    repack_client(arguments.input, arguments.source, arguments.output,
                  arguments.modules)
    print(f"Repacked {arguments.output}")
