from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from installer.diagnostics import (DiagnosticsError,
                                   current_diagnostics_text,
                                   export_diagnostics, parse_log_events)
from installer.mod_registry import ModRegistry, save_mod_registry
from installer.mod_service import ModService
from tests.test_mod_service import _package


class DiagnosticsTests(unittest.TestCase):
    def test_current_text_contains_report_and_newest_bounded_log_tails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            save_mod_registry(state / "mods.json", ModRegistry.empty())
            logs = state / "logs"
            logs.mkdir()
            for index in range(5):
                path = logs / f"star-empire-mods.log.{index}"
                path.write_text(
                    f"diagnostic line {index}\n", encoding="utf-8")
                path.touch()

            text = current_diagnostics_text(
                game_root=None, mod_state_root=state)

            self.assertIn("CURRENT STATUS", text)
            self.assertIn("RECENT MOD EVENTS", text)
            self.assertIn("Manager version:", text)
            self.assertEqual(4, text.count("Log file:"))
            self.assertIn("diagnostic line 4", text)
            self.assertNotIn("diagnostic line 0", text)

    def test_current_text_explains_when_no_logs_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            text = current_diagnostics_text(
                game_root=None, mod_state_root=Path(temporary) / "state")
        self.assertIn("No mod events have been logged yet.", text)

    def test_structured_log_is_summarised_with_cause_not_full_traceback(self):
        payload = (
            "2026-08-20 21:59:23,793 ERROR "
            "star_empire_mod_loader.host_client "
            "STAR_EMPIRE_MOD_LOADER_EVENT_FAILED\n"
            "Traceback (most recent call last):\n"
            "  File \"host_client.py\", line 188, in handle_event\n"
            "TypeError: emit() got multiple values for argument 'event'\n"
        ).encode()

        events = parse_log_events(payload)

        self.assertEqual(1, len(events))
        self.assertEqual("ERROR", events[0].level)
        self.assertEqual(
            "The game could not pass an input event to the mod loader.",
            events[0].message)
        self.assertEqual(
            "TypeError: emit() got multiple values for argument 'event'",
            events[0].cause)

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "logs").mkdir(parents=True)
            (state / "logs" / "star-empire-mods.log").write_bytes(payload)
            text = current_diagnostics_text(
                game_root=None, mod_state_root=state)
        self.assertIn("The game could not pass an input event", text)
        self.assertIn("Cause: TypeError:", text)
        self.assertNotIn("File \"host_client.py\"", text)

    def test_export_contains_report_bounded_logs_and_no_game_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            (game / "version.txt").write_text('"0.4.47"\n', encoding="utf-8")
            game_bytes = b"MZsecret-game-binary"
            (game / "Client.exe").write_bytes(game_bytes)
            state = root / "state"
            save_mod_registry(state / "mods.json", ModRegistry.empty())
            (state / "logs").mkdir()
            (state / "logs" / "star-empire-mods.log").write_text(
                "one useful line\n", encoding="utf-8")

            output = export_diagnostics(
                root / "diagnostics.zip", game_root=game,
                mod_state_root=state)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    {"report.json", "SHA256SUMS.json",
                     "logs/star-empire-mods.log"},
                    set(archive.namelist()))
                report = json.loads(archive.read("report.json"))
                self.assertEqual("0.4.47", report["game"]["version"])
                combined = b"".join(
                    archive.read(name) for name in archive.namelist())
            self.assertNotIn(game_bytes, combined)
            self.assertNotIn(b"Client.py", combined)

    def test_export_records_force_load_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            service = ModService(state, process_probe=lambda: ())
            service.install(
                _package(root / "packages", "example.mod"), source="local")
            service.set_force_load("example.mod", True)

            output = export_diagnostics(
                root / "diagnostics.zip", game_root=None,
                mod_state_root=state)
            with zipfile.ZipFile(output) as archive:
                report = json.loads(archive.read("report.json"))

            self.assertTrue(report["mods"][0]["force_load"])

    def test_invalid_extension_and_overwrite_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(DiagnosticsError, "zip"):
                export_diagnostics(
                    root / "report.txt", game_root=None,
                    mod_state_root=root / "state")
            output = root / "report.zip"
            output.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                export_diagnostics(
                    output, game_root=None, mod_state_root=root / "state")
            self.assertEqual(b"existing", output.read_bytes())


if __name__ == "__main__":
    unittest.main()
