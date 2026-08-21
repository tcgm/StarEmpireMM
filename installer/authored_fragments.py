"""Immutable, public-source fragments allowed in host hook recipes.

Recipes may reference these exact mod-authored UTF-8 bytes by versioned ID and
SHA-256.  They cannot carry insertion text of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from types import MappingProxyType
from typing import Mapping


MAX_AUTHORED_FRAGMENT_BYTES = 256 * 1024
RENDER_SHARED_CONTEXT_MENU_INSTALL_V1 = (
    "render.shared-context-menu.install.v1"
)
RENDER_SHARED_CONTEXT_MENU_INSTALL_V2 = (
    "render.shared-context-menu.install.v2"
)
RENDER_COALITION_FRAMEWORK_INSTALL_V1 = (
    "render.coalition-framework.install.v1"
)
RENDER_FRAMEWORK_CORE_INSTALL_V3 = (
    "render.framework-core.install.v3"
)
RENDER_FRAMEWORK_CORE_INSTALL_V4 = (
    "render.framework-core.install.v4"
)
RENDER_FRAMEWORK_CORE_INSTALL_V5 = (
    "render.framework-core.install.v5"
)
CLIENT_WORKSPACE_OWNERSHIP_INSTALL_V1 = (
    "client.workspace-ownership.install.v1"
)
CLIENT_WORKSPACE_INPUT_INSTALL_V1 = (
    "client.workspace-input.install.v1"
)
CLIENT_WORKSPACE_INPUT_DELEGATE_V1 = (
    "client.workspace-input.delegate.v1"
)
CLIENT_COALITION_GEOMETRY_INSTALL_V1 = (
    "client.coalition-geometry.install.v1"
)
CLIENT_COALITION_LIFECYCLE_INSTALL_V1 = (
    "client.coalition-lifecycle.install.v1"
)
CLIENT_FRAMEWORK_CORE_INSTALL_V2 = (
    "client.framework-core.install.v2"
)
CLIENT_FULL_UI_CORE_INSTALL_V1 = (
    "client.full-ui.core-install.v1"
)
CLIENT_FULL_UI_FRAMEWORK_INSTALL_V1 = (
    "client.full-ui.framework-install.v1"
)
CLIENT_FULL_UI_FRAMEWORK_INSTALL_V2 = (
    "client.full-ui.framework-install.v2"
)
CLIENT_FULL_UI_FRAMEWORK_INSTALL_V3 = (
    "client.full-ui.framework-install.v3"
)
CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4 = (
    "client.full-ui.framework-install.v4"
)
CLIENT_FULL_UI_BOOTSTRAP_V1 = (
    "client.full-ui.bootstrap.v1"
)
CLIENT_FULL_UI_EVENT_V1 = (
    "client.full-ui.event.v1"
)
CLIENT_FULL_UI_BEGIN_FRAME_V1 = (
    "client.full-ui.begin-frame.v1"
)
CLIENT_FULL_UI_OVERLAY_V1 = (
    "client.full-ui.overlay.v1"
)
CLIENT_ALPHA_TARGET_BOOTSTRAP_V1 = (
    "client.alpha-target.bootstrap.v1"
)
CLIENT_ALPHA_TARGET_EVENT_V1 = (
    "client.alpha-target.event.v1"
)
CLIENT_ALPHA_TARGET_OVERLAY_V1 = (
    "client.alpha-target.overlay.v1"
)
CLIENT_ALPHA_TARGET_INSTALL_V1 = (
    "client.alpha-target.install.v1"
)
CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1 = (
    "client.alpha-target-vitals.bootstrap.v1"
)
CLIENT_ALPHA_TARGET_VITALS_EVENT_V1 = (
    "client.alpha-target-vitals.event.v1"
)
CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1 = (
    "client.alpha-target-vitals.overlay.v1"
)
CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1 = (
    "client.alpha-target-vitals.install.v1"
)
CLIENT_MOD_LOADER_BOOTSTRAP_V1 = (
    "client.mod-loader.bootstrap.v1"
)
CLIENT_MOD_LOADER_EVENT_V1 = (
    "client.mod-loader.event.v1"
)
CLIENT_MOD_LOADER_BEGIN_FRAME_V1 = (
    "client.mod-loader.begin-frame.v1"
)
CLIENT_MOD_LOADER_OVERLAY_V1 = (
    "client.mod-loader.overlay.v1"
)
CLIENT_MOD_LOADER_INSTALL_V1 = (
    "client.mod-loader.install.v1"
)
RENDER_MOD_LOADER_REGION_V1 = (
    "render.mod-loader.region.v1"
)


@dataclass(frozen=True)
class AuthoredFragment:
    """One immutable, module-bound insertion reviewed in public source."""

    fragment_id: str
    module: str
    text: str
    sha256: str = field(init=False)
    size: int = field(init=False)

    def __post_init__(self) -> None:
        if (not self.fragment_id
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-_"
                       for character in self.fragment_id)):
            raise ValueError("authored fragment ID is invalid")
        if not self.module or not self.text:
            raise ValueError("authored fragment module and text are required")
        if "\x00" in self.text or "\r" in self.text or not self.text.endswith("\n"):
            raise ValueError("authored fragment text must be canonical LF UTF-8 text")
        payload = self.text.encode("utf-8")
        if len(payload) > MAX_AUTHORED_FRAGMENT_BYTES:
            raise ValueError("authored fragment exceeds its byte limit")
        object.__setattr__(self, "size", len(payload))
        object.__setattr__(
            self, "sha256", hashlib.sha256(payload).hexdigest().upper())

    @property
    def payload(self) -> bytes:
        return self.text.encode("utf-8")


_FRAGMENTS = (
    AuthoredFragment(
        RENDER_SHARED_CONTEXT_MENU_INSTALL_V1,
        "render_mixin",
        (
            "\n"
            "from star_empire_ui_mod.host_context_menu import SharedContextMenuBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    SharedContextMenuBridge,\n"
            "    globals(),\n"
            "    names=(\"_draw_shared_context_menu\",),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        RENDER_SHARED_CONTEXT_MENU_INSTALL_V2,
        "render_mixin",
        (
            "\n"
            "from star_empire_ui_mod.content import (\n"
            "    ContentMetrics as UiContentMetrics,\n"
            "    ContentRole as UiContentRole,\n"
            "    ContentRow as UiContentRow,\n"
            "    ContentStyle as UiContentStyle,\n"
            "    RowKind as UiRowKind,\n"
            "    TextSpan as UiTextSpan,\n"
            ")\n"
            "from star_empire_ui_mod.host_context_menu import SharedContextMenuBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.layout import Rect as UiRect\n"
            "from star_empire_ui_mod.pygame_content import PygameContentRenderer\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    SharedContextMenuBridge,\n"
            "    globals(),\n"
            "    names=(\"_draw_shared_context_menu\",),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        RENDER_COALITION_FRAMEWORK_INSTALL_V1,
        "render_mixin",
        (
            "\n"
            "from dataclasses import replace as _dataclass_replace\n"
            "from star_empire_ui_mod.content import (\n"
            "    ContentMetrics as UiContentMetrics,\n"
            "    ContentStyle as UiContentStyle,\n"
            ")\n"
            "from star_empire_ui_mod.host_coalition import CoalitionRendererBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.layout import Rect as UiRect\n"
            "from star_empire_ui_mod.pygame_content import PygameContentRenderer\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    CoalitionRendererBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_coalition_shared_content_context\",\n"
            "        \"_draw_coalition_framework_windows\",\n"
            "        \"_draw_coalition_framework_overlays\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        RENDER_FRAMEWORK_CORE_INSTALL_V3,
        "render_mixin",
        (
            "\n"
            "from dataclasses import replace as _dataclass_replace\n"
            "from star_empire_ui_mod.content import (\n"
            "    ContentMetrics as UiContentMetrics,\n"
            "    ContentRole as UiContentRole,\n"
            "    ContentRow as UiContentRow,\n"
            "    ContentStyle as UiContentStyle,\n"
            "    RowKind as UiRowKind,\n"
            "    TextSpan as UiTextSpan,\n"
            ")\n"
            "from star_empire_ui_mod.host_coalition import CoalitionRendererBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.layout import Rect as UiRect\n"
            "from star_empire_ui_mod.pygame_content import PygameContentRenderer\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    CoalitionRendererBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_draw_shared_context_menu\",\n"
            "        \"_coalition_shared_content_context\",\n"
            "        \"_draw_coalition_framework_windows\",\n"
            "        \"_draw_coalition_framework_overlays\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        RENDER_FRAMEWORK_CORE_INSTALL_V4,
        "render_mixin",
        (
            "\n"
            "from dataclasses import replace as _dataclass_replace\n"
            "from star_empire_ui_mod.content import (\n"
            "    ContentButton as UiContentButton,\n"
            "    ContentCard as UiContentCard,\n"
            "    ContentInput as UiContentInput,\n"
            "    ContentMetrics as UiContentMetrics,\n"
            "    ContentRole as UiContentRole,\n"
            "    ContentRow as UiContentRow,\n"
            "    ContentStyle as UiContentStyle,\n"
            "    RowKind as UiRowKind,\n"
            "    TextSpan as UiTextSpan,\n"
            ")\n"
            "from star_empire_ui_mod.host_coalition import CoalitionRendererBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.layout import Rect as UiRect\n"
            "from star_empire_ui_mod.pygame_content import PygameContentRenderer\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    CoalitionRendererBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_draw_shared_context_menu\",\n"
            "        \"_coalition_shared_content_context\",\n"
            "        \"_draw_coalition_framework_windows\",\n"
            "        \"_draw_coalition_framework_overlays\",\n"
            "        \"_draw_coalition_framework_modal\",\n"
            "        \"_draw_coalition_framework_popovers\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        RENDER_FRAMEWORK_CORE_INSTALL_V5,
        "render_mixin",
        (
            "\n"
            "import sys\n"
            "from dataclasses import replace as _dataclass_replace\n"
            "from star_empire_ui_mod.content import (\n"
            "    ContentButton as UiContentButton,\n"
            "    ContentCard as UiContentCard,\n"
            "    ContentInput as UiContentInput,\n"
            "    ContentMetrics as UiContentMetrics,\n"
            "    ContentRole as UiContentRole,\n"
            "    ContentRow as UiContentRow,\n"
            "    ContentStyle as UiContentStyle,\n"
            "    RowKind as UiRowKind,\n"
            "    TextSpan as UiTextSpan,\n"
            ")\n"
            "from star_empire_ui_mod.host_coalition import CoalitionRendererBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.layout import Rect as UiRect\n"
            "from star_empire_ui_mod.pygame_content import PygameContentRenderer\n"
            "\n"
            "install_bridge_methods(\n"
            "    RenderMixin,\n"
            "    CoalitionRendererBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_draw_shared_context_menu\",\n"
            "        \"_coalition_shared_content_context\",\n"
            "        \"_coalition_framework_rank_data\",\n"
            "        \"_draw_coalition_framework_windows\",\n"
            "        \"_draw_coalition_framework_permissions\",\n"
            "        \"_draw_coalition_framework_overlays\",\n"
            "        \"_draw_coalition_framework_modal\",\n"
            "        \"_draw_coalition_framework_popovers\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_WORKSPACE_OWNERSHIP_INSTALL_V1,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.pygame_workspace import (\n"
            "    WorkspaceHostBridge,\n"
            "    collect_live_drawn_windows,\n"
            "    topmost_window_at,\n"
            ")\n"
            "\n"
            "install_bridge_methods(\n"
            "    SolarSystemWindow,\n"
            "    WorkspaceHostBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_ui_mod_workspace_drawn_windows\",\n"
            "        \"_ui_mod_workspace_window_at\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_WORKSPACE_INPUT_INSTALL_V1,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "from star_empire_ui_mod.pygame_workspace import (\n"
            "    WorkspaceHostBridge,\n"
            "    collect_live_drawn_windows,\n"
            "    route_workspace_chrome_event,\n"
            "    topmost_window_at,\n"
            ")\n"
            "\n"
            "install_bridge_methods(\n"
            "    SolarSystemWindow,\n"
            "    WorkspaceHostBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_ui_mod_workspace_drawn_windows\",\n"
            "        \"_ui_mod_workspace_window_at\",\n"
            "        \"_handle_ui_mod_workspace_chrome_event\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_WORKSPACE_INPUT_DELEGATE_V1,
        "Client",
        (
            "        result = self._handle_ui_mod_workspace_chrome_event(\n"
            "            event, screen, window_ids=window_ids)\n"
            "        if result is None:\n"
            "            return None\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_COALITION_GEOMETRY_INSTALL_V1,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_coalition_client import CoalitionClientBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "\n"
            "install_bridge_methods(\n"
            "    SolarSystemWindow,\n"
            "    CoalitionClientBridge,\n"
            "    globals(),\n"
            "    names=(\"_clear_coalition_surface_geometry\",),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_COALITION_LIFECYCLE_INSTALL_V1,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_coalition_client import CoalitionClientBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_methods\n"
            "\n"
            "install_bridge_methods(\n"
            "    SolarSystemWindow,\n"
            "    CoalitionClientBridge,\n"
            "    globals(),\n"
            "    names=(\n"
            "        \"_initialize_ui_mod_coalition_state\",\n"
            "        \"_coalition_window_ids\",\n"
            "        \"_queue_coalition_window_visibility\",\n"
            "        \"_clear_coalition_surface_geometry\",\n"
            "        \"_apply_coalition_window_visibility\",\n"
            "        \"_set_coalition_window_visible\",\n"
            "        \"_sync_coalition_window_states\",\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FRAMEWORK_CORE_INSTALL_V2,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_coalition_client import CoalitionClientBridge\n"
            "from star_empire_ui_mod.host_method_bridge import install_bridge_groups\n"
            "from star_empire_ui_mod.pygame_workspace import (\n"
            "    WorkspaceHostBridge,\n"
            "    collect_live_drawn_windows,\n"
            "    route_workspace_chrome_event,\n"
            "    topmost_window_at,\n"
            ")\n"
            "\n"
            "install_bridge_groups(\n"
            "    SolarSystemWindow,\n"
            "    globals(),\n"
            "    groups=(\n"
            "        (WorkspaceHostBridge, (\n"
            "            \"_ui_mod_workspace_drawn_windows\",\n"
            "            \"_ui_mod_workspace_window_at\",\n"
            "            \"_handle_ui_mod_workspace_chrome_event\",\n"
            "        )),\n"
            "        (CoalitionClientBridge, (\n"
            "            \"_clear_coalition_surface_geometry\",\n"
            "        )),\n"
            "    ),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_CORE_INSTALL_V1,
        "Client",
        (
            "\n"
            "from star_empire_ui_mod.host_client_core import (\n"
            "    FullClientCoreBridge as _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "    initialize_full_ui as _SEUI_FULL_INITIALIZE,\n"
            ")\n"
            "from star_empire_ui_mod.host_method_bridge import (\n"
            "    install_bridge_methods as _SEUI_INSTALL_BRIDGE_METHODS,\n"
            ")\n"
            "\n"
            "_SEUI_INSTALL_BRIDGE_METHODS(\n"
            "    SolarSystemWindow,\n"
            "    _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "    globals(),\n"
            "    names=(\"_initialize_ui_mod_full\",),\n"
            ")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_FRAMEWORK_INSTALL_V1,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_client_core import (\n"
            "        FullClientCoreBridge as _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "        initialize_full_ui as _SEUI_FULL_INITIALIZE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_input import (\n"
            "        FullClientInputBridge as _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "        route_full_ui_input as _SEUI_FULL_ROUTE_INPUT,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_render import (\n"
            "        FullClientRenderBridge as _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "        begin_full_ui_frame as _SEUI_FULL_BEGIN_DRAW,\n"
            "        flush_full_ui_frame as _SEUI_FULL_FLUSH_DRAW,\n"
            "        register_full_ui_draw as _SEUI_FULL_REGISTER_DRAW,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_method_bridge import (\n"
            "        install_bridge_bundle as _SEUI_INSTALL_BRIDGE_BUNDLE,\n"
            "    )\n"
            "    _SEUI_INSTALL_BRIDGE_BUNDLE(\n"
            "        method_groups=(\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "             (\"_initialize_ui_mod_full\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "             (\"_handle_ui_mod_full_input\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "             (\"_ui_mod_begin_full_draw_frame\",\n"
            "              \"_ui_mod_register_full_draw\",\n"
            "              \"_draw_ui_mod_full_frame\")),\n"
            "        ),\n"
            "    )\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_FULL_FRAMEWORK_INSTALL_FAILED; using vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_FRAMEWORK_INSTALL_V2,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_client_core import (\n"
            "        FullClientCoreBridge as _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "        disable_full_ui as _SEUI_DISABLE_FULL_UI,\n"
            "        initialize_full_ui as _SEUI_FULL_INITIALIZE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_input import (\n"
            "        FullClientInputBridge as _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "        route_full_ui_input as _SEUI_FULL_ROUTE_INPUT,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_render import (\n"
            "        FullClientRenderBridge as _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "        begin_full_ui_frame as _SEUI_FULL_BEGIN_DRAW,\n"
            "        flush_full_ui_frame as _SEUI_FULL_FLUSH_DRAW,\n"
            "        register_full_ui_draw as _SEUI_FULL_REGISTER_DRAW,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_legacy_panels import (\n"
            "        LegacyPanelRendererBridge as _SEUI_LEGACY_PANEL_BRIDGE,\n"
            "        render_legacy_panel as _SEUI_RENDER_LEGACY_PANEL,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_method_bridge import (\n"
            "        install_bridge_bundle as _SEUI_INSTALL_BRIDGE_BUNDLE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_target import (\n"
            "        _call_original as _SEUI_CALL_ORIGINAL_PANEL,\n"
            "        render_target_panel as _SEUI_RENDER_TARGET_PANEL,\n"
            "    )\n"
            "    _SEUI_PYGAME = pygame\n"
            "    _SEUI_INSTALL_BRIDGE_BUNDLE(\n"
            "        method_groups=(\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "             (\"_initialize_ui_mod_full\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "             (\"_handle_ui_mod_full_input\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "             (\"_ui_mod_begin_full_draw_frame\",\n"
            "              \"_ui_mod_register_full_draw\",\n"
            "              \"_draw_ui_mod_full_frame\")),\n"
            "        ),\n"
            "        wrappers=((\n"
            "            SolarSystemWindow, globals(), RenderMixin,\n"
            "            _SEUI_LEGACY_PANEL_BRIDGE, \"_draw_panel\",\n"
            "            \"_SEUI_LEGACY_ORIGINAL_DRAW_PANEL\",\n"
            "        ),),\n"
            "    )\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_FULL_FRAMEWORK_V2_INSTALL_FAILED; using vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_FRAMEWORK_INSTALL_V3,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_chat import (\n"
            "        ChatRendererBridge as _SEUI_CHAT_RENDERER_BRIDGE,\n"
            "        render_chat_window as _SEUI_RENDER_CHAT,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_core import (\n"
            "        FullClientCoreBridge as _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "        disable_full_ui as _SEUI_DISABLE_FULL_UI,\n"
            "        initialize_full_ui as _SEUI_FULL_INITIALIZE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_input import (\n"
            "        FullClientInputBridge as _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "        route_full_ui_input as _SEUI_FULL_ROUTE_INPUT,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_client_render import (\n"
            "        FullClientRenderBridge as _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "        begin_full_ui_frame as _SEUI_FULL_BEGIN_DRAW,\n"
            "        flush_full_ui_frame as _SEUI_FULL_FLUSH_DRAW,\n"
            "        register_full_ui_draw as _SEUI_FULL_REGISTER_DRAW,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_legacy_panels import (\n"
            "        LegacyPanelRendererBridge as _SEUI_LEGACY_PANEL_BRIDGE,\n"
            "        render_legacy_panel as _SEUI_RENDER_LEGACY_PANEL,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_method_bridge import (\n"
            "        install_bridge_bundle as _SEUI_INSTALL_BRIDGE_BUNDLE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_target import (\n"
            "        _call_original as _SEUI_CALL_ORIGINAL_PANEL,\n"
            "        render_target_panel as _SEUI_RENDER_TARGET_PANEL,\n"
            "    )\n"
            "    _SEUI_PYGAME = pygame\n"
            "    _SEUI_INSTALL_BRIDGE_BUNDLE(\n"
            "        method_groups=(\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_CORE_BRIDGE,\n"
            "             (\"_initialize_ui_mod_full\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_INPUT_BRIDGE,\n"
            "             (\"_handle_ui_mod_full_input\",)),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_FULL_CLIENT_RENDER_BRIDGE,\n"
            "             (\"_ui_mod_begin_full_draw_frame\",\n"
            "              \"_ui_mod_register_full_draw\",\n"
            "              \"_draw_ui_mod_full_frame\")),\n"
            "        ),\n"
            "        wrappers=(\n"
            "            (SolarSystemWindow, globals(), RenderMixin,\n"
            "             _SEUI_LEGACY_PANEL_BRIDGE, \"_draw_panel\",\n"
            "             \"_SEUI_LEGACY_ORIGINAL_DRAW_PANEL\"),\n"
            "            (SolarSystemWindow, globals(), RenderMixin,\n"
            "             _SEUI_CHAT_RENDERER_BRIDGE, \"_draw_chat_panel\",\n"
            "             \"_SEUI_CHAT_ORIGINAL_DRAW\"),\n"
            "        ),\n"
            "    )\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_FULL_FRAMEWORK_V3_INSTALL_FAILED; using vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_full_install import (\n"
            "        install_full_framework as _SEUI_INSTALL_FULL_FRAMEWORK,\n"
            "    )\n"
            "    _SEUI_INSTALL_FULL_FRAMEWORK(\n"
            "        SolarSystemWindow, RenderMixin, globals(), pygame)\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_FULL_FRAMEWORK_V4_INSTALL_FAILED; using vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_BOOTSTRAP_V1,
        "Client",
        (
            "        _seui_full_init = getattr(\n"
            "            self, \"_initialize_ui_mod_full\", None)\n"
            "        if _seui_full_init is not None:\n"
            "            _seui_full_init(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_EVENT_V1,
        "Client",
        (
            "                _seui_full_event = getattr(\n"
            "                    self, \"_handle_ui_mod_full_input\", None)\n"
            "                _seui_full_blocked = bool(\n"
            "                    self._disconnected\n"
            "                    or self._turret_layout_open\n"
            "                    or self._profiler_results_open\n"
            "                    or self._server_profiler_results_open\n"
            "                    or self._server_net_profiler_results_open\n"
            "                    or self._server_db_profiler_results_open)\n"
            "                if (event.type != pygame.QUIT\n"
            "                        and _seui_full_event is not None\n"
            "                        and _seui_full_event(\n"
            "                            event, screen,\n"
            "                            modal_blocked=_seui_full_blocked)):\n"
            "                    continue\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_BEGIN_FRAME_V1,
        "Client",
        (
            "            _seui_full_begin = getattr(\n"
            "                self, \"_ui_mod_begin_full_draw_frame\", None)\n"
            "            if _seui_full_begin is not None:\n"
            "                _seui_full_begin()\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_FULL_UI_OVERLAY_V1,
        "Client",
        (
            "            _seui_full_draw = getattr(\n"
            "                self, \"_draw_ui_mod_full_frame\", None)\n"
            "            if _seui_full_draw is not None:\n"
            "                _seui_full_draw(_frame)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_BOOTSTRAP_V1,
        "Client",
        (
            "        _seui_alpha_init = getattr(\n"
            "            self, \"_initialize_ui_mod_target_alpha\", None)\n"
            "        if _seui_alpha_init is not None:\n"
            "            _seui_alpha_init(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_EVENT_V1,
        "Client",
        (
            "                _seui_alpha_event = getattr(\n"
            "                    self, \"_handle_ui_mod_target_alpha_event\", None)\n"
            "                if (event.type != pygame.QUIT\n"
            "                        and _seui_alpha_event is not None\n"
            "                        and _seui_alpha_event(event, screen)):\n"
            "                    continue\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_OVERLAY_V1,
        "Client",
        (
            "            _seui_alpha_draw = getattr(\n"
            "                self, \"_draw_ui_mod_target_alpha\", None)\n"
            "            if _seui_alpha_draw is not None:\n"
            "                _seui_alpha_draw(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_INSTALL_V1,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_alpha_client import (\n"
            "        AlphaClientBridge as _SEUI_ALPHA_CLIENT_BRIDGE,\n"
            "        draw_target_alpha as _SEUI_ALPHA_DRAW,\n"
            "        handle_target_alpha_event as _SEUI_ALPHA_HANDLE_EVENT,\n"
            "        initialize_target_alpha as _SEUI_ALPHA_INITIALIZE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_method_bridge import (\n"
            "        install_bridge_bundle as _SEUI_INSTALL_BRIDGE_BUNDLE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_target import (\n"
            "        TargetRendererBridge as _SEUI_TARGET_RENDERER_BRIDGE,\n"
            "        _call_original as _SEUI_CALL_ORIGINAL_PANEL,\n"
            "        render_target_panel as _SEUI_RENDER_TARGET_PANEL,\n"
            "    )\n"
            "    _SEUI_PYGAME = pygame\n"
            "    _SEUI_INSTALL_BRIDGE_BUNDLE(\n"
            "        method_groups=((\n"
            "            SolarSystemWindow, globals(),\n"
            "            _SEUI_ALPHA_CLIENT_BRIDGE,\n"
            "            (\"_initialize_ui_mod_target_alpha\",\n"
            "             \"_handle_ui_mod_target_alpha_event\",\n"
            "             \"_draw_ui_mod_target_alpha\"),\n"
            "        ),),\n"
            "        wrappers=((\n"
            "            SolarSystemWindow, globals(), RenderMixin,\n"
            "            _SEUI_TARGET_RENDERER_BRIDGE, \"_draw_panel\",\n"
            "            \"_SEUI_TARGET_ORIGINAL_DRAW_PANEL\",\n"
            "        ),),\n"
            "    )\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_ALPHA_INSTALL_FAILED; continuing with vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1,
        "Client",
        (
            "        _seui_alpha_init = getattr(\n"
            "            self, \"_initialize_ui_mod_target_vitals_alpha\", None)\n"
            "        if _seui_alpha_init is not None:\n"
            "            _seui_alpha_init(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_VITALS_EVENT_V1,
        "Client",
        (
            "                _seui_alpha_event = getattr(\n"
            "                    self, \"_handle_ui_mod_target_vitals_alpha_event\", None)\n"
            "                if (event.type != pygame.QUIT\n"
            "                        and _seui_alpha_event is not None\n"
            "                        and _seui_alpha_event(event, screen)):\n"
            "                    continue\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1,
        "Client",
        (
            "            _seui_alpha_draw = getattr(\n"
            "                self, \"_draw_ui_mod_target_vitals_alpha\", None)\n"
            "            if _seui_alpha_draw is not None:\n"
            "                _seui_alpha_draw(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_ui_mod.host_method_bridge import (\n"
            "        install_bridge_bundle as _SEUI_INSTALL_BRIDGE_BUNDLE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_target import (\n"
            "        TargetRendererBridge as _SEUI_TARGET_RENDERER_BRIDGE,\n"
            "        _call_original as _SEUI_CALL_ORIGINAL_PANEL,\n"
            "        render_target_panel as _SEUI_RENDER_TARGET_PANEL,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_target_vitals_client import (\n"
            "        TargetVitalsClientBridge as _SEUI_TARGET_VITALS_CLIENT_BRIDGE,\n"
            "        draw_target_vitals_alpha as _SEUI_TARGET_VITALS_DRAW,\n"
            "        handle_target_vitals_alpha_event as _SEUI_TARGET_VITALS_HANDLE_EVENT,\n"
            "        initialize_target_vitals_alpha as _SEUI_TARGET_VITALS_INITIALIZE,\n"
            "    )\n"
            "    from star_empire_ui_mod.host_vitals import (\n"
            "        VitalsRendererBridge as _SEUI_VITALS_RENDERER_BRIDGE,\n"
            "        render_vitals_window as _SEUI_RENDER_VITALS_WINDOW,\n"
            "    )\n"
            "    _SEUI_PYGAME = pygame\n"
            "    _SEUI_INSTALL_BRIDGE_BUNDLE(\n"
            "        method_groups=(\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_TARGET_VITALS_CLIENT_BRIDGE,\n"
            "             (\"_initialize_ui_mod_target_vitals_alpha\",\n"
            "              \"_handle_ui_mod_target_vitals_alpha_event\",\n"
            "              \"_draw_ui_mod_target_vitals_alpha\")),\n"
            "            (SolarSystemWindow, globals(),\n"
            "             _SEUI_VITALS_RENDERER_BRIDGE,\n"
            "             (\"_draw_ui_mod_vitals_frontmost\",)),\n"
            "        ),\n"
            "        wrappers=(\n"
            "            (SolarSystemWindow, globals(), RenderMixin,\n"
            "             _SEUI_TARGET_RENDERER_BRIDGE, \"_draw_panel\",\n"
            "             \"_SEUI_TARGET_ORIGINAL_DRAW_PANEL\"),\n"
            "            (SolarSystemWindow, globals(), SolarSystemWindow,\n"
            "             _SEUI_VITALS_RENDERER_BRIDGE,\n"
            "             \"_draw_hud_top_panel\",\n"
            "             \"_SEUI_VITALS_ORIGINAL_DRAW\"),\n"
            "        ),\n"
            "    )\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"UI_MOD_TARGET_VITALS_INSTALL_FAILED; using vanilla UI\")\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_MOD_LOADER_BOOTSTRAP_V1,
        "Client",
        (
            "        _semod_loader_init = getattr(\n"
            "            self, \"_initialize_star_empire_mod_loader\", None)\n"
            "        if _semod_loader_init is not None:\n"
            "            _semod_loader_init(screen)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_MOD_LOADER_EVENT_V1,
        "Client",
        (
            "                _semod_loader_event = getattr(\n"
            "                    self, \"_handle_star_empire_mod_event\", None)\n"
            "                if (_semod_loader_event is not None\n"
            "                        and _semod_loader_event(event, screen)):\n"
            "                    continue\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
        "Client",
        (
            "            _semod_loader_begin = getattr(\n"
            "                self, \"_begin_star_empire_mod_frame\", None)\n"
            "            if _semod_loader_begin is not None:\n"
            "                _semod_loader_begin(_frame)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_MOD_LOADER_OVERLAY_V1,
        "Client",
        (
            "            _semod_loader_draw = getattr(\n"
            "                self, \"_draw_star_empire_mod_overlay\", None)\n"
            "            if _semod_loader_draw is not None:\n"
            "                _semod_loader_draw(screen, _frame)\n"
        ),
    ),
    AuthoredFragment(
        RENDER_MOD_LOADER_REGION_V1,
        "render_mixin",
        (
            "                _semod_region_draw = getattr(\n"
            "                    self, \"_draw_star_empire_mod_region\", None)\n"
            "                if _semod_region_draw is not None:\n"
            "                    _semod_region_rect = pygame.Rect(\n"
            "                        OV_X + self._s(20), _iy,\n"
            "                        OV_W - self._s(40),\n"
            "                        max(0, OV_Y + OV_H - self._s(8) - _iy))\n"
            "                    _iy += _semod_region_draw(\n"
            "                        \"player_station.storage.filters\",\n"
            "                        screen, _semod_region_rect)\n"
        ),
    ),
    AuthoredFragment(
        CLIENT_MOD_LOADER_INSTALL_V1,
        "Client",
        (
            "\n"
            "try:\n"
            "    from star_empire_mod_loader.host_client import (\n"
            "        install_mod_loader_bridge as _SEMOD_INSTALL_LOADER,\n"
            "    )\n"
            "    _SEMOD_INSTALL_LOADER(SolarSystemWindow, pygame)\n"
            "except Exception:\n"
            "    logger.exception(\n"
            "        \"STAR_EMPIRE_MOD_LOADER_INSTALL_FAILED; continuing without mods\")\n"
        ),
    ),
)

if len({fragment.fragment_id for fragment in _FRAGMENTS}) != len(_FRAGMENTS):
    raise RuntimeError("authored fragment IDs must be unique")

AUTHORED_FRAGMENTS: Mapping[str, AuthoredFragment] = MappingProxyType({
    fragment.fragment_id: fragment for fragment in _FRAGMENTS
})

# Audit history stays immutable and resolvable, but only this reviewed set may
# enter a recipe/package. Superseded or incomplete fragments therefore cannot
# be revived by changing data in an otherwise correctly signed package.
ACTIVE_AUTHORED_FRAGMENT_IDS = frozenset({
    RENDER_FRAMEWORK_CORE_INSTALL_V5,
    CLIENT_FRAMEWORK_CORE_INSTALL_V2,
    CLIENT_WORKSPACE_INPUT_DELEGATE_V1,
    CLIENT_ALPHA_TARGET_BOOTSTRAP_V1,
    CLIENT_ALPHA_TARGET_EVENT_V1,
    CLIENT_ALPHA_TARGET_OVERLAY_V1,
    CLIENT_ALPHA_TARGET_INSTALL_V1,
    CLIENT_ALPHA_TARGET_VITALS_BOOTSTRAP_V1,
    CLIENT_ALPHA_TARGET_VITALS_EVENT_V1,
    CLIENT_ALPHA_TARGET_VITALS_OVERLAY_V1,
    CLIENT_ALPHA_TARGET_VITALS_INSTALL_V1,
    CLIENT_FULL_UI_BOOTSTRAP_V1,
    CLIENT_FULL_UI_EVENT_V1,
    CLIENT_FULL_UI_BEGIN_FRAME_V1,
    CLIENT_FULL_UI_OVERLAY_V1,
    CLIENT_FULL_UI_FRAMEWORK_INSTALL_V4,
    CLIENT_MOD_LOADER_BOOTSTRAP_V1,
    CLIENT_MOD_LOADER_EVENT_V1,
    CLIENT_MOD_LOADER_BEGIN_FRAME_V1,
    CLIENT_MOD_LOADER_OVERLAY_V1,
    CLIENT_MOD_LOADER_INSTALL_V1,
    RENDER_MOD_LOADER_REGION_V1,
})
ACTIVE_AUTHORED_FRAGMENTS: Mapping[str, AuthoredFragment] = MappingProxyType({
    fragment_id: AUTHORED_FRAGMENTS[fragment_id]
    for fragment_id in ACTIVE_AUTHORED_FRAGMENT_IDS
})


def resolve_authored_fragment(fragment_id: str) -> AuthoredFragment:
    """Return one exact catalog fragment or raise for an unknown ID."""
    try:
        return AUTHORED_FRAGMENTS[fragment_id]
    except KeyError as error:
        raise KeyError(f"unknown authored fragment: {fragment_id}") from error


def resolve_active_authored_fragment(fragment_id: str) -> AuthoredFragment:
    """Return a release-approved fragment; retain inactive entries for audit."""
    fragment = resolve_authored_fragment(fragment_id)
    if fragment_id not in ACTIVE_AUTHORED_FRAGMENTS:
        raise KeyError(f"authored fragment is not active: {fragment_id}")
    return fragment
