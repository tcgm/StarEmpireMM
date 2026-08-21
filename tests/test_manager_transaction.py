import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from installer.manager_core import (CompatibilityPack, ProcessProbeResult,
                                    inspect_installation, load_install_state)
from installer.manager_transaction import (ManagerTransaction, TransactionError,
                                           TransactionJournal)
from installer.elevated_copy import ElevationCancelled
from installer.candidate_builder import BuiltCandidate


HASH = lambda text: __import__("hashlib").sha256(text.encode("utf-8")).hexdigest().upper()


class ManagerTransactionTests(unittest.TestCase):
    @staticmethod
    def _built(candidate: Path, pack: CompatibilityPack) -> BuiltCandidate:
        audit = candidate.with_name(candidate.name + ".audit.json")
        audit.write_text("{}\n", encoding="utf-8")
        return BuiltCandidate(
            candidate, pack.expected_client_sha256,
            pack.official_client_sha256, pack.pack_id, pack.pack_digest,
            pack.key_id, ("Client",), audit)

    def _setup(self, root: Path):
        game = root / "game"
        game.mkdir()
        (game / "Client.exe").write_text("official", encoding="utf-8")
        (game / "StarEmpireLauncher.exe").write_text("launcher", encoding="utf-8")
        candidate = root / "candidate" / "Client.exe"
        candidate.parent.mkdir()
        candidate.write_text("modded", encoding="utf-8")
        pack_path = root / "pack.json"
        pack_path.write_text(json.dumps({
            "schema": 1, "pack_id": "0.1.56", "mod_version": "0.1.56",
            "game_version": "test", "official_client_sha256": HASH("official"),
            "expected_client_sha256": HASH("modded"),
        }), encoding="utf-8")
        return game, candidate, CompatibilityPack.load(pack_path)

    def test_install_then_restore_preserves_verified_vanilla_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            inspection = inspect_installation(game, pack, state_path)
            installed = transaction.install_or_update(
                inspection, self._built(candidate, pack))

            self.assertEqual("install", installed.action)
            self.assertEqual("official", installed.original_backup.read_text(encoding="utf-8"))
            self.assertEqual("modded", (game / "Client.exe").read_text(encoding="utf-8"))
            state = load_install_state(state_path)
            self.assertIsNotNone(state)
            self.assertEqual(pack.pack_digest, state.pack_digest)
            self.assertEqual("0.1.56", state.mod_version)
            self.assertEqual("test", state.game_version)
            self.assertEqual("", state.compatibility_source_game_version)

            healthy = inspect_installation(game, pack, state_path)
            restored = transaction.restore(healthy)
            self.assertEqual("restore", restored.action)
            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())

    def test_access_denied_uses_restricted_copy_for_install_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            calls = []

            def elevated(source: Path, target: Path,
                         expected: str, before: str) -> None:
                calls.append((Path(source), Path(target), expected, before))
                shutil.copy2(source, target)

            transaction = ManagerTransaction(
                state_path, process_probe=lambda: (), elevated_copy=elevated)
            transaction._backup_original(game / "Client.exe", HASH("official"))
            with patch.object(
                    transaction, "_replace_pending",
                    side_effect=PermissionError(5, "Access is denied")):
                transaction.install_or_update(
                    inspect_installation(game, pack, state_path),
                    self._built(candidate, pack))
                transaction.restore(
                    inspect_installation(game, pack, state_path))

            self.assertEqual(2, len(calls))
            self.assertEqual(HASH("official"), calls[0][3])
            self.assertEqual(HASH("modded"), calls[1][3])
            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(transaction.journal_path.exists())

    def test_declined_permission_leaves_client_and_transaction_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"

            def decline(_source: Path, _target: Path,
                        _expected: str, _before: str) -> None:
                raise ElevationCancelled("declined")

            transaction = ManagerTransaction(
                state_path, process_probe=lambda: (), elevated_copy=decline)
            transaction._backup_original(game / "Client.exe", HASH("official"))
            with patch.object(
                    transaction, "_replace_pending",
                    side_effect=PermissionError(5, "Access is denied")):
                with self.assertRaisesRegex(
                        TransactionError, "permission was declined"):
                    transaction.install_or_update(
                        inspect_installation(game, pack, state_path),
                        self._built(candidate, pack))

            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())

    def test_compatibility_candidate_installs_with_observed_hashes_and_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            (game / "version.txt").write_text("0.4.46\n", encoding="utf-8")
            (game / "Client.exe").write_text(
                "unlisted official", encoding="utf-8")
            candidate.write_text("compatibility modded", encoding="utf-8")
            audit = candidate.with_name("compatibility-audit.json")
            audit.write_text("{}\n", encoding="utf-8")
            built = BuiltCandidate(
                candidate, HASH("compatibility modded"),
                HASH("unlisted official"), pack.pack_id, pack.pack_digest,
                pack.key_id, ("Client",), audit, True, "0.4.46")
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            inspection = inspect_installation(game, pack, state_path)

            result = transaction.install_or_update(inspection, built)

            self.assertEqual("install", result.action)
            self.assertEqual(HASH("compatibility modded"), result.client_sha256)
            self.assertEqual(
                "compatibility modded",
                (game / "Client.exe").read_text(encoding="utf-8"))
            state = load_install_state(state_path)
            self.assertEqual(HASH("unlisted official"), state.original_sha256)
            self.assertEqual(HASH("compatibility modded"), state.installed_sha256)
            self.assertEqual("0.4.46", state.game_version)
            self.assertEqual(
                pack.game_version, state.compatibility_source_game_version)
            self.assertEqual(pack.pack_id, state.pack_id)
            self.assertEqual(pack.pack_digest, state.pack_digest)

            healthy = inspect_installation(game, pack, state_path)
            transaction.restore(healthy)
            self.assertEqual(
                "unlisted official",
                (game / "Client.exe").read_text(encoding="utf-8"))

    def test_compatibility_candidate_must_match_observed_host_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            (game / "version.txt").write_text("0.4.46\n", encoding="utf-8")
            (game / "Client.exe").write_text(
                "unlisted official", encoding="utf-8")
            candidate.write_text("compatibility modded", encoding="utf-8")
            inspection = inspect_installation(game, pack)
            transaction = ManagerTransaction(
                root / "state.json", process_probe=lambda: ())
            audit = root / "audit.json"
            audit.write_text("{}\n", encoding="utf-8")

            for label, official, version in (
                    ("hash", HASH("another build"), "0.4.46"),
                    ("version", HASH("unlisted official"), "0.4.47")):
                with self.subTest(label=label):
                    built = BuiltCandidate(
                        candidate, HASH("compatibility modded"), official,
                        pack.pack_id, pack.pack_digest, pack.key_id,
                        ("Client",), audit, True, version)
                    with self.assertRaisesRegex(
                            TransactionError, "evidence no longer matches"):
                        transaction.install_or_update(inspection, built)
            self.assertEqual(
                "unlisted official",
                (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(transaction.backup_root.exists())

            ordinary = BuiltCandidate(
                candidate, HASH("compatibility modded"),
                HASH("unlisted official"), pack.pack_id, pack.pack_digest,
                pack.key_id, ("Client",), audit)
            with self.assertRaisesRegex(TransactionError, "blocked"):
                transaction.install_or_update(inspection, ordinary)

    def test_rejects_wrong_candidate_without_creating_a_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            candidate.write_text("wrong", encoding="utf-8")
            transaction = ManagerTransaction(root / "state.json", process_probe=lambda: ())
            with self.assertRaisesRegex(TransactionError, "no longer matches"):
                transaction.install_or_update(
                    inspect_installation(game, pack), self._built(candidate, pack))
            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(transaction.backup_root.exists())

    def test_stale_journal_blocks_every_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            transaction = ManagerTransaction(root / "state.json")
            transaction.journal_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(TransactionError, "previous install"):
                transaction.install_or_update(
                    inspect_installation(game, pack), self._built(candidate, pack))
            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))

    def test_rechecks_running_game_and_changed_client_at_transaction_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            inspection = inspect_installation(game, pack)
            running = ManagerTransaction(root / "state.json", process_probe=lambda: ("Client.exe",))
            with self.assertRaisesRegex(TransactionError, "Close Star Empire"):
                running.install_or_update(inspection, self._built(candidate, pack))

            changed = ManagerTransaction(root / "other-state.json", process_probe=lambda: ())
            (game / "Client.exe").write_text("outside change", encoding="utf-8")
            with self.assertRaisesRegex(TransactionError, "changed after verification"):
                changed.install_or_update(inspection, self._built(candidate, pack))
            self.assertEqual("outside change", (game / "Client.exe").read_text(encoding="utf-8"))

    def test_rechecks_version_and_serializes_multiple_managers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            version = game / "version.txt"
            version.write_text("test\n", encoding="utf-8")
            inspection = inspect_installation(game, pack)
            version.write_text("new-test\n", encoding="utf-8")
            transaction = ManagerTransaction(
                root / "state.json", process_probe=lambda: ())
            with self.assertRaisesRegex(TransactionError, "version.txt changed"):
                transaction.install_or_update(
                    inspection, self._built(candidate, pack))
            self.assertEqual(
                "official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(transaction.backup_root.exists())

            first = ManagerTransaction(
                root / "shared-state.json", process_probe=lambda: ())
            second = ManagerTransaction(
                root / "shared-state.json", process_probe=lambda: ())
            with first._exclusive_transaction():
                with self.assertRaisesRegex(TransactionError, "already active"):
                    with second._exclusive_transaction():
                        self.fail("second Manager acquired the transaction lock")

    def test_rechecks_game_and_candidate_after_backup_before_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state.json"
            transaction = ManagerTransaction(
                state_path, process_probe=lambda: ())
            inspection = inspect_installation(game, pack, state_path)
            built = self._built(candidate, pack)
            original_backup = transaction._backup_original

            def mutate_client_after_backup(source, digest):
                backup = original_backup(source, digest)
                source.write_text("launcher update", encoding="utf-8")
                return backup

            with patch.object(
                    transaction, "_backup_original",
                    side_effect=mutate_client_after_backup):
                with self.assertRaisesRegex(
                        TransactionError, "changed after verification"):
                    transaction.install_or_update(inspection, built)

            self.assertEqual(
                "launcher update",
                (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state.json"
            transaction = ManagerTransaction(
                state_path, process_probe=lambda: ())
            inspection = inspect_installation(game, pack, state_path)
            built = self._built(candidate, pack)
            original_backup = transaction._backup_original

            def mutate_candidate_after_backup(source, digest):
                backup = original_backup(source, digest)
                candidate.write_text("changed candidate", encoding="utf-8")
                return backup

            with patch.object(
                    transaction, "_backup_original",
                    side_effect=mutate_candidate_after_backup):
                with self.assertRaisesRegex(
                        TransactionError, "no longer matches"):
                    transaction.install_or_update(inspection, built)

            self.assertEqual(
                "official",
                (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())

    def test_unknown_or_failed_process_probe_blocks_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            inspection = inspect_installation(game, pack)
            unknown = ManagerTransaction(
                root / "state.json",
                process_probe=lambda: ProcessProbeResult.unknown("tasklist denied"))
            with self.assertRaisesRegex(TransactionError, "tasklist denied"):
                unknown.install_or_update(inspection, self._built(candidate, pack))

            failed = ManagerTransaction(
                root / "other-state.json",
                process_probe=lambda: (_ for _ in ()).throw(OSError("probe failed")))
            with self.assertRaisesRegex(TransactionError, "Cannot verify"):
                failed.install_or_update(inspection, self._built(candidate, pack))

            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(unknown.backup_root.exists())
            self.assertFalse(failed.backup_root.exists())

    def test_update_reuses_the_verified_original_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, first_pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            transaction.install_or_update(
                inspect_installation(game, first_pack, state_path),
                self._built(candidate, first_pack))

            update_candidate = root / "candidate" / "Client-new.exe"
            update_candidate.write_text("modded-new", encoding="utf-8")
            update_manifest = root / "update-pack.json"
            update_manifest.write_text(json.dumps({
                "schema": 1, "pack_id": "0.1.57", "mod_version": "0.1.57",
                "game_version": "test", "official_client_sha256": HASH("official"),
                "expected_client_sha256": HASH("modded-new"),
            }), encoding="utf-8")
            update_pack = CompatibilityPack.load(update_manifest)
            result = transaction.install_or_update(
                inspect_installation(game, update_pack, state_path),
                self._built(update_candidate, update_pack))

            self.assertEqual("update", result.action)
            self.assertEqual("official", result.original_backup.read_text(encoding="utf-8"))
            self.assertEqual("modded-new", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertEqual("0.1.57", load_install_state(state_path).pack_id)
            self.assertEqual(
                update_pack.pack_digest,
                load_install_state(state_path).pack_digest)

    def test_recovery_commits_state_after_target_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            with patch("installer.manager_transaction.save_install_state",
                       side_effect=OSError("simulated state write failure")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    transaction.install_or_update(
                        inspect_installation(game, pack, state_path),
                        self._built(candidate, pack))

            self.assertEqual("modded", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())
            self.assertTrue(transaction.journal_path.exists())

            recovered = transaction.recover()
            self.assertEqual("completed", recovered.outcome)
            self.assertEqual("install", recovered.action)
            self.assertEqual("0.1.56", load_install_state(state_path).pack_id)
            self.assertFalse(transaction.journal_path.exists())
            with self.assertRaisesRegex(TransactionError, "no interrupted"):
                transaction.recover()

    def test_restore_rechecks_game_after_backup_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(
                state_path, process_probe=lambda: ())
            transaction.install_or_update(
                inspect_installation(game, pack, state_path),
                self._built(candidate, pack))
            healthy = inspect_installation(game, pack, state_path)
            state = healthy.state
            self.assertIsNotNone(state)
            original_sha256_file = __import__(
                "installer.manager_transaction", fromlist=["sha256_file"]
            ).sha256_file
            changed = False

            def mutate_after_backup_check(path):
                nonlocal changed
                digest = original_sha256_file(path)
                if Path(path) == state.original_client and not changed:
                    (game / "Client.exe").write_text(
                        "launcher update", encoding="utf-8")
                    changed = True
                return digest

            with patch(
                    "installer.manager_transaction.sha256_file",
                    side_effect=mutate_after_backup_check):
                with self.assertRaisesRegex(
                        TransactionError, "changed after verification"):
                    transaction.restore(healthy)

            self.assertEqual(
                "launcher update",
                (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertTrue(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())

    def test_recovery_abandons_prepared_operation_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            transaction._backup_original(game / "Client.exe", HASH("official"))
            with patch.object(transaction, "_atomic_copy",
                              side_effect=OSError("simulated replacement failure")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    transaction.install_or_update(
                        inspect_installation(game, pack, state_path),
                        self._built(candidate, pack))

            recovered = transaction.recover()
            self.assertEqual("abandoned", recovered.outcome)
            self.assertEqual("official", (game / "Client.exe").read_text(encoding="utf-8"))
            self.assertFalse(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())

    def test_recovery_refuses_unknown_target_or_changed_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            transaction._backup_original(game / "Client.exe", HASH("official"))
            with patch.object(transaction, "_atomic_copy",
                              side_effect=OSError("simulated replacement failure")):
                with self.assertRaises(OSError):
                    transaction.install_or_update(
                        inspect_installation(game, pack, state_path),
                        self._built(candidate, pack))
            (game / "Client.exe").write_text("unknown bytes", encoding="utf-8")

            with self.assertRaisesRegex(TransactionError, "neither the before nor after"):
                transaction.recover()
            self.assertTrue(transaction.journal_path.exists())

    def test_recovery_finishes_interrupted_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, candidate, pack = self._setup(root)
            state_path = root / "state" / "install-state.json"
            transaction = ManagerTransaction(state_path, process_probe=lambda: ())
            transaction.install_or_update(
                inspect_installation(game, pack, state_path),
                self._built(candidate, pack))
            state = load_install_state(state_path)
            self.assertIsNotNone(state)
            (game / "Client.exe").write_text("official", encoding="utf-8")
            journal = TransactionJournal(
                "restore", "target_replaced", game / "Client.exe",
                HASH("modded"), HASH("official"), state.original_client,
                HASH("official"), state, None, "restore-test", "now")
            transaction._save_journal(journal)

            recovered = transaction.recover()
            self.assertEqual("completed", recovered.outcome)
            self.assertEqual("restore", recovered.action)
            self.assertFalse(state_path.exists())
            self.assertFalse(transaction.journal_path.exists())


if __name__ == "__main__":
    unittest.main()
