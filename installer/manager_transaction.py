"""Atomic, hash-verified file transactions for the UI Mod Manager.

The manager receives a locally built candidate from authorised private tooling;
it never ships or downloads a game executable.  Every write is guarded by an
inspection result, a journal, and SHA-256 verification.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .candidate_builder import BuiltCandidate, CandidateBuildError
from .elevated_copy import (ElevationCancelled, ElevationError,
                            request_elevated_atomic_copy)
from .manager_core import (GAME_PROCESS_NAMES, CompatibilityPack, Inspection,
                           InstallState, InstallStatus, ManagerDataError,
                           ProcessProbeResult, running_game_processes,
                           load_install_state, read_game_version,
                           save_install_state, sha256_file)


class TransactionError(RuntimeError):
    """Raised before an unsafe file operation can begin."""


@dataclass(frozen=True)
class TransactionResult:
    action: Literal["install", "update", "restore"]
    original_backup: Path
    client_sha256: str


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of an explicit, hash-classified interrupted-operation repair."""

    outcome: Literal["abandoned", "completed"]
    action: Literal["install", "update", "restore"]
    client_sha256: str


@dataclass(frozen=True)
class TransactionJournal:
    """Durable evidence required to recover without guessing."""

    action: Literal["install", "update", "restore"]
    phase: Literal["prepared", "target_replaced", "state_committed"]
    target: Path
    before_sha256: str
    after_sha256: str
    original_backup: Path
    original_sha256: str
    prior_state: InstallState | None
    intended_state: InstallState | None
    transaction_id: str
    started_at: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "transaction_id": self.transaction_id,
            "started_at": self.started_at,
            "action": self.action,
            "phase": self.phase,
            "target": str(self.target),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "original_backup": str(self.original_backup),
            "original_sha256": self.original_sha256,
            "prior_state": (self.prior_state.to_mapping()
                            if self.prior_state is not None else None),
            "intended_state": (self.intended_state.to_mapping()
                               if self.intended_state is not None else None),
        }

    def with_phase(self, phase: Literal[
            "prepared", "target_replaced", "state_committed"]) -> "TransactionJournal":
        return TransactionJournal(
            self.action, phase, self.target, self.before_sha256,
            self.after_sha256, self.original_backup, self.original_sha256,
            self.prior_state, self.intended_state, self.transaction_id,
            self.started_at)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TransactionJournal":
        if data.get("schema") != 2:
            raise TransactionError("Unsupported transaction journal schema.")
        action = str(data.get("action", ""))
        phase = str(data.get("phase", ""))
        if action not in {"install", "update", "restore"}:
            raise TransactionError("Invalid transaction journal action.")
        if phase not in {"prepared", "target_replaced", "state_committed"}:
            raise TransactionError("Invalid transaction journal phase.")
        hashes = (str(data.get("before_sha256", "")),
                  str(data.get("after_sha256", "")),
                  str(data.get("original_sha256", "")))
        if any(len(value) != 64 or any(char not in "0123456789ABCDEFabcdef"
                                       for char in value) for value in hashes):
            raise TransactionError("Transaction journal contains an invalid SHA-256.")
        try:
            prior_raw = data.get("prior_state")
            intended_raw = data.get("intended_state")
            prior = (InstallState.from_mapping(prior_raw)
                     if isinstance(prior_raw, dict) else None)
            intended = (InstallState.from_mapping(intended_raw)
                        if isinstance(intended_raw, dict) else None)
        except ManagerDataError as error:
            raise TransactionError("Transaction journal contains invalid install state.") from error
        if action == "restore" and intended is not None:
            raise TransactionError("Restore journal must not contain an intended install state.")
        if action != "restore" and intended is None:
            raise TransactionError("Install journal is missing its intended state.")
        required_text = ("target", "original_backup", "transaction_id", "started_at")
        if any(not str(data.get(key, "")).strip() for key in required_text):
            raise TransactionError("Transaction journal is incomplete.")
        return cls(
            action, phase, Path(str(data["target"])), hashes[0].upper(),
            hashes[1].upper(), Path(str(data["original_backup"])),
            hashes[2].upper(), prior, intended,
            str(data["transaction_id"]), str(data["started_at"]))


