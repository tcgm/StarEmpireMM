"""Focused regression tests for the frozen UI-module repacker."""

from __future__ import annotations

import __future__
import io
import importlib.util
import marshal
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter

from tools.repack_client import (MODULE_SOURCE_PATHS,
                                 REQUIRED_SOURCE_MARKER_ALTERNATIVES,
                                 REQUIRED_SOURCE_MARKERS,
                                 _compiled_module, _copy_raw_carchive_entry,
                                 _ordered_carchive_entries,
                                 _raw_carchive_entry, _read_cookie,
                                 _rebuild_pyz, _replacement_modules,
                                 repack_client)


def _pyz_payload(entries: dict[str, tuple[int, bytes]]) -> tuple[bytes, dict]:
    payload = bytearray(b"PYZ\0" + importlib.util.MAGIC_NUMBER + b"\0\0\0\0")
    toc = {}
    for name, (typecode, raw) in entries.items():
        blob = b"" if typecode == 3 else zlib.compress(raw)
        toc[name] = (typecode, len(payload), len(blob))
        payload.extend(blob)
    toc_offset = len(payload)
    payload.extend(marshal.dumps(toc))
    payload[8:12] = struct.pack("!i", toc_offset)
    return bytes(payload), toc


class RebuildPyzTests(unittest.TestCase):
    def test_repacker_embeds_the_complete_ui_framework_package(self):
        package_modules = {
            "star_empire_ui_mod",
            "star_empire_ui_mod.catalog",
            "star_empire_ui_mod.config",
            "star_empire_ui_mod.content",
            "star_empire_ui_mod.control_centre",
            "star_empire_ui_mod.dock",
            "star_empire_ui_mod.layout",
            "star_empire_ui_mod.legacy_panels",
            "star_empire_ui_mod.host_alpha_client",
            "star_empire_ui_mod.host_chat",
            "star_empire_ui_mod.host_client_core",
            "star_empire_ui_mod.host_client_content",
            "star_empire_ui_mod.host_client_input",
            "star_empire_ui_mod.host_client_lifecycle",
            "star_empire_ui_mod.host_client_render",
            "star_empire_ui_mod.host_full_install",
            "star_empire_ui_mod.host_flight_controls",
            "star_empire_ui_mod.host_friends",
            "star_empire_ui_mod.host_legacy_panels",
            "star_empire_ui_mod.host_minimap",
            "star_empire_ui_mod.host_missions",
            "star_empire_ui_mod.host_captain",
            "star_empire_ui_mod.host_game_surfaces",
            "star_empire_ui_mod.host_galaxy",
            "star_empire_ui_mod.host_item_search",
            "star_empire_ui_mod.host_hangar_context",
            "star_empire_ui_mod.host_rc_bot_operations",
            "star_empire_ui_mod.host_simple_modals",
            "star_empire_ui_mod.host_stasis",
            "star_empire_ui_mod.host_squad",
            "star_empire_ui_mod.host_station",
            "star_empire_ui_mod.host_station_dialogs",
            "star_empire_ui_mod.host_coalition",
            "star_empire_ui_mod.host_coalition_client",
            "star_empire_ui_mod.host_method_bridge",
            "star_empire_ui_mod.host_target",
            "star_empire_ui_mod.host_turret_control",
            "star_empire_ui_mod.host_turret_pickers",
            "star_empire_ui_mod.host_target_vitals_client",
            "star_empire_ui_mod.host_vitals",
            "star_empire_ui_mod.pygame_chat",
            "star_empire_ui_mod.pygame_content",
            "star_empire_ui_mod.pygame_control_centre",
            "star_empire_ui_mod.pygame_dock",
            "star_empire_ui_mod.pygame_flight_hud",
            "star_empire_ui_mod.pygame_legacy_panels",
            "star_empire_ui_mod.pygame_window",
            "star_empire_ui_mod.asset_runtime",
            "star_empire_ui_mod.nocturne_assets",
            "star_empire_ui_mod.skin_assets",
            "star_empire_ui_mod.runtime",
            "star_empire_ui_mod.theme",
            "star_empire_ui_mod.windowing",
            "star_empire_ui_mod.workspace",
        }
        self.assertTrue(package_modules.issubset(REQUIRED_SOURCE_MARKERS))
        self.assertTrue(package_modules.issubset(MODULE_SOURCE_PATHS))

    def test_target_alpha_bridges_are_explicit_frozen_replacements(self):
        expected = {
            "star_empire_ui_mod.host_alpha_client": (
                Path("star_empire_ui_mod/host_alpha_client.py"),
                ("class AlphaClientBridge",
                 "def _initialize_ui_mod_target_alpha",
                 "def _handle_ui_mod_target_alpha_event",
                 "def _draw_ui_mod_target_alpha"),
            ),
            "star_empire_ui_mod.host_target": (
                Path("star_empire_ui_mod/host_target.py"),
                ("class TargetRendererBridge", "def render_target_panel",
                 "def _draw_panel"),
            ),
        }
        for module, (path, markers) in expected.items():
            with self.subTest(module=module):
                self.assertEqual(path, MODULE_SOURCE_PATHS[module])
                self.assertEqual(markers, REQUIRED_SOURCE_MARKERS[module])

    def test_generic_mod_loader_is_an_explicit_frozen_replacement(self):
        expected = {
            "star_empire_mod_loader": (
                Path("star_empire_mod_loader/__init__.py"),
                ("LOADER_API_VERSION", "class ModEventBus",
                 "class ScopedModApi")),
            "star_empire_mod_loader.host_client": (
                Path("star_empire_mod_loader/host_client.py"),
                ("class ModLoaderClientBridge",
                 "def install_mod_loader_bridge",
                 "def initialize_mod_loader")),
            "star_empire_mod_loader.runtime": (
                Path("star_empire_mod_loader/runtime.py"),
                ("class ExternalModLoader", "def load_enabled",
                 "def shutdown")),
        }
        for module, (path, markers) in expected.items():
            with self.subTest(module=module):
                self.assertEqual(path, MODULE_SOURCE_PATHS[module])
                self.assertEqual(markers, REQUIRED_SOURCE_MARKERS[module])

    def test_target_vitals_bridges_are_explicit_frozen_replacements(self):
        expected = {
            "star_empire_ui_mod.host_target_vitals_client": (
                Path("star_empire_ui_mod/host_target_vitals_client.py"),
                ("class TargetVitalsClientBridge",
                 "def initialize_target_vitals_alpha",
                 "def handle_target_vitals_alpha_event",
                 "def draw_target_vitals_alpha"),
            ),
            "star_empire_ui_mod.host_vitals": (
                Path("star_empire_ui_mod/host_vitals.py"),
                ("class VitalsRendererBridge",
                 "def capture_vitals_snapshot",
                 "def render_vitals_window",
                 "def _draw_hud_top_panel"),
            ),
        }
        for module, (path, markers) in expected.items():
            with self.subTest(module=module):
                self.assertEqual(path, MODULE_SOURCE_PATHS[module])
                self.assertEqual(markers, REQUIRED_SOURCE_MARKERS[module])

        self.assertIn(
            "class WindowLayerBuffer",
            REQUIRED_SOURCE_MARKERS["star_empire_ui_mod.pygame_window"],
        )
        self.assertIn(
            "transactional: bool = False",
            REQUIRED_SOURCE_MARKERS[
                "star_empire_ui_mod.pygame_flight_hud"],
        )

    def test_full_ui_framework_bridges_are_explicit_frozen_replacements(self):
        expected = {
            "star_empire_ui_mod.host_chat": (
                Path("star_empire_ui_mod/host_chat.py"),
                ("class ChatRendererBridge", "def render_chat_window",
                 "def _clear_chat_geometry"),
            ),
            "star_empire_ui_mod.host_client_core": (
                Path("star_empire_ui_mod/host_client_core.py"),
                ("class FullClientCoreBridge", "def disable_full_ui",
                 "def initialize_full_ui"),
            ),
            "star_empire_ui_mod.host_client_content": (
                Path("star_empire_ui_mod/host_client_content.py"),
                ("class FullClientContentBridge",
                 "def route_full_ui_content_event",
                 "def mask_non_owner_geometry"),
            ),
            "star_empire_ui_mod.host_client_input": (
                Path("star_empire_ui_mod/host_client_input.py"),
                ("class FullClientInputBridge", "def route_full_ui_input"),
            ),
            "star_empire_ui_mod.host_client_lifecycle": (
                Path("star_empire_ui_mod/host_client_lifecycle.py"),
                ("class GameAuthorityBinding",
                 "def sync_game_authority_windows",
                 "GAME_AUTHORITY_BINDINGS"),
            ),
            "star_empire_ui_mod.host_client_render": (
                Path("star_empire_ui_mod/host_client_render.py"),
                ("class FullClientRenderBridge", "class FullUiDrawScheduler",
                 "def flush_full_ui_frame"),
            ),
            "star_empire_ui_mod.host_full_install": (
                Path("star_empire_ui_mod/host_full_install.py"),
                ("def install_full_framework", "_ALIASES"),
            ),
            "star_empire_ui_mod.host_flight_controls": (
                Path("star_empire_ui_mod/host_flight_controls.py"),
                ("class FlightControlsRendererBridge",
                 "def render_quick_actions_window",
                 "QUICK_ACTIONS_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_friends": (
                Path("star_empire_ui_mod/host_friends.py"),
                ("class FriendsRendererBridge",
                 "def render_friends_window",
                 "FRIENDS_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_legacy_panels": (
                Path("star_empire_ui_mod/host_legacy_panels.py"),
                ("class LegacyPanelRendererBridge",
                 "def render_legacy_panel",
                 "def _translate_hit_mapping"),
            ),
            "star_empire_ui_mod.host_minimap": (
                Path("star_empire_ui_mod/host_minimap.py"),
                ("class MinimapRendererBridge",
                 "def render_minimap_window",
                 "def clear_minimap_geometry"),
            ),
            "star_empire_ui_mod.host_missions": (
                Path("star_empire_ui_mod/host_missions.py"),
                ("class MissionRendererBridge",
                 "class MissionDetailsRendererBridge",
                 "def render_mission_log_window",
                 "def render_mission_details_window",
                 "MISSION_LOG_WINDOW_ID",
                 "MISSION_DETAILS_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_simple_modals": (
                Path("star_empire_ui_mod/host_simple_modals.py"),
                ("class FixedModalSpec",
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
                 "FIXED_MODAL_SPECS"),
            ),
            "star_empire_ui_mod.host_turret_pickers": (
                Path("star_empire_ui_mod/host_turret_pickers.py"),
                ("class TurretPickerRendererBridge",
                 "def render_turret_pickers",
                 "def _draw_turret_pickers",
                 "WEAPON_PICKER_ID",
                 "UPGRADE_PICKER_ID"),
            ),
            "star_empire_ui_mod.host_turret_control": (
                Path("star_empire_ui_mod/host_turret_control.py"),
                ("class TurretControlRendererBridge",
                 "def render_turret_control",
                 "def _draw_turret_control_panel",
                 "TURRET_CONTROL_ID"),
            ),
            "star_empire_ui_mod.host_stasis": (
                Path("star_empire_ui_mod/host_stasis.py"),
                ("class StasisRendererBridge",
                 "def render_stasis_window",
                 "def _draw_stasis_gui",
                 "STASIS_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_squad": (
                Path("star_empire_ui_mod/host_squad.py"),
                ("class SquadClientBridge", "def sync_squad_windows",
                 "def draw_squad_main", "def draw_squad_invite",
                 "def register_squad_windows"),
            ),
            "star_empire_ui_mod.host_station": (
                Path("star_empire_ui_mod/host_station.py"),
                ("class StationRendererBridge", "def sync_station_window",
                 "def render_station_window", "def register_station_window",
                 "register_station_dialogs", "suppress_inline_station_dialogs",
                 "STATION_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_station_dialogs": (
                Path("star_empire_ui_mod/host_station_dialogs.py"),
                ("STATION_DIALOG_FIELDS",
                 "def suppress_inline_station_dialogs",
                 "def restore_inline_station_dialogs",
                 "def render_station_dialog",
                 "def register_station_dialogs"),
            ),
            "star_empire_ui_mod.host_captain": (
                Path("star_empire_ui_mod/host_captain.py"),
                ("class CaptainRendererBridge",
                 "def render_captain_window",
                 "def register_captain_window",
                 "def clear_captain_geometry",
                 "def _draw_stats_panel",
                 "CAPTAIN_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_game_surfaces": (
                Path("star_empire_ui_mod/host_game_surfaces.py"),
                ("class GameSurfacesRendererBridge",
                 "def render_game_surfaces",
                 "def clear_game_surface_geometry",
                 "def _draw_esc_menu",
                 "GAME_MENU_WINDOW_ID",
                 "OPTIONS_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_galaxy": (
                Path("star_empire_ui_mod/host_galaxy.py"),
                ("class GalaxyRendererBridge",
                 "class GalaxyOverlayProxy",
                 "def render_galaxy_window",
                 "def register_galaxy_window",
                 "def prepare_galaxy_foreground",
                 "def clear_galaxy_geometry",
                 "def _draw_galaxy_map",
                 "GALAXY_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_item_search": (
                Path("star_empire_ui_mod/host_item_search.py"),
                ("class ItemLocatorRendererBridge",
                 "def render_item_locator",
                 "def clear_item_locator_geometry",
                 "def _draw_item_search",
                 "ITEM_LOCATOR_WINDOW_ID"),
            ),
            "star_empire_ui_mod.host_hangar_context": (
                Path("star_empire_ui_mod/host_hangar_context.py"),
                ("class HangarContextRendererBridge",
                 "def render_station_hangar_context",
                 "def render_ship_inventory_context",
                 "def _draw_hangar_ctx_menu",
                 "def _hinv_draw_ctx_menu"),
            ),
            "star_empire_ui_mod.host_rc_bot_operations": (
                Path("star_empire_ui_mod/host_rc_bot_operations.py"),
                ("class RcBotOperationsRendererBridge",
                 "def render_rc_bot_operations",
                 "def register_rc_bot_operations",
                 "def clear_rc_bot_operations_geometry",
                 "def _draw_rc_bot_haul_panel",
                 "RC_BOT_OPERATIONS_WINDOW_ID"),
            ),
        }
        for module, (path, markers) in expected.items():
            with self.subTest(module=module):
                self.assertEqual(path, MODULE_SOURCE_PATHS[module])
                self.assertEqual(markers, REQUIRED_SOURCE_MARKERS[module])

    def test_coalition_bridge_is_an_explicit_frozen_replacement(self):
        module = "star_empire_ui_mod.host_coalition"
        self.assertEqual(
            Path("star_empire_ui_mod/host_coalition.py"),
            MODULE_SOURCE_PATHS[module],
        )
        self.assertEqual(
            ("class CoalitionRendererBridge",
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
             "def _draw_coalition_framework_popovers"),
            REQUIRED_SOURCE_MARKERS[module],
        )

    def test_coalition_client_bridge_is_an_explicit_frozen_replacement(self):
        module = "star_empire_ui_mod.host_coalition_client"
        self.assertEqual(
            Path("star_empire_ui_mod/host_coalition_client.py"),
            MODULE_SOURCE_PATHS[module],
        )
        self.assertEqual(
            ("class CoalitionClientBridge",
             "def _initialize_ui_mod_coalition_state",
             "def _coalition_window_ids",
             "def _queue_coalition_window_visibility",
             "def _clear_coalition_surface_geometry",
             "def _apply_coalition_window_visibility",
             "def _set_coalition_window_visible",
             "def _sync_coalition_window_states"),
            REQUIRED_SOURCE_MARKERS[module],
        )

    def test_ship_cargo_mixin_is_an_explicit_frozen_ui_replacement(self):
        self.assertEqual(Path("hangar_inventory.py"),
                         MODULE_SOURCE_PATHS["hangar_inventory"])
        self.assertEqual(
            ("class HangarInventoryMixin", "def _hinv_draw_ctx_menu"),
            REQUIRED_SOURCE_MARKERS["hangar_inventory"],
        )

    def test_gl_renderer_uses_updated_map_layer_markers(self):
        self.assertEqual(
            ("def present_world", "canvas_above", "map_connectors",
             "map_labels"),
            REQUIRED_SOURCE_MARKERS["gl_renderer"],
        )

    def test_item_locator_is_an_explicit_frozen_ui_replacement(self):
        self.assertEqual(Path("item_search.py"),
                         MODULE_SOURCE_PATHS["item_search"])
        self.assertEqual(
            ("class ItemSearchMixin", "def _draw_item_search"),
            REQUIRED_SOURCE_MARKERS["item_search"],
        )

    def test_embedded_skin_assets_are_an_explicit_frozen_replacement(self):
        self.assertEqual(Path("star_empire_ui_mod/asset_runtime.py"),
                         MODULE_SOURCE_PATHS["star_empire_ui_mod.asset_runtime"])
        self.assertEqual(
            ("class AssetRuntime", "ASSETS = AssetRuntime()"),
            REQUIRED_SOURCE_MARKERS["star_empire_ui_mod.asset_runtime"],
        )
        self.assertEqual(Path("star_empire_ui_mod/skin_assets.py"),
                         MODULE_SOURCE_PATHS["star_empire_ui_mod.skin_assets"])
        self.assertEqual(
            ("GLOSSY_BLACK_TECHNICAL_ASSETS", "CONCEPT_BLACK_COMMAND_ATLAS",
             "SOURCE_ZIP_SHA256"),
            REQUIRED_SOURCE_MARKERS["star_empire_ui_mod.skin_assets"],
        )
        self.assertEqual(Path("star_empire_ui_mod/nocturne_assets.py"),
                         MODULE_SOURCE_PATHS["star_empire_ui_mod.nocturne_assets"])
        self.assertEqual(
            ("NOCTURNE_SMOKE_COMMAND_ASSET_SHA256", "def asset"),
            REQUIRED_SOURCE_MARKERS["star_empire_ui_mod.nocturne_assets"],
        )

    def test_selected_repack_modules_preserve_the_default_and_validate_names(self):
        self.assertEqual(tuple(REQUIRED_SOURCE_MARKERS),
                         _replacement_modules(None))
        self.assertEqual(
            ("star_empire_ui_mod.theme", "star_empire_ui_mod.pygame_window"),
            _replacement_modules(("star_empire_ui_mod.theme",
                                  "star_empire_ui_mod.pygame_window",
                                  "star_empire_ui_mod.theme")),
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            _replacement_modules(())
        with self.assertRaisesRegex(ValueError, "unknown replacement"):
            _replacement_modules(("not.a.real.module",))

    def test_source_compile_does_not_inherit_repacker_future_flags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "example.py"
            source.write_text("value: MissingType = 1\n", encoding="utf-8")
            code = _compiled_module(source, "example")

        self.assertFalse(
            code.co_flags & __future__.annotations.compiler_flag)

    def test_client_compile_accepts_each_complete_bridge_marker_profile(self):
        common = REQUIRED_SOURCE_MARKERS["Client"]
        alternatives = REQUIRED_SOURCE_MARKER_ALTERNATIVES["Client"]

        for profile in alternatives:
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                source = Path(temp_dir) / "Client.py"
                source.write_text(
                    "".join(
                        f"def {marker}():\n    pass\n"
                        for marker in (*common, *profile)
                    ),
                    encoding="utf-8",
                )
                code = _compiled_module(source, "Client")

            self.assertEqual("<module>", code.co_name)

    def test_client_compile_rejects_missing_profile_installer_marker(self):
        profile = REQUIRED_SOURCE_MARKER_ALTERNATIVES["Client"][0]
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Client.py"
            source.write_text(
                "".join(
                    f"def {marker}():\n    pass\n"
                    for marker in profile[1:]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                    ValueError, "complete required UI Mod marker profile"):
                _compiled_module(source, "Client")

    def test_client_compile_rejects_incomplete_or_mixed_bridge_markers(self):
        old, target_vitals, full, loader = (
            REQUIRED_SOURCE_MARKER_ALTERNATIVES["Client"])
        cases = {
            "incomplete": old[:2],
            "mixed": (old[0], target_vitals[1], target_vitals[2]),
            "mixed-full": (
                full[0], target_vitals[1], full[2], full[3]),
            "incomplete-loader": loader[:-1],
        }
        for name, markers in cases.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                source = Path(temp_dir) / "Client.py"
                source.write_text(
                    "".join(
                        f"def {marker}():\n    pass\n" for marker in markers
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError, "complete required UI Mod marker profile"
                ):
                    _compiled_module(source, "Client")

    def test_render_mixin_accepts_full_ui_or_loader_region_marker(self):
        for marker in ("ui_mod_visible", "_draw_star_empire_mod_region"):
            with (
                self.subTest(marker=marker),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                source = Path(temp_dir) / "render_mixin.py"
                source.write_text(
                    f"def _draw_panel():\n    pass\n{marker} = True\n",
                    encoding="utf-8",
                )
                code = _compiled_module(source, "render_mixin")

            self.assertEqual("<module>", code.co_name)

    def test_copies_untouched_outer_entry_as_exact_stored_bytes(self):
        raw = b"unchanged outer entry" * 20
        stored = zlib.compress(raw, level=1)
        prefix = b"BOOT"
        gap = b"gap"
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.bin"
            archive.write_bytes(prefix + gap + stored + b"tail")

            class Reader:
                _filename = str(archive)
                _start_offset = len(prefix)
                toc = {
                    "entry": (len(gap), len(stored), len(raw), 1, "m"),
                }

            output = io.BytesIO(b"XX")
            output.seek(2)
            entry = _copy_raw_carchive_entry(output, Reader(), "entry", 2)
            copied = _raw_carchive_entry(Reader(), "entry")

        self.assertEqual(stored, copied)
        self.assertEqual(stored, output.getvalue()[2:])
        self.assertEqual((0, len(stored), len(raw), 1, "m", "entry"), entry)
        self.assertNotEqual(zlib.compress(raw, level=9), stored)

    def test_full_repack_preserves_outer_blob_and_option_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_source = root / "old_Client.py"
            old_source.write_text("VALUE = 'old'\n", encoding="utf-8")
            untouched = root / "untouched.bin"
            untouched.write_bytes(b"outer dependency" * 200)
            pyz_path = root / "PYZ.pyz"
            pyz_path.write_bytes(_pyz_payload({
                "Audio": (0, b"audio-code"),
                "Client": (0, b"old-client-code"),
            })[0])
            original = root / "Client.exe"
            previous_level = CArchiveWriter._COMPRESSION_LEVEL
            try:
                CArchiveWriter._COMPRESSION_LEVEL = 1
                CArchiveWriter(str(original), (
                    ("Client", str(old_source), True, "s"),
                    ("pyi-contents-directory _internal", "", False, "o"),
                    ("untouched.bin", str(untouched), True, "x"),
                    ("PYZ.pyz", str(pyz_path), False, "z"),
                ), "python314.dll")
            finally:
                CArchiveWriter._COMPRESSION_LEVEL = previous_level

            stage = root / "stage" / "_internal"
            stage.mkdir(parents=True)
            (stage / "Client.py").write_text(
                "# _initialize_ui_mod_target_alpha\n"
                "# _handle_ui_mod_target_alpha_event\n"
                "# _draw_ui_mod_target_alpha\n"
                "# install_bridge_bundle\n"
                "VALUE = 'new'\n",
                encoding="utf-8",
            )
            candidate = root / "candidate.exe"
            before_reader = CArchiveReader(str(original))
            before_blob = _raw_carchive_entry(before_reader, "untouched.bin")
            before_cookie = _read_cookie(original)
            before_order = _ordered_carchive_entries(
                original, before_reader._start_offset,
                before_cookie[2], before_cookie[3])

            repack_client(
                original, stage / "Client.py", candidate,
                module_names=("Client",),
            )

            after_reader = CArchiveReader(str(candidate))
            after_cookie = _read_cookie(candidate)
            after_order = _ordered_carchive_entries(
                candidate, after_reader._start_offset,
                after_cookie[2], after_cookie[3])
            self.assertEqual(before_order, after_order)
            self.assertEqual(
                (("Client", "s"),
                 ("pyi-contents-directory _internal", "o"),
                 ("untouched.bin", "x"), ("PYZ.pyz", "z")),
                after_order,
            )
            self.assertEqual(
                before_blob,
                _raw_carchive_entry(after_reader, "untouched.bin"),
            )
            self.assertNotEqual(
                before_reader.extract("Client"),
                after_reader.extract("Client"),
            )

    def test_replaces_requested_modules_and_preserves_other_compressed_modules(self):
        original, toc = _pyz_payload({
            "Audio": (0, b"audio-code"),
            "Client": (0, b"old-client-code"),
            "render_mixin": (0, b"old-render-code"),
            "gl_renderer": (0, b"old-gl-code"),
            "namespace": (3, b""),
        })

        rebuilt = _rebuild_pyz(original, toc, {
            "Client": b"new-client-code",
            "render_mixin": b"new-render-code",
            "gl_renderer": b"new-gl-code",
            "star_empire_ui_mod.content": b"new-content-code",
        })
        rebuilt_toc = marshal.loads(
            rebuilt[struct.unpack("!i", rebuilt[8:12])[0]:])

        audio_before = original[toc["Audio"][1]:sum(toc["Audio"][1:])]
        audio_after_entry = rebuilt_toc["Audio"]
        audio_after = rebuilt[
            audio_after_entry[1]:audio_after_entry[1] + audio_after_entry[2]]
        client_entry = rebuilt_toc["Client"]
        client_blob = rebuilt[
            client_entry[1]:client_entry[1] + client_entry[2]]
        render_entry = rebuilt_toc["render_mixin"]
        render_blob = rebuilt[
            render_entry[1]:render_entry[1] + render_entry[2]]
        gl_entry = rebuilt_toc["gl_renderer"]
        gl_blob = rebuilt[
            gl_entry[1]:gl_entry[1] + gl_entry[2]]
        content_entry = rebuilt_toc["star_empire_ui_mod.content"]
        content_blob = rebuilt[
            content_entry[1]:content_entry[1] + content_entry[2]]

        self.assertEqual(audio_before, audio_after)
        self.assertEqual(b"new-client-code", zlib.decompress(client_blob))
        self.assertEqual(b"new-render-code", zlib.decompress(render_blob))
        self.assertEqual(b"new-gl-code", zlib.decompress(gl_blob))
        self.assertEqual(0, content_entry[0])
        self.assertEqual(b"new-content-code", zlib.decompress(content_blob))
        self.assertEqual((3, gl_entry[1] + gl_entry[2], 0),
                         rebuilt_toc["namespace"])

    def test_adds_a_requested_module_missing_from_the_original_pyz(self):
        original, toc = _pyz_payload({"Audio": (0, b"audio-code")})
        rebuilt = _rebuild_pyz(
            original, toc, {"new_ui_module": b"new-client-code"})
        rebuilt_toc = marshal.loads(
            rebuilt[struct.unpack("!i", rebuilt[8:12])[0]:])
        entry = rebuilt_toc["new_ui_module"]

        self.assertEqual(0, entry[0])
        self.assertEqual(
            b"new-client-code",
            zlib.decompress(rebuilt[entry[1]:entry[1] + entry[2]]))

    def test_package_init_is_added_or_repaired_as_a_package(self):
        for existing_entry in (False, True):
            with self.subTest(existing_entry=existing_entry):
                entries = {"Audio": (0, b"audio-code")}
                if existing_entry:
                    entries["star_empire_ui_mod"] = (0, b"broken-package-code")
                original, toc = _pyz_payload(entries)
                rebuilt = _rebuild_pyz(
                    original,
                    toc,
                    {"star_empire_ui_mod": b"fixed-package-code"},
                )
                rebuilt_toc = marshal.loads(
                    rebuilt[struct.unpack("!i", rebuilt[8:12])[0]:])
                entry = rebuilt_toc["star_empire_ui_mod"]

                self.assertEqual(1, entry[0])
                self.assertEqual(
                    b"fixed-package-code",
                    zlib.decompress(
                        rebuilt[entry[1]:entry[1] + entry[2]]),
                )


if __name__ == "__main__":
    unittest.main()
