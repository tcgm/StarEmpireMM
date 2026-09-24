"""Tkinter UI for the fail-closed Star Empire Mod Manager."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
from queue import Empty, SimpleQueue
import subprocess
import sys
from threading import Thread
from tkinter import (BOTH, DISABLED, END, LEFT, NORMAL, RIGHT, BooleanVar,
                     Button, PhotoImage, StringVar, TclError, Text, Tk,
                     Toplevel)
from tkinter import filedialog, messagebox, ttk
import uuid

from .candidate_builder import (BuiltCandidate, CandidateBuildError,
                                build_candidate)
from .diagnostics import (DiagnosticsError, current_diagnostics_text,
                          export_diagnostics)
from .manager_core import (
    CLIENT_EXE,
    Inspection,
    InstallStatus,
    LAUNCHER_EXE,
    ManagerDataError,
    default_state_path,
    inspect_installation,
    load_install_state,
    read_game_version,
    running_game_processes,
    sha256_file,
)
from .manager_transaction import ManagerTransaction, TransactionError
from .manager_mods import ModManagerController
from .manager_settings import (
    ManagerSettings, ManagerSettingsError, SETTINGS_FILENAME,
    load_manager_settings, save_manager_settings,
)
from .mod_registry import InstalledMod, ModRegistryError
from .mod_service import ModServiceError
from .mod_updates import ModUpdateError
from .mod_package import (PackageError, VerifiedModPackage,
                          verify_mod_package)
from .semod_package import SemodPackageError, verify_semod_package
from .trusted_keys import load_trust_configuration, load_trusted_keys
from .update_feed import (
    FeedError,
    UpdateFeed,
    UpdateFeedIndex,
    accept_update_feed_sequence,
    download_update_package,
    fetch_update_feed,
    package_filename,
    select_update_release,
)
from .version import runtime_identity
from .windows_drop import (create_drop_root, install_windows_file_drop,
                           unique_semod_paths)


LIGHT_PALETTE = {
    "bg": "#f0f0f0", "fg": "#1a1a1a", "field_bg": "#ffffff",
    "select_bg": "#0a5fd1", "select_fg": "#ffffff",
    "border": "#c9c9c9", "trough": "#d9d9d9",
}
DARK_PALETTE = {
    "bg": "#1e1f22", "fg": "#e6e6e6", "field_bg": "#2b2d31",
    "select_bg": "#3a6df0", "select_fg": "#ffffff",
    "border": "#3a3b3e", "trough": "#3a3b3e",
}


def _theme_text_widget(widget: Text, palette: dict[str, str]) -> None:
    """Colour a plain tk Text widget to match the current ttk palette."""
    widget.configure(
        background=palette["field_bg"], foreground=palette["fg"],
        insertbackground=palette["fg"],
        selectbackground=palette["select_bg"],
        selectforeground=palette["select_fg"])


SELECTED_GAME_FILENAME = "selected-game.txt"
MOD_README_MAX_BYTES = 256 * 1024
MOD_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
MOD_PREVIEW_MAX_WIDTH = 360
MOD_PREVIEW_MAX_HEIGHT = 220
MOD_REGISTRY_POLL_MS = 1000


@dataclass(frozen=True)
class ModDisplayDetails:
    """Bounded, package-declared information safe to show in the Manager."""

    title: str
    readme: str
    preview_png: bytes | None


@dataclass(frozen=True)
class ModCompatibilityView:
    compatible: bool | None
    supported: str
    status: str
    detail: str


def mod_compatibility_view(
        compatibility, force_load: bool, game_version: str,
        client_sha256: str = "") -> ModCompatibilityView:
    """Describe declared support without weakening the runtime decision."""
    version = str(game_version).strip()
    digest = str(client_sha256).strip().upper()
    if compatibility is None:
        return ModCompatibilityView(
            True, "Any", "COMPATIBLE",
            "Compatible: this mod does not restrict Star Empire versions.")
    if compatibility.game_versions:
        supported = ", ".join(compatibility.game_versions)
        can_check = bool(version)
    else:
        versions = tuple(dict.fromkeys(
            item.game_version for item in compatibility.game_builds))
        supported = (
            ", ".join(versions) + " (exact build)" if versions
            else "No approved builds")
        can_check = bool(version and len(digest) == 64)
    if not can_check:
        return ModCompatibilityView(
            None, supported, "UNKNOWN",
            "Compatibility unknown: choose and verify the Star Empire game folder.")
    compatible = compatibility.supports(version, digest)
    if compatible:
        return ModCompatibilityView(
            True, supported, "COMPATIBLE",
            f"Compatible with Star Empire {version}. Declared support: {supported}.")
    if force_load:
        return ModCompatibilityView(
            False, supported, "FORCED",
            f"WARNING: this mod declares {supported}, but the selected game is "
            f"{version}. Force load is ON and may crash or behave incorrectly.")
    return ModCompatibilityView(
        False, supported, "INCOMPATIBLE",
        f"WARNING: this mod declares {supported}, but the selected game is "
        f"{version}. It will not load unless Force load anyway is enabled.")


def _read_declared_mod_file(
        installed: InstalledMod, filename: str, maximum: int,
        *, allow_truncate: bool = False) -> tuple[bytes | None, bool]:
    namespace = installed.manifest.mod_id.replace("-", "_")
    relative = PurePosixPath("mod", *namespace.split("."), filename)
    if relative.as_posix() not in installed.manifest.files:
        return None, False
    try:
        install_root = Path(installed.install_path).resolve(strict=True)
        candidate = Path(installed.install_path).joinpath(*relative.parts)
        if candidate.is_symlink():
            return None, False
        resolved = candidate.resolve(strict=True)
        if (not resolved.is_file()
                or not resolved.is_relative_to(install_root)):
            return None, False
        with resolved.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except (OSError, RuntimeError):
        return None, False
    if len(payload) <= maximum:
        return payload, False
    if allow_truncate:
        return payload[:maximum], True
    return None, True


def load_mod_display_details(installed: InstalledMod) -> ModDisplayDetails:
    """Load one installed mod's README and optional PNG without trusting paths."""
    title = f"{installed.manifest.name}  {installed.manifest.version}"
    readme_bytes, truncated = _read_declared_mod_file(
        installed, "README.md", MOD_README_MAX_BYTES, allow_truncate=True)
    if readme_bytes is not None:
        readme = readme_bytes.decode("utf-8", errors="replace").strip()
        if truncated:
            readme += "\n\n[README truncated by the Mod Manager.]"
    else:
        description = installed.manifest.description.strip()
        readme = description or "No description was provided for this mod."
        readme += "\n\nThis installed mod does not include a readable README.md."
    preview_png, _oversized = _read_declared_mod_file(
        installed, "preview.png", MOD_PREVIEW_MAX_BYTES)
    return ModDisplayDetails(title, readme, preview_png)


def load_selected_game(path: Path) -> Path | None:
    """Read the last selected game folder from Manager-owned state."""
    source = Path(path).expanduser().resolve()
    if not source.exists():
        return None
    if not source.is_file():
        raise ManagerDataError("saved game selection is not a file")
    try:
        value = source.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise ManagerDataError("saved game selection cannot be read") from error
    if (not value or len(value) > 32767
            or "\x00" in value or "\n" in value or "\r" in value):
        raise ManagerDataError("saved game selection is invalid")
    return Path(value).expanduser().resolve()