class ManagerTransaction:
    """Owns the narrow mutation boundary for a single manager state file."""

    def __init__(self, state_path: Path, backup_root: Path | None = None,
                 process_probe: Callable[[], Iterable[str]] = running_game_processes,
                 elevated_copy: Callable[[Path, Path, str, str], None] | None = None) -> None:
        self.state_path = Path(state_path).expanduser().resolve()
        self.backup_root = (Path(backup_root).expanduser().resolve()
                            if backup_root is not None
                            else self.state_path.parent / "backups")
        self.journal_path = self.state_path.with_name(
            self.state_path.name + ".transaction.json")
        self.lock_path = self.state_path.with_name(
            self.state_path.name + ".transaction.lock")
        self._process_probe = process_probe
        self._elevated_copy = (
            elevated_copy if elevated_copy is not None else
            lambda source, target, expected, before:
                request_elevated_atomic_copy(
                    self.state_path.parent, source, target, expected, before))

    @contextmanager
    def _exclusive_transaction(self):
        """Hold one OS-released lock across journal, target, and state writes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        locked = False
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise TransactionError(
                    "Another UI Mod Manager transaction is already active.") from error
            locked = True
            yield
        finally:
            if locked:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()

    def _require_no_journal(self) -> None:
        if self.journal_path.exists():
            raise TransactionError(
                "A previous install or restore did not finish. No overwrite is safe until its "
                f"journal is investigated: {self.journal_path}")

    def has_interrupted_operation(self) -> bool:
        return self.journal_path.is_file()

    def _require_processes_closed(self) -> None:
        try:
            result = self._process_probe()
            if isinstance(result, ProcessProbeResult) and not result.verified:
                raise TransactionError(
                    "Cannot verify that Star Empire is closed: "
                    f"{result.error}")
            names = {str(name).lower() for name in result}
        except TransactionError:
            raise
        except Exception as error:
            raise TransactionError(
                "Cannot verify that Star Empire is closed; no files were changed.") from error
        if names & GAME_PROCESS_NAMES:
            raise TransactionError("Close Star Empire and its launcher before changing files.")

    def _require_unchanged_and_closed(self, inspection: Inspection) -> None:
        self._require_processes_closed()
        if not inspection.client_path.is_file():
            raise TransactionError("Client.exe disappeared after verification; no overwrite is safe.")
        actual = sha256_file(inspection.client_path)
        if actual != inspection.current_sha256:
            raise TransactionError(
                "Client.exe changed after verification; run Verify again before changing files.")
        try:
            actual_version = read_game_version(
                inspection.game_root / "version.txt")
        except ManagerDataError as error:
            raise TransactionError(
                "version.txt changed after verification; run Verify again.") from error
        if actual_version != inspection.version:
            raise TransactionError(
                "version.txt changed after verification; run Verify again before changing files.")

    @staticmethod
    def _require_candidate(candidate: BuiltCandidate,
                           pack: CompatibilityPack) -> Path:
        if not isinstance(candidate, BuiltCandidate):
            raise TransactionError(
                "Install requires a candidate built and audited by this Manager session.")
        try:
            return candidate.verify_for(pack)
        except (CandidateBuildError, OSError) as error:
            raise TransactionError(str(error)) from error

    def _write_journal(self, payload: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.journal_path.with_name(self.journal_path.name + ".pending")
        if pending.exists():
            raise TransactionError(f"Stale manager journal write exists: {pending}")
        try:
            pending.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
            pending.replace(self.journal_path)
        except Exception:
            pending.unlink(missing_ok=True)
            raise

    def _save_journal(self, journal: TransactionJournal) -> None:
        self._write_journal(journal.to_mapping())

    def _load_journal(self) -> TransactionJournal:
        try:
            raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TransactionError(
                f"Cannot read interrupted-operation journal: {self.journal_path}") from error
        if not isinstance(raw, dict):
            raise TransactionError("Transaction journal must contain one JSON object.")
        journal = TransactionJournal.from_mapping(raw)
        if journal.target.name.lower() != "client.exe":
            raise TransactionError("Transaction journal target is not Client.exe.")
        try:
            journal.original_backup.resolve().relative_to(self.backup_root.resolve())
        except ValueError as error:
            raise TransactionError(
                "Transaction journal backup is outside the manager backup folder.") from error
        if (not journal.original_backup.is_file()
                or sha256_file(journal.original_backup) != journal.original_sha256):
            raise TransactionError(
                "Interrupted-operation vanilla backup is missing or has changed.")
        return journal

    @staticmethod
    def _state_matches(actual: InstallState | None,
                       expected: InstallState | None) -> bool:
        if actual is None or expected is None:
            return actual is expected
        return actual.to_mapping() == expected.to_mapping()

    def recover(self) -> RecoveryResult:
        with self._exclusive_transaction():
            return self._recover_locked()

    def _recover_locked(self) -> RecoveryResult:
        """Repair or abandon one interrupted operation using only verified hashes."""
        if not self.journal_path.is_file():
            raise TransactionError("There is no interrupted operation to repair.")
        self._require_processes_closed()
        journal = self._load_journal()
        if not journal.target.is_file():
            raise TransactionError("Interrupted-operation Client.exe is missing.")
        target_hash = sha256_file(journal.target)
        try:
            actual_state = load_install_state(self.state_path)
        except ManagerDataError as error:
            raise TransactionError(
                "Install state is invalid; recovery cannot safely classify it.") from error

        # No replacement took effect. The verified prior state and client are
        # already consistent, so recovery only removes the abandoned journal.
        if (target_hash == journal.before_sha256
                and self._state_matches(actual_state, journal.prior_state)):
            self.journal_path.unlink()
            return RecoveryResult("abandoned", journal.action, target_hash)

        if target_hash != journal.after_sha256:
            raise TransactionError(
                "Client.exe matches neither the before nor after hash; automatic recovery is blocked.")

        if journal.action == "restore":
            if actual_state is not None and not self._state_matches(
                    actual_state, journal.prior_state):
                raise TransactionError(
                    "Install state changed independently; restore recovery is blocked.")
            if actual_state is not None:
                self.state_path.unlink()
        else:
            if not (self._state_matches(actual_state, journal.prior_state)
                    or self._state_matches(actual_state, journal.intended_state)):
                raise TransactionError(
                    "Install state changed independently; install recovery is blocked.")
            if not self._state_matches(actual_state, journal.intended_state):
                if journal.intended_state is None:
                    raise TransactionError("Interrupted install has no intended state.")
                save_install_state(self.state_path, journal.intended_state)

        self._save_journal(journal.with_phase("state_committed"))
        self.journal_path.unlink()
        return RecoveryResult("completed", journal.action, target_hash)

    @staticmethod
    def _replace_pending(pending: Path, destination: Path) -> None:
        pending.replace(destination)

    def _atomic_copy(self, source: Path, destination: Path,
                     expected_sha256: str, *, allow_elevation: bool = False) -> None:
        """Copy within destination volume, verify, then atomically replace it."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        before_sha256 = sha256_file(destination) if allow_elevation else ""
        pending = destination.with_name(
            f".{destination.name}.ui-mod-{uuid.uuid4().hex}.pending")
        try:
            try:
                shutil.copy2(source, pending)
                if sha256_file(pending) != expected_sha256:
                    raise TransactionError(
                        "Copied file failed SHA-256 verification; nothing was replaced.")
                self._replace_pending(pending, destination)
            except PermissionError as error:
                if not allow_elevation:
                    raise
                pending.unlink(missing_ok=True)
                try:
                    self._elevated_copy(
                        source, destination, expected_sha256, before_sha256)
                except ElevationCancelled:
                    raise
                except ElevationError as elevation_error:
                    raise TransactionError(str(elevation_error)) from elevation_error
            if sha256_file(destination) != expected_sha256:
                raise TransactionError("Atomic replacement failed SHA-256 verification.")
        finally:
            pending.unlink(missing_ok=True)

    def _backup_original(self, source: Path, original_sha256: str) -> Path:
        backup = self.backup_root / original_sha256 / "Client.exe"
        if backup.exists():
            if sha256_file(backup) != original_sha256:
                raise TransactionError("Existing original backup has changed; no overwrite is safe.")
            return backup
        self._atomic_copy(source, backup, original_sha256)
        return backup

    def install_or_update(self, inspection: Inspection,
                          candidate: BuiltCandidate) -> TransactionResult:
        with self._exclusive_transaction():
            return self._install_or_update_locked(inspection, candidate)

    def _install_or_update_locked(self, inspection: Inspection,
                                  candidate: BuiltCandidate) -> TransactionResult:
        """Install a verified candidate over a recognised vanilla or modded client."""
        self._require_no_journal()
        self._require_unchanged_and_closed(inspection)
        pack = inspection.pack
        if pack is None:
            raise TransactionError("No authorised update pack is selected.")
        compatibility_install = (
            inspection.status is InstallStatus.UNSUPPORTED_VANILLA
            and candidate.compatibility_test)
        if compatibility_install and (
                inspection.current_sha256 is None
                or candidate.official_sha256 != inspection.current_sha256
                or candidate.observed_version != inspection.version):
            raise TransactionError(
                "Compatibility-test evidence no longer matches the selected game.")
        is_install = (inspection.status is InstallStatus.READY_TO_INSTALL
                      or compatibility_install)
        is_update = inspection.can_update
        if not (is_install or is_update):
            raise TransactionError(f"Install/update is blocked: {inspection.message}")
        source = self._require_candidate(candidate, pack)
        installed_sha256 = (candidate.sha256 if compatibility_install
                            else pack.expected_client_sha256)
        if source == inspection.client_path.resolve():
            raise TransactionError("Candidate Client.exe must be outside the selected game folder.")

        if is_install:
            original_sha256 = inspection.current_sha256
            if original_sha256 is None:
                raise TransactionError("The selected client could not be hashed.")
            backup = self._backup_original(inspection.client_path, original_sha256)
            action: Literal["install", "update"] = "install"
        else:
            if inspection.state is None:
                raise TransactionError("Installed state is missing.")
            original_sha256 = inspection.state.original_sha256
            backup = inspection.state.original_client
            if not backup.is_file() or sha256_file(backup) != original_sha256:
                raise TransactionError("Recorded original backup no longer verifies.")
            action = "update"

        # Backup creation and verification can take long enough for the game,
        # launcher, version, client, or candidate to change.  Re-prove every
        # mutable input immediately before creating the durable journal and
        # replacing the target.
        self._require_unchanged_and_closed(inspection)
        source = self._require_candidate(candidate, pack)

        state = InstallState.new(
            inspection.game_root, backup, original_sha256,
            installed_sha256, pack.pack_id,
            pack_digest=pack.pack_digest, key_id=pack.key_id,
            mod_version=pack.mod_version,
            game_version=(candidate.observed_version
                          if compatibility_install else pack.game_version),
            compatibility_source_game_version=(
                pack.game_version if compatibility_install else ""))
        journal = TransactionJournal(
            action, "prepared", inspection.client_path.resolve(),
            inspection.current_sha256 or original_sha256,
            installed_sha256, backup.resolve(), original_sha256,
            inspection.state, state, uuid.uuid4().hex,
            datetime.now(timezone.utc).isoformat())
        self._save_journal(journal)
        try:
            self._atomic_copy(
                source, inspection.client_path, installed_sha256,
                allow_elevation=True)
            self._save_journal(journal.with_phase("target_replaced"))
            save_install_state(self.state_path, state)
            self._save_journal(journal.with_phase("state_committed"))
        except ElevationCancelled as error:
            if (inspection.client_path.is_file()
                    and sha256_file(inspection.client_path) == journal.before_sha256):
                self.journal_path.unlink(missing_ok=True)
            raise TransactionError(
                "Administrator permission was declined; no files were changed.") from error
        except Exception:
            raise
        else:
            self.journal_path.unlink(missing_ok=True)
        return TransactionResult(action, backup, installed_sha256)

    def restore(self, inspection: Inspection) -> TransactionResult:
        with self._exclusive_transaction():
            return self._restore_locked(inspection)

    def _restore_locked(self, inspection: Inspection) -> TransactionResult:
        """Restore only the exact original backup recorded for this installation."""
        self._require_no_journal()
        self._require_unchanged_and_closed(inspection)
        if not inspection.can_restore or inspection.state is None:
            raise TransactionError(f"Restore is blocked: {inspection.message}")
        state = inspection.state
        if not state.original_client.is_file() or sha256_file(state.original_client) != state.original_sha256:
            raise TransactionError("Recorded original backup no longer verifies.")
        self._require_unchanged_and_closed(inspection)
        journal = TransactionJournal(
            "restore", "prepared", inspection.client_path.resolve(),
            inspection.current_sha256 or state.installed_sha256,
            state.original_sha256, state.original_client.resolve(),
            state.original_sha256, state, None, uuid.uuid4().hex,
            datetime.now(timezone.utc).isoformat())
        self._save_journal(journal)
        try:
            self._atomic_copy(
                state.original_client, inspection.client_path,
                state.original_sha256, allow_elevation=True)
            self._save_journal(journal.with_phase("target_replaced"))
            self.state_path.unlink(missing_ok=True)
            self._save_journal(journal.with_phase("state_committed"))
        except ElevationCancelled as error:
            if (inspection.client_path.is_file()
                    and sha256_file(inspection.client_path) == journal.before_sha256):
                self.journal_path.unlink(missing_ok=True)
            raise TransactionError(
                "Administrator permission was declined; no files were changed.") from error
        except Exception:
            raise
        else:
            self.journal_path.unlink(missing_ok=True)
        return TransactionResult("restore", state.original_client, state.original_sha256)
