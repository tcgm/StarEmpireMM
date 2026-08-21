from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from installer.manager_settings import (
    ManagerSettings, ManagerSettingsError, load_manager_settings,
    save_manager_settings,
)
from mod_loader.host_client import configure_mod_logging


class ManagerSettingsTests(unittest.TestCase):
    def test_missing_defaults_and_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "manager-settings.json"
            self.assertEqual(ManagerSettings(), load_manager_settings(path))
            save_manager_settings(path, ManagerSettings(debug_logging=True))
            self.assertEqual(
                ManagerSettings(debug_logging=True), load_manager_settings(path))
            self.assertFalse(tuple(path.parent.glob("*.partial")))

    def test_malformed_or_duplicate_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-settings.json"
            for payload in (
                    '{"schema":1,"schema":1,"debug_logging":false}',
                    '{"schema":1,"debug_logging":"yes"}',
                    '{"schema":2,"debug_logging":false}'):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises(ManagerSettingsError):
                    load_manager_settings(path)

    def test_failed_replace_preserves_previous_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-settings.json"
            save_manager_settings(path, ManagerSettings())
            previous = path.read_bytes()
            with patch("pathlib.Path.replace", side_effect=OSError("blocked")):
                with self.assertRaises(ManagerSettingsError):
                    save_manager_settings(path, ManagerSettings(True))
            self.assertEqual(previous, path.read_bytes())

    def test_loader_uses_debug_level_only_when_setting_is_strictly_true(self):
        import logging

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_manager_settings(
                root / "manager-settings.json", ManagerSettings(True))
            path = configure_mod_logging(root)
            self.assertEqual(logging.DEBUG, logging.getLogger("mod_loader").level)
            save_manager_settings(
                root / "manager-settings.json", ManagerSettings(False))
            configure_mod_logging(root)
            self.assertEqual(logging.INFO, logging.getLogger("mod_loader").level)
            for namespace in ("star_empire_mod", "mod_loader"):
                target = logging.getLogger(namespace)
                for handler in tuple(target.handlers):
                    if getattr(handler, "_star_empire_mod_log", None) == path:
                        target.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()
