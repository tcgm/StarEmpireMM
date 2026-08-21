"""Stable Tk file-drop adapter with a normal file-picker fallback."""

from __future__ import annotations

from pathlib import Path
from tkinter import Tk
from typing import Callable, Iterable

try:
    from tkinterdnd2 import COPY, DND_FILES, TkinterDnD
except (ImportError, OSError):  # The Manager remains usable without DnD.
    COPY = "copy"
    DND_FILES = "DND_Files"
    TkinterDnD = None


class WindowsFileDrop:
    """Receive native shell file drops through the maintained TkDND bridge."""

    def __init__(self, root, on_files: Callable[[tuple[Path, ...]], None]) -> None:
        if not callable(getattr(root, "drop_target_register", None)):
            raise OSError("Tk drag-and-drop support is unavailable")
        if not callable(getattr(root, "dnd_bind", None)):
            raise OSError("Tk drag-and-drop event binding is unavailable")
        self._root = root
        self._on_files = on_files
        self._closed = False
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", self._on_drop)
        root.bind("<Destroy>", self._on_destroy, add="+")

    def _on_drop(self, event):
        if self._closed:
            return "refuse_drop"
        try:
            paths = tuple(Path(value) for value in self._root.tk.splitlist(event.data))
        except (AttributeError, TypeError, ValueError):
            return "refuse_drop"
        if paths:
            self._root.after_idle(
                lambda selected=paths: self._on_files(selected))
        return COPY

    def _on_destroy(self, event) -> None:
        if event.widget is self._root:
            self._closed = True

    def close(self) -> None:
        """Unregister the drop target exactly once when the window is alive."""
        if self._closed:
            return
        self._closed = True
        try:
            self._root.dnd_bind("<<Drop>>", "")
            self._root.drop_target_unregister()
        except Exception:
            # Closing the Tk window may already have removed the Tcl commands.
            pass


def create_drop_root():
    """Create a DnD-capable root, or a normal Tk root as a safe fallback."""
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except (OSError, RuntimeError):
            pass
    return Tk()


def install_windows_file_drop(
        root, on_files: Callable[[tuple[Path, ...]], None],
) -> WindowsFileDrop | None:
    """Install stable file drop when available; file-picker use remains valid."""
    try:
        return WindowsFileDrop(root, on_files)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def unique_semod_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Return ordered, case-insensitively unique .semod file paths."""
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != ".semod":
            continue
        identity = str(path).casefold()
        if identity not in seen:
            seen.add(identity)
            result.append(path)
    return tuple(result)


__all__ = (
    "WindowsFileDrop", "create_drop_root", "install_windows_file_drop",
    "unique_semod_paths",
)