def save_selected_game(path: Path, game_root: Path) -> Path:
    """Atomically remember a game folder without writing inside the game."""
    target = Path(path).expanduser().resolve()
    value = str(Path(game_root).expanduser().resolve())
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ManagerDataError("selected game path is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    try:
        with pending.open("xb") as stream:
            stream.write((value + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(target)
    except Exception as error:
        pending.unlink(missing_ok=True)
        if isinstance(error, ManagerDataError):
            raise
        raise ManagerDataError("selected game folder could not be remembered") from error
    return target


@dataclass(frozen=True)
class GameBuildIdentity:
    game_root: Path
    game_version: str
    official_client_sha256: str
    current_client_sha256: str


@dataclass(frozen=True)
class UpdateResolution:
    identity: GameBuildIdentity
    package: VerifiedModPackage | None


def detect_game_build_identity(
        game_root: Path, state_path: Path) -> GameBuildIdentity:
    """Resolve the official build from vanilla bytes or a verified backup."""
    root = Path(game_root).expanduser().resolve()
    client = root / CLIENT_EXE
    if not client.is_file():
        raise FeedError(f"Client.exe was not found in {root}")
    try:
        version = read_game_version(root / "version.txt")
        state = load_install_state(Path(state_path))
    except ManagerDataError as error:
        raise FeedError("cannot verify the selected game build") from error
    if not version:
        raise FeedError("the selected game has no verified version")
    current = sha256_file(client)
    official = current
    if (state is not None
            and state.game_root.resolve() == root
            and current == state.installed_sha256):
        if (not state.original_client.is_file()
                or sha256_file(state.original_client) != state.original_sha256):
            raise FeedError(
                "the installed mod's recorded vanilla backup does not verify")
        official = state.original_sha256
    return GameBuildIdentity(root, version, official, current)


def _verify_release_package(
        package: VerifiedModPackage, release: UpdateFeed,
        identity: GameBuildIdentity) -> None:
    compatibility = package.compatibility
    if (compatibility.pack_id != release.pack_id
            or compatibility.mod_version != release.mod_version
            or compatibility.game_version != identity.game_version
            or compatibility.official_client_sha256
            != identity.official_client_sha256
            or compatibility.key_id != release.key_id):
        raise FeedError(
            "signed package does not match the selected official game build")


def acquire_signed_update(
        game_root: Path, state_path: Path, feed_url: str,
        trusted_keys, downloads_root: Path) -> UpdateResolution:
    """Find and cache only the release signed for the exact selected build."""
    identity = detect_game_build_identity(game_root, state_path)
    feed_document = fetch_update_feed(feed_url, trusted_keys)
    if isinstance(feed_document, UpdateFeedIndex):
        accept_update_feed_sequence(
            feed_document, Path(state_path).parent / "feed-sequences.json")
        release = select_update_release(
            feed_document, identity.game_version,
            identity.official_client_sha256)
    elif isinstance(feed_document, UpdateFeed):
        raise FeedError(
            "legacy single-release feeds are not accepted by automatic updates")
    else:
        raise FeedError("signed update feed returned an unsupported result")
    if release is None:
        if detect_game_build_identity(game_root, state_path) != identity:
            raise FeedError("the selected game changed during the update check")
        return UpdateResolution(identity, None)

    destination = Path(downloads_root) / package_filename(release)
    if destination.is_file():
        if sha256_file(destination) != release.package_sha256:
            raise FeedError(
                f"existing download has unexpected bytes: {destination}")
        package = verify_mod_package(destination, trusted_keys)
    else:
        package = download_update_package(
            release, destination, trusted_keys)
    _verify_release_package(package, release, identity)
    if detect_game_build_identity(game_root, state_path) != identity:
        raise FeedError("the selected game changed during the update check")
    return UpdateResolution(identity, package)


def manager_bundle_root() -> Path:
    """Return the folder containing the distributed Manager executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def manager_internal_loader_root() -> Path:
    """Return the private frozen-resource folder for compatibility support."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve() / "embedded_loaders"
    return manager_bundle_root() / "embedded_loaders"


def discover_local_loader(
        game_root: Path, state_path: Path, bundle_root: Path,
        trusted_keys, *,
        allow_compatibility_fallback: bool = False,
        strict_internal_inventory: bool = False) -> VerifiedModPackage | None:
    """Find authenticated internal compatibility support for a game build."""
    identity = detect_game_build_identity(game_root, state_path)
    root = Path(bundle_root).expanduser().resolve()
    packages: dict[str, VerifiedModPackage] = {}
    exact_matches: dict[str, VerifiedModPackage] = {}
    invalid_packages: list[tuple[Path, Exception]] = []
    for pattern in ("*.seloader", "*.seuimod"):
        for candidate in sorted(root.glob(pattern)):
            try:
                package = verify_mod_package(candidate, trusted_keys)
                compatibility = package.compatibility
            except (PackageError, ManagerDataError, OSError) as error:
                invalid_packages.append((candidate, error))
                continue
            packages.setdefault(package.manifest_sha256, package)
            if (compatibility.game_version != identity.game_version
                    or compatibility.official_client_sha256
                    != identity.official_client_sha256):
                continue
            exact_matches.setdefault(package.manifest_sha256, package)
    if strict_internal_inventory and invalid_packages:
        candidate, error = invalid_packages[0]
        raise PackageError(
            "the bundled compatibility template could not be verified "
            f"({candidate.name}): {error}")
    if len(exact_matches) > 1:
        raise PackageError(
            "multiple distinct signed compatibility bindings match this game build")
    if exact_matches:
        return next(iter(exact_matches.values()))
    if not allow_compatibility_fallback:
        return None
    if len(packages) > 1:
        raise PackageError(
            "multiple internal compatibility templates are available for an "
            "unlisted game build")
    return next(iter(packages.values()), None)


@dataclass(frozen=True)
class ActionAvailability:
    build: bool = False
    install: bool = False
    update: bool = False
    restore: bool = False
    repair: bool = False


def action_availability(inspection: Inspection | None,
                        candidate: BuiltCandidate | None,
                        interrupted: bool,
                        compatibility_test: bool = False) -> ActionAvailability:
    """Derive every mutating button from inspected state, never UI history."""
    if interrupted:
        return ActionAvailability(repair=True)
    if inspection is None:
        return ActionAvailability()
    build = inspection.can_install or inspection.can_update or compatibility_test
    candidate_ready = False
    compatibility_ready = False
    if build and candidate is not None and inspection.pack is not None:
        try:
            candidate.verify_for(inspection.pack)
            candidate_ready = True
            compatibility_ready = (
                compatibility_test and candidate.compatibility_test
                and candidate.official_sha256 == inspection.current_sha256
                and candidate.observed_version == inspection.version)
        except (CandidateBuildError, OSError):
            candidate_ready = False
    return ActionAvailability(
        build=build,
        install=((inspection.can_install and candidate_ready)
                 or compatibility_ready),
        update=inspection.can_update and candidate_ready,
        restore=inspection.can_restore,
    )


def compatibility_test_available(
        inspection: Inspection | None,
        package: VerifiedModPackage | None) -> bool:
    """Allow a warning-gated test only for a readable, authenticated host."""
    return bool(
        inspection is not None
        and inspection.status is InstallStatus.UNSUPPORTED_VANILLA
        and inspection.current_sha256 is not None
        and inspection.version is not None
        and inspection.pack is not None
        and package is not None
        and package.manifest.get("schema") == 2
        and package.release_binding is not None
        and package.release_profile is not None
        and package.release_binding.ui_source_sha256
    )


def selected_game_matches_inspection(
        selected: str, inspection: Inspection | None) -> bool:
    """Require mutating actions to use the exact path that was inspected."""
    if inspection is None or not selected.strip():
        return False
    try:
        return (Path(selected).expanduser().resolve()
                == inspection.game_root.resolve())
    except OSError:
        return False


class SetupProgressDialog:
    """Small player-facing record of an automatic compatibility operation."""

    def __init__(self, root: Tk, palette: dict[str, str] = LIGHT_PALETTE) -> None:
        self.window = Toplevel(root)
        self.window.title("Preparing Star Empire for mods")
        self.window.geometry("640x390")
        self.window.minsize(540, 330)
        self.window.transient(root)
        self.window.protocol("WM_DELETE_WINDOW", lambda: None)
        self.status = StringVar(value="Starting automatic setup…")
        self._finished = False
        self._step = 0

        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer, text="AUTOMATIC GAME SETUP",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer, textvariable=self.status, wraplength=580,
        ).pack(anchor="w", pady=(6, 10))
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))
        self.progress.start(12)
        self.log = Text(
            outer, wrap="word", state=DISABLED, undo=False,
            font=("Segoe UI", 10), padx=10, pady=10, height=12,
        )
        _theme_text_widget(self.log, palette)
        self.log.pack(fill=BOTH, expand=True)
        self.close_button = ttk.Button(
            outer, text="Close", state=DISABLED, command=self.close)
        self.close_button.pack(anchor="e", pady=(12, 0))
        self.window.grab_set()
        self._pump()

    def _pump(self) -> None:
        try:
            self.window.update_idletasks()
        except Exception:
            pass

    def _append(self, text: str) -> None:
        self.log.configure(state=NORMAL)
        self.log.insert(END, text.rstrip() + "\n")
        self.log.configure(state=DISABLED)
        self.log.see(END)
        self._pump()

    def step(self, message: str, detail: str = "") -> None:
        self._step += 1
        self.status.set(message)
        self._append(f"{self._step}. {message}")
        if detail.strip():
            self._append(f"   {detail.strip()}")

    def complete(self, message: str) -> None:
        self._finished = True
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self.status.set(message)
        self._append(f"✓ {message}")
        self.close_button.configure(state=NORMAL)
        try:
            self.window.grab_release()
        except Exception:
            pass
        self._pump()

    def fail(self, message: str) -> None:
        self._finished = True
        self.progress.stop()
        self.status.set("Automatic setup stopped safely")
        self._append(f"STOPPED: {message}")
        self._append("No unverified game file was installed.")
        self.close_button.configure(state=NORMAL)
        try:
            self.window.grab_release()
        except Exception:
            pass
        self._pump()

    def close(self) -> None:
        if self._finished:
            self.window.destroy()


