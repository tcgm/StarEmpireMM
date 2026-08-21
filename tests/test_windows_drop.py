from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from installer.manager_ui import ManagerApp
from installer.windows_drop import (WindowsFileDrop, create_drop_root,
                                    install_windows_file_drop,
                                    unique_semod_paths)


class _FakeTk:
    def __init__(self, values=()) -> None:
        self.values = tuple(values)

    def splitlist(self, _data):
        return self.values


class _FakeDropRoot:
    def __init__(self, values=()) -> None:
        self.tk = _FakeTk(values)
        self.registered = []
        self.bindings = []
        self.idle_callbacks = []
        self.unregistered = 0

    def drop_target_register(self, value) -> None:
        self.registered.append(value)

    def drop_target_unregister(self) -> None:
        self.unregistered += 1

    def dnd_bind(self, sequence, callback) -> None:
        self.bindings.append((sequence, callback))

    def bind(self, sequence, callback, add=None) -> None:
        self.bindings.append((sequence, callback, add))

    def after_idle(self, callback) -> None:
        self.idle_callbacks.append(callback)


class WindowsDropTests(unittest.TestCase):
    def test_semod_filter_is_ordered_case_insensitive_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "First.semod"
            second = root / "second.SEMOD"
            ignored = root / "notes.txt"
            self.assertEqual(
                (first.resolve(), second.resolve()),
                unique_semod_paths((first, ignored, first, second)),
            )

    def test_install_without_dnd_support_is_a_noop(self) -> None:
        self.assertIsNone(install_windows_file_drop(object(), lambda _p: None))

    def test_drop_uses_tcl_list_parser_and_schedules_paths(self) -> None:
        root = _FakeDropRoot((r"C:\One File.semod", r"C:\Two.semod"))
        received = []
        target = WindowsFileDrop(root, received.append)

        result = target._on_drop(SimpleNamespace(data="ignored"))
        self.assertEqual("copy", result)
        self.assertEqual(1, len(root.idle_callbacks))
        root.idle_callbacks[0]()
        self.assertEqual(
            [(Path(r"C:\One File.semod"), Path(r"C:\Two.semod"))], received)

    def test_close_unregisters_once_and_late_drop_is_refused(self) -> None:
        root = _FakeDropRoot((r"C:\One.semod",))
        target = WindowsFileDrop(root, lambda _paths: None)
        target.close()
        target.close()
        self.assertEqual(1, root.unregistered)
        self.assertEqual(
            "refuse_drop", target._on_drop(SimpleNamespace(data="ignored")))

    def test_drop_root_falls_back_when_tkdnd_initialization_fails(self) -> None:
        dnd = SimpleNamespace(Tk=unittest.mock.Mock(side_effect=RuntimeError))
        fallback = object()
        with patch("installer.windows_drop.TkinterDnD", dnd), patch(
                "installer.windows_drop.Tk", return_value=fallback):
            self.assertIs(fallback, create_drop_root())

    def test_drop_rejects_non_mod_files_without_preparing_game(self) -> None:
        app = object.__new__(ManagerApp)
        app._install_external_mod_paths = unittest.mock.Mock()
        with patch("installer.manager_ui.messagebox.showerror") as error:
            app._handle_dropped_mod_files((Path("picture.png"),))
        error.assert_called_once()
        app._install_external_mod_paths.assert_not_called()

    def test_drop_installs_verified_batch_disabled_without_game_preparation(self) -> None:
        app = object.__new__(ManagerApp)
        app._prepare_game_for_mod_install = unittest.mock.Mock(return_value=True)
        app._mods = SimpleNamespace(install=unittest.mock.Mock(side_effect=(
            SimpleNamespace(mod_id="first", version="1.0"),
            SimpleNamespace(mod_id="second", version="2.0"),
        )))
        app.status_text = SimpleNamespace(set=unittest.mock.Mock())
        app.summary_text = SimpleNamespace(set=unittest.mock.Mock())
        app._refresh_mods = unittest.mock.Mock()
        paths = (Path("first.semod"), Path("second.semod"))
        with patch("installer.manager_ui.verify_semod_package") as verify:
            app._install_external_mod_paths(paths)

        self.assertEqual(2, verify.call_count)
        app._prepare_game_for_mod_install.assert_not_called()
        self.assertEqual(2, app._mods.install.call_count)
        for call in app._mods.install.call_args_list:
            self.assertFalse(call.kwargs["enable"])
        app.status_text.set.assert_called_once_with("2 mods installed disabled")
        app.summary_text.set.assert_called_once_with(
            "Select a mod and click Enable next launch when it is ready to use.")
        app._refresh_mods.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
