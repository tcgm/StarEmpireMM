import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from installer.elevated_copy import (
    ElevationCancelled,
    ElevationError,
    _perform_elevated_copy,
    handle_elevated_copy_request,
    request_elevated_atomic_copy,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class ElevatedCopyTests(unittest.TestCase):
    @staticmethod
    def _setup(root: Path) -> tuple[Path, Path, Path]:
        state = root / "state"
        source = state / "work" / "candidate" / "Client.exe"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"modded client")
        game = root / "game"
        game.mkdir()
        target = game / "Client.exe"
        target.write_bytes(b"official client")
        (game / "StarEmpireLauncher.exe").write_bytes(b"launcher")
        (game / "version.txt").write_text("0.4.63\n", encoding="utf-8")
        return state, source, target

    @staticmethod
    def _write_request(state: Path, source: Path, target: Path,
                       expected: str, before: str) -> Path:
        request_root = state / "elevation-requests"
        request_root.mkdir(parents=True, exist_ok=True)
        request = request_root / ("copy-" + "1" * 32 + ".json")
        request.write_text(json.dumps({
            "schema": 1,
            "request_id": "1" * 32,
            "source": str(source),
            "target": str(target),
            "expected_sha256": expected,
            "before_sha256": before,
        }), encoding="utf-8")
        return request

    def test_parent_and_restricted_helper_complete_verified_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, source, target = self._setup(Path(temporary))

            def launch(request: Path) -> int:
                return handle_elevated_copy_request(
                    request, state_root=state, process_probe=lambda: ())

            request_elevated_atomic_copy(
                state, source, target, _hash(source), _hash(target),
                launcher=launch)

            self.assertEqual(b"modded client", target.read_bytes())
            self.assertEqual([], list((state / "elevation-requests").glob("*.json")))

    def test_source_outside_manager_work_and_backups_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, source, target = self._setup(root)
            outside = root / "untrusted.exe"
            outside.write_bytes(source.read_bytes())
            request = self._write_request(
                state, outside, target, _hash(outside), _hash(target))

            with self.assertRaisesRegex(ElevationError, "outside"):
                _perform_elevated_copy(request, state, lambda: ())

            self.assertEqual(b"official client", target.read_bytes())

    def test_unrecognised_target_and_changed_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, source, target = self._setup(root)
            (target.parent / "version.txt").unlink()
            request = self._write_request(
                state, source, target, _hash(source), _hash(target))
            with self.assertRaisesRegex(ElevationError, "recognised"):
                _perform_elevated_copy(request, state, lambda: ())

            (target.parent / "version.txt").write_text("0.4.63\n", encoding="utf-8")
            before = _hash(target)
            request = self._write_request(
                state, source, target, _hash(source), before)
            target.write_bytes(b"game update")
            with self.assertRaisesRegex(ElevationError, "changed"):
                _perform_elevated_copy(request, state, lambda: ())

            self.assertEqual(b"game update", target.read_bytes())

    def test_declined_uac_is_cleanly_reported_and_request_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, source, target = self._setup(Path(temporary))

            def cancel(_request: Path) -> int:
                raise ElevationCancelled("declined")

            with self.assertRaises(ElevationCancelled):
                request_elevated_atomic_copy(
                    state, source, target, _hash(source), _hash(target),
                    launcher=cancel)

            self.assertEqual(b"official client", target.read_bytes())
            self.assertEqual([], list((state / "elevation-requests").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