class ManagerApp:
    """Verified package, isolated build, atomic install, recovery, and restore."""

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.release_identity = runtime_identity()
        self.root.title(
            "Star Empire Mod Manager "
            f"{self.release_identity.version} [{self.release_identity.build_id}]")
        self.root.geometry("940x660")
        self.root.minsize(800, 560)
        self.game_path = StringVar()
        self.pack_path = StringVar()
        self.status_text = StringVar(
            value="Choose a game folder; its signed loader is detected automatically.")
        self.summary_text = StringVar(value="No inspection has been run.")
        self.manager_hint = StringVar(
            value="Choose the Star Empire folder, then click once to enable mods.")
        self._package: VerifiedModPackage | None = None
        self._inspection: Inspection | None = None
        self._candidate: BuiltCandidate | None = None
        self._state_path = default_state_path()
        self._transaction = ManagerTransaction(self._state_path)
        self._work_root = self._state_path.parent / "work"
        self._mods = ModManagerController()
        self._settings_path = self._mods.state_root / SETTINGS_FILENAME
        self._selected_game_path = (
            self._mods.state_root / SELECTED_GAME_FILENAME)
        try:
            settings = load_manager_settings(self._settings_path)
        except ManagerSettingsError:
            settings = ManagerSettings()
        self.debug_logging = BooleanVar(value=settings.debug_logging)
        self.dark_mode = BooleanVar(value=settings.dark_mode)
        self.force_load_mod = BooleanVar(value=False)
        self.mod_compatibility_text = StringVar(
            value="Compatibility: select a mod")
        self._saved_debug_logging = settings.debug_logging
        self._saved_dark_mode = settings.dark_mode
        self._update_results = SimpleQueue()
        self._mod_update_results = SimpleQueue()
        self._mod_update_active = False
        self._update_generation = 0
        self._update_check_active = False
        self._mod_registry_stamp: tuple[int, int] | None = None
        self._mod_registry_watch_id: str | None = None
        try:
            remembered_game = load_selected_game(self._selected_game_path)
        except ManagerDataError:
            remembered_game = None
        if remembered_game is None or not remembered_game.is_dir():
            try:
                remembered_state = load_install_state(self._state_path)
            except ManagerDataError:
                remembered_state = None
            if (remembered_state is not None
                    and remembered_state.game_root.is_dir()):
                remembered_game = remembered_state.game_root
        if remembered_game is not None and remembered_game.is_dir():
            self.game_path.set(str(remembered_game))
        self._style = ttk.Style(self.root)
        self._build_ui()
        self._apply_theme(self.dark_mode.get())
        self.root.protocol("WM_DELETE_WINDOW", self._close_manager)
        self.root.bind(
            "<FocusIn>", self._refresh_mods_if_registry_changed, add="+")
        self._schedule_mod_registry_watch()
        self._drop_target = install_windows_file_drop(
            self.root, self._handle_dropped_mod_files)
        if self.game_path.get().strip():
            self._auto_select_local_loader(Path(self.game_path.get()))
            self.refresh()

    @staticmethod
    def _cleanup_candidate(candidate: BuiltCandidate | None) -> str | None:
        if candidate is None:
            return None
        cleanup = getattr(candidate, "cleanup", None)
        if not callable(cleanup):
            return None
        try:
            cleanup()
        except (CandidateBuildError, OSError) as error:
            return str(error)
        return None

    def _apply_theme(self, dark: bool) -> None:
        """Recolour the whole window; ttk styles are shared by every tab."""
        palette = DARK_PALETTE if dark else LIGHT_PALETTE
        style = self._style
        style.theme_use("clam")
        style.configure(
            ".", background=palette["bg"], foreground=palette["fg"],
            fieldbackground=palette["field_bg"])
        for name in (
                "TFrame", "TLabel", "TLabelframe", "TLabelframe.Label",
                "TCheckbutton", "TPanedwindow", "TNotebook"):
            style.configure(name, background=palette["bg"], foreground=palette["fg"])
        style.map(
            "TCheckbutton",
            background=[("active", palette["bg"])],
            foreground=[("active", palette["fg"])])
        style.configure(
            "TButton", background=palette["field_bg"], foreground=palette["fg"])
        style.map(
            "TButton",
            background=[("active", palette["border"]), ("disabled", palette["bg"])])
        style.configure(
            "TEntry", fieldbackground=palette["field_bg"],
            foreground=palette["fg"], insertcolor=palette["fg"])
        style.configure(
            "TNotebook.Tab", background=palette["field_bg"],
            foreground=palette["fg"])
        style.map(
            "TNotebook.Tab",
            background=[("selected", palette["bg"])],
            foreground=[("selected", palette["fg"])])
        style.configure(
            "Treeview", background=palette["field_bg"],
            fieldbackground=palette["field_bg"], foreground=palette["fg"])
        style.map(
            "Treeview",
            background=[("selected", palette["select_bg"])],
            foreground=[("selected", palette["select_fg"])])
        style.configure(
            "Treeview.Heading", background=palette["border"],
            foreground=palette["fg"])
        style.configure(
            "TScrollbar", background=palette["field_bg"],
            troughcolor=palette["trough"])
        style.configure(
            "TProgressbar", background=palette["select_bg"],
            troughcolor=palette["trough"])
        style.configure("TSeparator", background=palette["border"])
        self.root.configure(background=palette["bg"])
        for widget in (
                getattr(self, "log_text", None),
                getattr(self, "mod_readme_text", None)):
            if widget is not None:
                _theme_text_widget(widget, palette)

    def _toggle_dark_mode(self) -> None:
        self._apply_theme(bool(self.dark_mode.get()))
        self._save_manager_settings()

    def _launch_game(self) -> None:
        selected = self.game_path.get().strip()
        if not selected:
            messagebox.showerror(
                "Cannot launch Star Empire", "Choose a game folder first.")
            return
        root = Path(selected).expanduser().resolve()
        launcher = root / LAUNCHER_EXE
        if not launcher.is_file():
            messagebox.showerror(
                "Cannot launch Star Empire",
                f"{LAUNCHER_EXE} was not found in {root}.")
            return
        processes = running_game_processes()
        if processes.verified and processes.names and not messagebox.askyesno(
                "Star Empire may already be running",
                "Star Empire or its launcher appears to already be running. "
                "Launch it again anyway?"):
            return
        try:
            subprocess.Popen([str(launcher)], cwd=str(root))
        except OSError as error:
            messagebox.showerror("Cannot launch Star Empire", str(error))
            return
        self.status_text.set("STAR EMPIRE LAUNCHED")
        self.summary_text.set(f"Started {LAUNCHER_EXE} from {root}.")

    def _close_manager(self) -> None:
        if self._mod_registry_watch_id is not None:
            try:
                self.root.after_cancel(self._mod_registry_watch_id)
            except TclError:
                pass
            self._mod_registry_watch_id = None
        self._cleanup_candidate(self._candidate)
        self._candidate = None
        self.root.destroy()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text="STAR EMPIRE MOD MANAGER",
                  font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(f"Version {self.release_identity.version}  •  "
                  f"Build {self.release_identity.build_id}"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Mods • automatic game compatibility • verified backups • recoverable changes",
        ).pack(anchor="w", pady=(2, 10))

        game_bar = ttk.Frame(outer)
        game_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(game_bar, text="Game:", width=7).pack(side=LEFT)
        ttk.Entry(
            game_bar, textvariable=self.game_path, state="readonly"
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            game_bar, text="Change…", command=self._choose_game
        ).pack(side=RIGHT, padx=(8, 0))
        self.launch_button = ttk.Button(
            game_bar, text="Launch Star Empire", command=self._launch_game)
        self.launch_button.pack(side=RIGHT, padx=(8, 0))

        status_bar = ttk.Frame(outer, padding=(0, 4))
        status_bar.pack(fill="x", pady=(0, 8))
        self.manager_switch = Button(
            status_bar,
            text="SELECT STAR EMPIRE",
            command=self._toggle_manager_enabled,
            font=("Segoe UI", 13, "bold"),
            foreground="white",
            background="#555b63",
            activeforeground="white",
            activebackground="#666d76",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            padx=18,
            pady=13,
        )
        self.manager_switch.pack(fill="x")
        ttk.Label(
            status_bar, textvariable=self.manager_hint, wraplength=820
        ).pack(anchor="w", pady=(6, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=BOTH, expand=True)
        self.notebook = notebook
        dashboard = ttk.Frame(notebook, padding=14)
        mods = ttk.Frame(notebook, padding=14)
        logs = ttk.Frame(notebook, padding=14)
        settings = ttk.Frame(notebook, padding=14)
        notebook.add(mods, text="Mods")
        notebook.add(logs, text="Logs")
        self.logs_tab = logs
        notebook.add(settings, text="Settings")

        status = ttk.LabelFrame(
            dashboard, text="Compatibility details", padding=12)
        status.pack(fill="x", pady=16)
        ttk.Label(status, textvariable=self.status_text,
                  font=("Segoe UI", 11, "bold"), wraplength=720).pack(anchor="w")
        ttk.Label(status, textvariable=self.summary_text,
                  wraplength=720).pack(anchor="w", pady=(8, 0))

        actions = ttk.Frame(dashboard)
        actions.pack(fill="x")
        self.build_button = ttk.Button(
            actions, text="Prepare Game", command=self._build_candidate)
        self.install_button = ttk.Button(
            actions, text="Apply Compatibility", command=self._install)
        self.update_button = ttk.Button(
            actions, text="Update Compatibility", command=self._update)
        self.restore_button = ttk.Button(
            actions, text="Restore Vanilla", command=self._restore)
        self.repair_button = ttk.Button(
            actions, text="Repair Interrupted Operation", command=self._repair)
        self.verify_button = ttk.Button(actions, text="Verify", command=self.refresh)
        self.update_check_button = ttk.Button(
            actions, text="Check for Updates", command=self._check_updates)
        for button in (self.build_button, self.install_button, self.update_button,
                       self.restore_button, self.repair_button, self.verify_button,
                       self.update_check_button):
            button.pack(side=LEFT, padx=(0, 8), pady=(0, 6))
        ttk.Button(actions, text="Diagnostics", command=self._diagnostics).pack(
            side=RIGHT)
        self._set_actions()

        ttk.Separator(dashboard).pack(fill="x", pady=16)
        ttk.Label(dashboard, text=(
            "Safety rules: verified compatibility • exact match or isolated compatibility test • "
            "game closed • "
            "permanent vanilla backup • isolated candidate audit • atomic replacement • "
            "hash-classified crash recovery"), wraplength=740).pack(anchor="w")

        mods.columnconfigure(0, weight=1)
        mods.rowconfigure(3, weight=1)
        ttk.Label(
            mods, text="MODS", font=("Segoe UI", 12, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            mods,
            text=("New mods are installed disabled. Enable a mod to queue it "
                  "for the next Star Empire launch; mod support is prepared "
                  "automatically when needed."),
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))
        ttk.Label(
            mods,
            text="DROP .SEMOD FILES ANYWHERE IN THIS WINDOW",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=2, column=0, sticky="w", pady=(0, 8))
        mod_browser = ttk.Panedwindow(mods, orient="horizontal")
        mod_browser.grid(row=3, column=0, sticky="nsew")
        mod_list = ttk.Frame(mod_browser)
        mod_list.columnconfigure(0, weight=1)
        mod_list.rowconfigure(0, weight=1)
        mod_details = ttk.LabelFrame(
            mod_browser, text="MOD INFORMATION", padding=10, width=330)
        mod_browser.add(mod_list, weight=3)
        mod_browser.add(mod_details, weight=2)

        mod_scroll = ttk.Scrollbar(mod_list, orient="vertical")
        self.mods_tree = ttk.Treeview(
            mod_list, columns=("state", "name", "version", "game", "status"),
            show="headings", height=6, selectmode="browse")
        for column, label, width in (
                ("state", "Next launch", 90), ("name", "Mod", 190),
                ("version", "Mod Ver", 75), ("game", "Game", 85),
                ("status", "Status", 100)):
            self.mods_tree.heading(column, text=label)
            self.mods_tree.column(column, width=width, anchor="w")
        mod_scroll.configure(command=self.mods_tree.yview)
        self.mods_tree.configure(yscrollcommand=mod_scroll.set)
        self.mods_tree.grid(row=0, column=0, sticky="nsew")
        mod_scroll.grid(row=0, column=1, sticky="ns")
        self.mods_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._set_mod_actions())
        self.mods_tree.bind("<Button-1>", self._toggle_mod_from_click)
        self.mods_tree.bind("<space>", self._toggle_mod_from_keyboard)

        self.mod_info_title = StringVar(value="Select a mod")
        ttk.Label(
            mod_details, textvariable=self.mod_info_title,
            font=("Segoe UI", 10, "bold"), wraplength=300,
        ).pack(fill="x", anchor="w")
        ttk.Label(
            mod_details, textvariable=self.mod_compatibility_text,
            wraplength=300,
        ).pack(fill="x", anchor="w", pady=(6, 2))
        self.mod_force_check = ttk.Checkbutton(
            mod_details,
            text="Force load anyway",
            variable=self.force_load_mod,
            command=self._set_selected_mod_force_load,
        )
        self.mod_force_check.pack(fill="x", anchor="w", pady=(2, 4))
        self.mod_preview = ttk.Label(
            mod_details, text="No preview image", anchor="center")
        self.mod_preview.pack(fill="x", pady=(8, 8))
        self._mod_preview_image = None
        readme_frame = ttk.Frame(mod_details)
        readme_frame.pack(fill=BOTH, expand=True)
        readme_scroll = ttk.Scrollbar(readme_frame, orient="vertical")
        self.mod_readme_text = Text(
            readme_frame, wrap="word", state=DISABLED, undo=False,
            font=("Segoe UI", 9), padx=8, pady=8,
            yscrollcommand=readme_scroll.set)
        readme_scroll.configure(command=self.mod_readme_text.yview)
        readme_scroll.pack(side=RIGHT, fill="y")
        self.mod_readme_text.pack(side=LEFT, fill=BOTH, expand=True)
        mod_actions = ttk.Frame(mods)
        mod_actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(
            mod_actions, text="Install Mod…",
            command=self._install_external_mod).pack(side=LEFT, padx=(0, 8))
        self.mod_toggle_button = ttk.Button(
            mod_actions, text="Enable / Disable",
            command=self._toggle_external_mod)
        self.mod_toggle_button.pack(side=LEFT, padx=(0, 8))
        self.mod_remove_button = ttk.Button(
            mod_actions, text="Uninstall", command=self._uninstall_external_mod)
        self.mod_remove_button.pack(side=LEFT, padx=(0, 8))
        self.mod_update_button = ttk.Button(
            mod_actions, text="Check Selected for Update",
            command=self._check_selected_mod_update)
        self.mod_update_button.pack(side=LEFT, padx=(0, 8))
        ttk.Button(
            mod_actions, text="Refresh", command=self._refresh_mods
        ).pack(side=RIGHT)

        ttk.Label(
            logs, text="DIAGNOSTICS AND LOGS",
            font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(
            logs, wraplength=820,
            text=("Loader and per-mod errors are isolated and written through "
                  "named loggers. Export a privacy-conscious ZIP containing "
                  "versions, hashes, mod state, and bounded log tails."),
        ).pack(anchor="w", pady=(8, 12))
        log_actions = ttk.Frame(logs)
        log_actions.pack(fill="x")
        ttk.Button(
            log_actions, text="Refresh Logs",
            command=self._diagnostics).pack(side=LEFT, padx=(0, 8))
        ttk.Button(
            log_actions, text="Export Diagnostics ZIP…",
            command=self._export_diagnostics).pack(side=LEFT)
        log_view = ttk.Frame(logs)
        log_view.pack(fill=BOTH, expand=True, pady=(10, 0))
        log_scroll_y = ttk.Scrollbar(log_view, orient="vertical")
        log_scroll_x = ttk.Scrollbar(log_view, orient="horizontal")
        self.log_text = Text(
            log_view, wrap="none", state=DISABLED, undo=False,
            font=("Consolas", 9), padx=8, pady=8,
            yscrollcommand=log_scroll_y.set,
            xscrollcommand=log_scroll_x.set,
        )
        log_scroll_y.configure(command=self.log_text.yview)
        log_scroll_x.configure(command=self.log_text.xview)
        log_scroll_y.pack(side=RIGHT, fill="y")
        log_scroll_x.pack(side="bottom", fill="x")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)

        ttk.Label(
            settings, text="MANAGER SETTINGS",
            font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(
            settings, wraplength=820,
            text=("External .semod files do not require signing keys. Packages "
                  "are still checked for safe paths, exact file inventory, "
                  "blocked executable content, and SHA-256 integrity. "
                  "Game compatibility updates are handled automatically by "
                  "the Manager."),
        ).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(
            settings, text="Enable verbose mod debug logging",
            variable=self.debug_logging,
            command=self._save_manager_settings,
        ).pack(anchor="w", pady=(14, 2))
        ttk.Label(
            settings,
            text=("Verbose logging takes effect the next time Star Empire "
                  "starts and may create larger rotating log files."),
        ).pack(anchor="w")
        ttk.Checkbutton(
            settings, text="Dark mode",
            variable=self.dark_mode,
            command=self._toggle_dark_mode,
        ).pack(anchor="w", pady=(14, 2))
        self._refresh_mods()
        self._diagnostics(select_tab=False)

    @staticmethod
    def _path_row(parent: ttk.Frame, label: str, variable: StringVar, command,
                  button_text: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=24).pack(side=LEFT)
        ttk.Entry(row, textvariable=variable, state="readonly").pack(
            side=LEFT, fill="x", expand=True)
        ttk.Button(row, text=button_text, command=command).pack(
            side=RIGHT, padx=(8, 0))

    def _selected_mod_id(self) -> str | None:
        selected = tuple(self.mods_tree.selection())
        return selected[0] if len(selected) == 1 else None

    def _set_mod_actions(self) -> None:
        selected = self._selected_mod_id()
        state = NORMAL if selected is not None else DISABLED
        self.mod_toggle_button.configure(state=state)
        self.mod_remove_button.configure(state=state)
        update_state = DISABLED
        installed = None
        compatibility = None
        if selected is not None:
            try:
                installed = self._mods.installed(selected)
                enabled = installed.enabled
                game_version, client_sha256 = self._mod_game_identity()
                compatibility = mod_compatibility_view(
                    installed.manifest.compatibility, installed.force_load,
                    game_version, client_sha256)
                if installed.manifest.update is not None:
                    update_state = NORMAL
            except (ModRegistryError, OSError):
                enabled = False
            self.mod_toggle_button.configure(
                text=("Disable next launch" if enabled
                      else "Enable next launch"))
        if self._mod_update_active:
            update_state = DISABLED
        self.mod_update_button.configure(state=update_state)
        if installed is None:
            self.force_load_mod.set(False)
            self.mod_compatibility_text.set("Compatibility: select a mod")
            self.mod_force_check.configure(state=DISABLED)
        else:
            self.force_load_mod.set(installed.force_load)
            self.mod_compatibility_text.set(compatibility.detail)
            force_state = (
                NORMAL if compatibility.compatible is False
                or installed.force_load else DISABLED)
            self.mod_force_check.configure(state=force_state)
        self._show_mod_information(installed)

    def _mod_game_identity(self) -> tuple[str, str]:
        selected = self.game_path.get().strip()
        if not selected:
            return "", ""
        try:
            root = Path(selected).expanduser().resolve()
            inspection = self._inspection
            if inspection is not None and inspection.game_root == root:
                return inspection.version or "", inspection.current_sha256 or ""
            version = read_game_version(root / "version.txt") or ""
            client = root / CLIENT_EXE
            digest = sha256_file(client) if client.is_file() else ""
            return version, digest
        except (ManagerDataError, OSError, RuntimeError, ValueError):
            return "", ""

    def _set_selected_mod_force_load(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id is None:
            self.force_load_mod.set(False)
            return
        requested = bool(self.force_load_mod.get())
        if requested and not messagebox.askyesno(
                "Force load incompatible mod",
                "Force loading bypasses only the mod's declared game-version "
                "check. The mod may crash Star Empire or behave incorrectly.\n\n"
                "Force load this mod anyway?"):
            self.force_load_mod.set(False)
            return
        try:
            result = self._mods.set_force_load(mod_id, requested)
        except (ModServiceError, ModRegistryError, OSError) as error:
            messagebox.showerror("Force-load change blocked", str(error))
        else:
            self.status_text.set(
                f"{result.mod_id} force load is "
                f"{'enabled' if requested else 'disabled'}")
            self.summary_text.set(
                "This setting will be used the next time Star Empire starts.")
        self._refresh_mods()

    def _show_mod_information(self, installed: InstalledMod | None) -> None:
        if installed is None:
            title = "Select a mod"
            readme = "Select an installed mod to view its README."
            preview_png = None
        else:
            details = load_mod_display_details(installed)
            title = details.title
            readme = details.readme
            preview_png = details.preview_png
        self.mod_info_title.set(title)
        self.mod_readme_text.configure(state=NORMAL)
        self.mod_readme_text.delete("1.0", END)
        self.mod_readme_text.insert("1.0", readme)
        self.mod_readme_text.configure(state=DISABLED)

        self._mod_preview_image = None
        if preview_png is None:
            self.mod_preview.configure(image="", text="No preview image")
            return
        try:
            image = PhotoImage(data=base64.b64encode(preview_png).decode("ascii"))
            scale = max(
                1,
                (image.width() + MOD_PREVIEW_MAX_WIDTH - 1)
                // MOD_PREVIEW_MAX_WIDTH,
                (image.height() + MOD_PREVIEW_MAX_HEIGHT - 1)
                // MOD_PREVIEW_MAX_HEIGHT,
            )
            if scale > 1:
                image = image.subsample(scale, scale)
            self._mod_preview_image = image
            self.mod_preview.configure(image=image, text="")
        except (TclError, ValueError):
            self.mod_preview.configure(
                image="", text="Preview image could not be displayed")

    def _refresh_mods(self) -> None:
        if not hasattr(self, "mods_tree"):
            return
        try:
            observed_stamp = self._mod_registry_file_stamp()
        except OSError:
            observed_stamp = self._mod_registry_stamp
        current = self._selected_mod_id()
        for item in self.mods_tree.get_children():
            self.mods_tree.delete(item)
        try:
            summaries = self._mods.summaries()
        except (ModRegistryError, OSError) as error:
            messagebox.showerror("Mod registry error", str(error))
            summaries = ()
        game_version, client_sha256 = self._mod_game_identity()
        for item in summaries:
            compatibility = mod_compatibility_view(
                item.compatibility, item.force_load,
                game_version, client_sha256)
            self.mods_tree.insert(
                "", END, iid=item.mod_id,
                values=("LOAD" if item.enabled else "DISABLED",
                        item.name, item.version, compatibility.supported,
                        compatibility.status))
        if current is not None and self.mods_tree.exists(current):
            self.mods_tree.selection_set(current)
        self._mod_registry_stamp = observed_stamp
        self._set_mod_actions()

    def _mod_registry_file_stamp(self) -> tuple[int, int] | None:
        registry_path = self._mods.state_root / "mods.json"
        try:
            stat = registry_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _refresh_mods_if_registry_changed(self, _event=None) -> None:
        try:
            current_stamp = self._mod_registry_file_stamp()
        except OSError:
            return
        if current_stamp != self._mod_registry_stamp:
            self._refresh_mods()

    def _schedule_mod_registry_watch(self) -> None:
        try:
            self._mod_registry_watch_id = self.root.after(
                MOD_REGISTRY_POLL_MS, self._poll_mod_registry)
        except TclError:
            self._mod_registry_watch_id = None

    def _poll_mod_registry(self) -> None:
        self._mod_registry_watch_id = None
        self._refresh_mods_if_registry_changed()
        self._schedule_mod_registry_watch()

    def _toggle_mod_from_click(self, event) -> str | None:
        if self.mods_tree.identify_column(event.x) != "#1":
            return None
        mod_id = self.mods_tree.identify_row(event.y)
        if not mod_id:
            return None
        self.mods_tree.selection_set(mod_id)
        self._toggle_external_mod()
        return "break"

    def _toggle_mod_from_keyboard(self, _event=None) -> str:
        self._toggle_external_mod()
        return "break"

    def _prepare_game_for_mod_install(self) -> bool:
        if not self.game_path.get().strip():
            self._choose_game()
            if not self.game_path.get().strip():
                return False
        if self._package is None:
            self._auto_select_local_loader(Path(self.game_path.get()))
        self.refresh()
        inspection = self._inspection
        if inspection is None:
            return False
        if not self._recover_interrupted_automatically():
            return False
        inspection = self._inspection
        if inspection is None:
            return False
        if (inspection.status is InstallStatus.INSTALLED_HEALTHY
                and not inspection.can_update):
            return True
        compatibility_test = compatibility_test_available(
            inspection, self._package)
        if not (inspection.can_install or inspection.can_update
                or compatibility_test):
            messagebox.showerror(
                "Game setup blocked", inspection.message)
            return False
        dark_mode = getattr(self, "dark_mode", None)
        progress = SetupProgressDialog(
            self.root,
            DARK_PALETTE if dark_mode is not None and dark_mode.get()
            else LIGHT_PALETTE)
        progress.step(
            "Checking the selected Star Empire build",
            f"Detected game version {inspection.version or 'unknown'}.")
        progress.step(
            ("Comparing this update with the previous working loader"
             if compatibility_test else
             "Verifying internal compatibility support"),
            ("Every required loader hook must still exist exactly once."
             if compatibility_test else
             "Package integrity and the supported game build are being checked."))
        baseline = (inspection.state.original_client
                    if inspection.can_update and inspection.state is not None
                    else None)
        candidate = None
        try:
            candidate = build_candidate(
                self._package, inspection.game_root, self._work_root,
                baseline_client=baseline,
                compatibility_test=compatibility_test)
            progress.step(
                "The isolated loader build passed",
                "The rebuilt client compiled and its archive checks succeeded.")
            progress.step(
                "Backing up vanilla and enabling mod support",
                "The current vanilla Client.exe is preserved before the verified replacement.")
            self._transaction.install_or_update(inspection, candidate)
        except (CandidateBuildError, PackageError, TransactionError,
                ManagerDataError, OSError, ValueError) as error:
            self._cleanup_candidate(candidate)
            progress.fail(str(error))
            messagebox.showerror("Automatic game setup blocked", str(error))
            return False
        cleanup_error = self._cleanup_candidate(candidate)
        self._candidate = None
        progress.step(
            "Removing temporary build files",
            ("Temporary work folder removed."
             if cleanup_error is None else
             f"Temporary cleanup needs attention: {cleanup_error}"))
        progress.step(
            "Verifying the installed loader and mod list",
            "Installed mods and their ON/OFF choices are retained.")
        self.refresh()
        succeeded = bool(
            self._inspection is not None
            and self._inspection.status is InstallStatus.INSTALLED_HEALTHY)
        if succeeded:
            progress.complete(
                "Mod support is enabled and ready for Star Empire.")
        else:
            progress.fail(
                "The final verification did not report a healthy installation.")
        return succeeded

    def _recover_interrupted_automatically(self) -> bool:
        """Finish a hash-classified interrupted swap without exposing tools."""
        if not self._transaction.has_interrupted_operation():
            return True
        try:
            self._transaction.recover()
        except (TransactionError, ManagerDataError, OSError) as error:
            messagebox.showerror(
                "Automatic recovery could not continue",
                f"No game file was guessed or overwritten. {error}\n\n"
                "Open Logs for diagnostic details.")
            self.refresh()
            return False
        self._candidate = None
        self.refresh()
        return not self._transaction.has_interrupted_operation()

    def _toggle_manager_enabled(self) -> None:
        """One-click global switch between verified modded and vanilla clients."""
        if not self.game_path.get().strip():
            self._choose_game()
            if not self.game_path.get().strip():
                return
        self.refresh()
        if not self._recover_interrupted_automatically():
            return
        inspection = self._inspection
        if inspection is None:
            return
        if inspection.status is InstallStatus.GAME_RUNNING:
            messagebox.showerror(
                "Close Star Empire",
                "Close Star Empire and its launcher, then click the switch again.")
            return
        if inspection.status is InstallStatus.INSTALLED_HEALTHY:
            try:
                self._transaction.restore(inspection)
            except (TransactionError, ManagerDataError, OSError) as error:
                messagebox.showerror("Could not disable the Mod Manager", str(error))
            else:
                self._candidate = None
            self.refresh()
            return
        self._prepare_game_for_mod_install()
        self.refresh()

    def _install_external_mod(self) -> None:
        selected = filedialog.askopenfilename(
            title="Install a Star Empire mod",
            filetypes=(("Star Empire Mod", "*.semod"),))
        if not selected:
            return
        self._install_external_mod_paths((Path(selected),))

    def _handle_dropped_mod_files(self, paths) -> None:
        """Route shell drops through the normal verified mod installer."""
        selected = unique_semod_paths(paths)
        if not selected:
            messagebox.showerror(
                "No Star Empire mod found",
                "Drop one or more files ending in .semod.")
            return
        self._install_external_mod_paths(selected)

    def _install_external_mod_paths(self, paths) -> None:
        """Verify and store a complete batch disabled without changing the game."""
        selected = unique_semod_paths(paths)
        if not selected:
            return
        try:
            for selected_path in selected:
                verify_semod_package(selected_path)
        except (SemodPackageError, ManagerDataError, OSError) as error:
            messagebox.showerror("Mod installation blocked", str(error))
            return
        installed = []
        try:
            for selected_path in selected:
                installed.append(self._mods.install(
                    selected_path, enable=False))
        except (SemodPackageError, ModServiceError, ModRegistryError,
                OSError) as error:
            messagebox.showerror("Mod installation blocked", str(error))
            self._refresh_mods()
            return
        self.status_text.set(
            (f"{installed[0].mod_id} {installed[0].version} installed disabled"
             if len(installed) == 1 else
             f"{len(installed)} mods installed disabled"))
        self.summary_text.set(
            "Select a mod and click Enable next launch when it is ready to use.")
        self._refresh_mods()

    def _toggle_external_mod(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id is None:
            return
        try:
            installed = self._mods.installed(mod_id)
        except (ModRegistryError, OSError) as error:
            messagebox.showerror("Mod state change blocked", str(error))
            self._refresh_mods()
            return
        enabling = not installed.enabled
        if enabling and not self._prepare_game_for_mod_install():
            self._refresh_mods()
            return
        try:
            result = self._mods.set_enabled(mod_id, enabling)
        except (ModServiceError, ModRegistryError, OSError) as error:
            messagebox.showerror("Mod state change blocked", str(error))
        else:
            self.status_text.set(
                (f"{result.mod_id} will load on the next Star Empire launch"
                 if enabling else
                 f"{result.mod_id} is disabled for the next launch"))
            self.summary_text.set(
                "The running game is unchanged; this choice applies at startup.")
        self._refresh_mods()

    def _uninstall_external_mod(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id is None or not messagebox.askyesno(
                "Uninstall mod",
                f"Remove {mod_id}? Its files will be moved to recoverable storage."):
            return
        try:
            result = self._mods.uninstall(mod_id)
        except (ModServiceError, ModRegistryError, OSError) as error:
            messagebox.showerror("Mod removal blocked", str(error))
        else:
            if result.removed_path is None:
                messagebox.showwarning(
                    "Legacy mod disabled",
                    f"{mod_id} was removed from the loader and cannot run.\n\n"
                    "Windows would not allow the old elevated installation "
                    "folder to be moved. Its files remain inert and can be "
                    "cleaned later with administrator permission. Restart the game.")
            else:
                messagebox.showinfo(
                    "Mod removed", f"{mod_id} was removed. Restart the game.")
        self._refresh_mods()

    def _check_selected_mod_update(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id is None or self._mod_update_active:
            return
        self._mod_update_active = True
        self._set_mod_actions()

        def worker() -> None:
            try:
                result = self._mods.check_update(mod_id)
                error = None
            except Exception as failure:
                result = None
                error = failure
            self._mod_update_results.put((mod_id, result, error))

        Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_selected_mod_update)

    def _poll_selected_mod_update(self) -> None:
        try:
            mod_id, update, error = self._mod_update_results.get_nowait()
        except Empty:
            self.root.after(100, self._poll_selected_mod_update)
            return
        self._mod_update_active = False
        self._set_mod_actions()
        if error is not None:
            messagebox.showerror("Mod update check failed", str(error))
            return
        if update is None:
            messagebox.showinfo("No update", f"{mod_id} is up to date.")
            return
        if not messagebox.askyesno(
                "Install mod update",
                f"Install {update.package.manifest.name} "
                f"{update.package.manifest.version} from "
                f"{update.repository}?\n\nStar Empire must be closed."):
            return
        try:
            result = self._mods.apply_update(update)
        except (ModUpdateError, SemodPackageError, ModServiceError,
                ModRegistryError, OSError) as failure:
            messagebox.showerror("Mod update blocked", str(failure))
        else:
            messagebox.showinfo(
                "Mod updated",
                f"{result.mod_id} {result.version} is ready. Restart "
                "Star Empire to load it.")
        self._refresh_mods()

    def _choose_game(self) -> None:
        selected = filedialog.askdirectory(
            title="Select the Star Empire game folder")
        if selected:
            self._cancel_pending_update_check()
            self.game_path.set(selected)
            try:
                save_selected_game(self._selected_game_path, Path(selected))
            except ManagerDataError as error:
                messagebox.showerror(
                    "Game folder preference not saved", str(error))
            self._package = None
            self._candidate = None
            self.pack_path.set("")
            local_loader = self._auto_select_local_loader(Path(selected))
            self.refresh()
            if not local_loader:
                self._check_updates(automatic=True)

    def _auto_select_local_loader(self, game_root: Path) -> bool:
        """Select signed compatibility support without exposing its package."""
        try:
            keys = load_trusted_keys()
            if not keys:
                return False
            package = None
            internal_root = manager_internal_loader_root()
            if internal_root.is_dir():
                package = discover_local_loader(
                    game_root, self._state_path, internal_root, keys,
                    allow_compatibility_fallback=True,
                    strict_internal_inventory=True)
            if package is None:
                package = discover_local_loader(
                    game_root, self._state_path, manager_bundle_root(), keys)
        except (FeedError, PackageError, ManagerDataError, OSError) as error:
            messagebox.showerror("Automatic compatibility check failed", str(error))
            return False
        if package is None:
            return False
        self._package = package
        self._candidate = None
        self.pack_path.set(str(package.path))
        return True

    def _cancel_pending_update_check(self) -> None:
        """Make any already-running automatic result stale and harmless."""
        self._update_generation += 1
        self._update_check_active = False
        button = getattr(self, "update_check_button", None)
        if button is not None:
            button.configure(state=NORMAL)

    def _check_updates(self, automatic: bool = False) -> None:
        if self._update_check_active:
            return
        selected_game = self.game_path.get().strip()
        if not selected_game:
            if not automatic:
                messagebox.showerror(
                    "Update check blocked", "Choose a game folder first.")
            return
        try:
            trust = load_trust_configuration()
            if not trust.keys:
                raise FeedError(
                    "Internal compatibility verification data is unavailable")
            if not trust.update_feed_url:
                raise FeedError(
                    "The automatic compatibility update source is unavailable")
        except (FeedError, ManagerDataError, OSError) as error:
            if not automatic:
                messagebox.showerror("Update check failed", str(error))
            return
        self._update_generation += 1
        generation = self._update_generation
        self._update_check_active = True
        self.update_check_button.configure(state=DISABLED)
        self.status_text.set("CHECKING COMPATIBILITY UPDATES")
        self.summary_text.set(
            "Matching the selected game by exact version and official Client hash…")

        def worker() -> None:
            try:
                result = acquire_signed_update(
                    Path(selected_game), self._state_path,
                    trust.update_feed_url, trust.keys,
                    self._state_path.parent / "downloads")
            except (FeedError, PackageError, ManagerDataError, OSError) as error:
                self._update_results.put(
                    (generation, automatic, selected_game, None, error))
            else:
                self._update_results.put(
                    (generation, automatic, selected_game, result, None))

        Thread(target=worker, name="StarEmpireLoaderUpdateCheck", daemon=True).start()
        self.root.after(100, self._poll_update_result)

    def _poll_update_result(self) -> None:
        try:
            generation, automatic, selected_game, result, error = (
                self._update_results.get_nowait())
        except Empty:
            if self._update_check_active:
                self.root.after(100, self._poll_update_result)
            return
        if generation != self._update_generation:
            if self._update_check_active:
                self.root.after(100, self._poll_update_result)
            return
        self._update_check_active = False
        self.update_check_button.configure(state=NORMAL)
        try:
            same_game = (Path(self.game_path.get()).resolve()
                         == Path(selected_game).resolve())
        except OSError:
            same_game = False
        if not same_game:
            return
        if error is not None:
            if automatic:
                self.status_text.set("AUTOMATIC UPDATE CHECK FAILED")
                self.summary_text.set(str(error))
            else:
                messagebox.showerror("Update check failed", str(error))
                self.refresh()
            return
        if result.package is None:
            self._package = None
            self._candidate = None
            self._inspection = None
            self.pack_path.set("")
            self.status_text.set("GAME UPDATE DETECTED — PACKAGE PENDING")
            self.summary_text.set(
                f"No reviewed compatibility package is published yet for Star Empire "
                f"{result.identity.game_version} / "
                f"{result.identity.official_client_sha256[:12]}. "
                "Vanilla files were not changed.")
            self._set_actions()
            return
        self._package = result.package
        self._candidate = None
        self.pack_path.set(str(result.package.path))
        self.refresh()
        if not automatic:
            messagebox.showinfo(
                "Signed update ready",
                f"Verified loader {result.package.compatibility.mod_version} for "
                f"Star Empire {result.package.compatibility.game_version}.")

    def refresh(self) -> None:
        if not self.game_path.get().strip():
            self.status_text.set("Choose a game folder to begin.")
            self.summary_text.set("No game path selected.")
            self._inspection = None
            self._set_actions()
            self._refresh_mods()
            return
        pack = self._package.compatibility if self._package is not None else None
        self._inspection = inspect_installation(
            Path(self.game_path.get()), pack, self._state_path,
            running_game_processes())
        compatibility_test = compatibility_test_available(
            self._inspection, self._package)
        self.status_text.set(
            "COMPATIBILITY CONFLICT" if compatibility_test
            else self._inspection.status.value.replace("_", " ").upper())
        self.summary_text.set(self._inspection.message)
        if compatibility_test:
            self.summary_text.set(
                self._inspection.message
                + " You may run an isolated compatibility test; it will stop "
                  "before installation if the reviewed UI hooks cannot be "
                  "located and compiled safely.")
        self._set_actions()
        self._refresh_mods()

    def _set_actions(self) -> None:
        compatibility_test = compatibility_test_available(
            self._inspection, self._package)
        availability = action_availability(
            self._inspection, self._candidate,
            self._transaction.has_interrupted_operation(),
            compatibility_test)
        if getattr(self, "build_button", None) is not None:
            self.build_button.configure(
                text=("Try Compatibility Test…"
                      if compatibility_test else "Build Candidate"))
        for button, enabled in (
                (getattr(self, "build_button", None), availability.build),
                (getattr(self, "install_button", None), availability.install),
                (getattr(self, "update_button", None), availability.update),
                (getattr(self, "restore_button", None), availability.restore),
                (getattr(self, "repair_button", None), availability.repair)):
            if button is not None:
                button.configure(state=NORMAL if enabled else DISABLED)
        self._update_manager_switch()

    def _update_manager_switch(self) -> None:
        """Render only the simple global state; keep technical state internal."""
        button = getattr(self, "manager_switch", None)
        hint = getattr(self, "manager_hint", None)
        if button is None or hint is None:
            return
        inspection = self._inspection
        if inspection is None:
            text = "SELECT STAR EMPIRE"
            colour = "#555b63"
            active = "#666d76"
            help_text = "Choose the Star Empire folder, then click once to enable mods."
        elif inspection.status is InstallStatus.INSTALLED_HEALTHY:
            text = "MOD SUPPORT ENABLED"
            colour = "#198754"
            active = "#157347"
            help_text = (
                "Enabled mods will load the next time Star Empire starts. "
                "Click once to restore vanilla mode; individual choices are kept.")
        elif inspection.status is InstallStatus.GAME_RUNNING:
            text = "CLOSE STAR EMPIRE TO MAKE CHANGES"
            colour = "#9a6700"
            active = "#805500"
            help_text = "The game and launcher must be closed before files are swapped."
        elif self._transaction.has_interrupted_operation():
            text = "CLICK TO FINISH AUTOMATIC RECOVERY"
            colour = "#9a6700"
            active = "#805500"
            help_text = "A previous file swap was interrupted; recovery uses verified hashes."
        else:
            text = "MOD SUPPORT DISABLED"
            colour = "#b4232f"
            active = "#921d27"
            help_text = (
                "Vanilla mode is active. Enabling a mod prepares support "
                "automatically, or click here to enable support without a mod.")
        button.configure(
            text=text,
            background=colour,
            activebackground=active,
            foreground="white",
            activeforeground="white",
            state=NORMAL,
        )
        hint.set(help_text)

    def _build_candidate(self) -> None:
        if self._inspection is None or self._package is None:
            return
        if not selected_game_matches_inspection(
                self.game_path.get(), self._inspection):
            self._candidate = None
            messagebox.showerror(
                "Verification required",
                "The selected game folder no longer matches the verified "
                "inspection. Choose the folder again and run Verify.")
            self._set_actions()
            return
        compatibility_test = compatibility_test_available(
            self._inspection, self._package)
        if compatibility_test and not messagebox.askyesno(
                "Compatibility conflict",
                "This game build is not an exact signed match for the selected "
                "package. The Manager can try to locate the four reviewed UI "
                "hooks, compile them, and repack a candidate entirely outside "
                "the game folder.\n\n"
                "This does not prove that the unrecognized build is official. "
                "The build will stop if its loose and frozen Client sources "
                "disagree, known UI Mod markers are present, or any hook is "
                "missing or ambiguous. Continue with the compatibility test?"):
            return
        baseline = (self._inspection.state.original_client
                    if self._inspection.can_update
                    and self._inspection.state is not None else None)
        self.status_text.set(
            "RUNNING COMPATIBILITY TEST" if compatibility_test
            else "BUILDING VERIFIED CANDIDATE")
        self.summary_text.set(
            "Working in the Manager data folder. The game installation is not being modified.")
        self.root.update_idletasks()
        try:
            self._candidate = build_candidate(
                self._package, self._inspection.game_root, self._work_root,
                baseline_client=baseline,
                compatibility_test=compatibility_test)
        except (CandidateBuildError, PackageError, OSError, ValueError) as error:
            self._candidate = None
            messagebox.showerror("Candidate build blocked", str(error))
        else:
            messagebox.showinfo(
                "Candidate ready",
                ("The compatibility-test candidate compiled and its isolated "
                 "build audit verifies. Install is now available, with one "
                 "final compatibility warning."
                 if compatibility_test else
                 "The isolated candidate and build audit both verify. "
                 "Install or Update is now available."))
        self.refresh()

    def _install(self) -> None:
        self._install_or_update("install")

    def _update(self) -> None:
        self._install_or_update("update")

    def _install_or_update(self, action: str) -> None:
        if self._inspection is None or self._candidate is None:
            return
        if not selected_game_matches_inspection(
                self.game_path.get(), self._inspection):
            self._candidate = None
            messagebox.showerror(
                "Verification required",
                "The selected game folder no longer matches the verified "
                "inspection. Choose the folder again and rebuild the candidate.")
            self._set_actions()
            return
        if self._candidate.compatibility_test:
            confirmed = messagebox.askyesno(
                "Final compatibility warning",
                "This candidate was adapted locally for an unlisted game build. "
                "It compiled and passed the isolated archive checks, but it has "
                "not received normal signed-build compatibility testing.\n\n"
                "The Manager will create a permanent hash-verified vanilla backup "
                "before installation, and Restore Vanilla remains available. "
                "Continue?")
        else:
            confirmed = messagebox.askyesno(
                "Confirm " + action,
                "The Manager will reverify the game, signed-package-bound candidate, "
                "and vanilla backup before atomically replacing Client.exe. Continue?")
        if not confirmed:
            return
        try:
            result = self._transaction.install_or_update(
                self._inspection, self._candidate)
        except (TransactionError, OSError, ManagerDataError) as error:
            messagebox.showerror("Operation blocked", str(error))
        else:
            candidate = self._candidate
            self._candidate = None
            cleanup_error = self._cleanup_candidate(candidate)
            messagebox.showinfo(
                "Completed",
                f"{result.action.title()} completed. Original backup:\n"
                f"{result.original_backup}"
                + ("" if cleanup_error is None else
                   f"\n\nTemporary cleanup warning: {cleanup_error}"))
        self.refresh()

    def _restore(self) -> None:
        if self._inspection is None:
            return
        if not messagebox.askyesno(
                "Restore vanilla Client.exe",
                "This restores only the recorded, hash-verified vanilla backup.\n\nContinue?"):
            return
        try:
            result = self._transaction.restore(self._inspection)
        except (TransactionError, OSError, ManagerDataError) as error:
            messagebox.showerror("Restore blocked", str(error))
        else:
            self._candidate = None
            messagebox.showinfo(
                "Vanilla restored",
                f"Original Client.exe restored from:\n{result.original_backup}")
        self.refresh()

    def _repair(self) -> None:
        if not self._transaction.has_interrupted_operation():
            return
        if not messagebox.askyesno(
                "Repair interrupted operation",
                "The Manager will classify the journal, Client.exe, backup, and state "
                "by exact hashes. It will not guess. Continue?"):
            return
        try:
            result = self._transaction.recover()
        except (TransactionError, OSError, ManagerDataError) as error:
            messagebox.showerror("Automatic repair blocked", str(error))
        else:
            messagebox.showinfo(
                "Repair complete",
                f"Interrupted {result.action} was {result.outcome} safely.")
        self.refresh()

    def _diagnostics(self, *, select_tab: bool = True) -> None:
        selected_game = self.game_path.get().strip()
        game_root = (
            Path(selected_game).expanduser() if selected_game else None)
        try:
            value = current_diagnostics_text(
                game_root=game_root, mod_state_root=self._mods.state_root)
        except (DiagnosticsError, ManagerDataError, OSError) as error:
            value = (
                "CURRENT DIAGNOSTICS\n"
                "===================\n"
                f"Unable to read diagnostics: {type(error).__name__}: {error}\n"
            )
        if self._inspection is None:
            session = (
                "Manager status: Not checked yet\n"
                "Compatibility package: Not selected\n"
                "Prepared candidate: None")
        else:
            pack = self._inspection.pack
            session = (
                "Manager status: "
                f"{self._inspection.status.value.replace('_', ' ').title()}\n"
                "Compatibility package: "
                f"{pack.mod_version if pack else 'Not selected'}\n"
                "Prepared candidate: "
                f"{'Ready' if self._candidate else 'None'}"
            )
        journal_status = (
            "Pending recovery" if self._transaction.journal_path.is_file()
            else "None")
        rendered = (
            "MANAGER SESSION\n"
            "===============\n"
            f"{session}\n"
            f"Interrupted operation: {journal_status}\n\n"
            f"{value}"
        )
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.insert(END, rendered)
        self.log_text.configure(state=DISABLED)
        self.log_text.see(END)
        if select_tab:
            self.notebook.select(self.logs_tab)

    def _export_diagnostics(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Export Star Empire Mod Manager diagnostics",
            defaultextension=".zip",
            filetypes=(("ZIP archive", "*.zip"),))
        if not selected:
            return
        game_root = None
        selected_game = self.game_path.get().strip()
        if selected_game:
            game_root = Path(selected_game).expanduser()
        try:
            output = export_diagnostics(
                Path(selected), game_root=game_root,
                mod_state_root=self._mods.state_root)
        except (DiagnosticsError, FileExistsError, OSError) as error:
            messagebox.showerror("Diagnostics export blocked", str(error))
        else:
            messagebox.showinfo(
                "Diagnostics exported",
                f"Diagnostic report written to:\n{output}")

    def _save_manager_settings(self) -> None:
        requested_debug = bool(self.debug_logging.get())
        requested_dark = bool(self.dark_mode.get())
        try:
            save_manager_settings(
                self._settings_path,
                ManagerSettings(
                    debug_logging=requested_debug, dark_mode=requested_dark))
        except (ManagerSettingsError, OSError) as error:
            self.debug_logging.set(self._saved_debug_logging)
            self.dark_mode.set(self._saved_dark_mode)
            self._apply_theme(self._saved_dark_mode)
            messagebox.showerror("Settings not saved", str(error))
        else:
            self._saved_debug_logging = requested_debug
            self._saved_dark_mode = requested_dark


def main() -> None:
    root = create_drop_root()
    ManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
