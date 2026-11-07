#!/usr/bin/env python3
"""Private, one-attempt receipt sessions; no UI actions or message sending.

Wraps the existing independent snapshots and strict target verifier. Submission
is journaled BEFORE the caller performs an effect. An uncertain/failed submit
can only be verified, never reauthorized by this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

from snapshot_recipient import DEFAULT_CONFIG, checked_file, create_snapshot, private_directory
import verify_sent


DEFAULT_STATE_ROOT = Path.home() / ".config/wechat-skill/send-operations"
_OPERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9_@.\-]{1,512}\Z")


class ReceiptError(RuntimeError):
    @property
    def code(self):
        return str(self)


@dataclass(frozen=True)
class SubmissionTicket:
    operation_id: str
    committed_at: float


class ReceiptSession:
    """Fixed identity and body hash with a durable, exclusive submit gate."""

    def __init__(self, operation_id, recipient_id, self_wxid, payload_hash, *,
                 state_root=DEFAULT_STATE_ROOT, snapshotter=None):
        if (not isinstance(operation_id, str) or not _OPERATION.fullmatch(operation_id)
                or not isinstance(payload_hash, str) or not _HASH.fullmatch(payload_hash)):
            raise ReceiptError("INVALID_OPERATION_OR_PAYLOAD_HASH")
        if any(not isinstance(value, str) or not _IDENTITY.fullmatch(value)
               for value in (recipient_id, self_wxid)):
            raise ReceiptError("INVALID_RECEIPT_IDENTITY")
        self.expected = {"operation_id": operation_id, "recipient_id": recipient_id,
                         "self_wxid": self_wxid, "payload_hash": payload_hash}
        root = private_directory(Path(state_root))
        self.directory = private_directory(root / hashlib.sha256(operation_id.encode()).hexdigest())
        self.state_file = self.directory / "operation.json"
        self.baseline_file = self.directory / "baseline.json"
        self.snapshot_root = self.directory / "snapshots"
        self.snapshotter = snapshotter or create_snapshot

    @contextmanager
    def _lock(self):
        path = self.directory / "operation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ReceiptError("PRIVATE_OPERATION_LOCK_REQUIRED")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReceiptError("OPERATION_IN_USE") from None
            yield
        finally:
            os.close(fd)

    def _load(self, *, missing_ok=False):
        if not self.state_file.exists():
            if missing_ok:
                return None
            raise ReceiptError("OPERATION_NOT_PREPARED")
        try:
            document = json.loads(checked_file(self.state_file).read_text())
            if (document.get("version") != 1
                    or any(document.get(key) != value for key, value in self.expected.items())):
                raise ReceiptError("OPERATION_IDENTITY_CONFLICT")
            return document
        except ReceiptError:
            raise
        except Exception:
            raise ReceiptError("OPERATION_STATE_UNAVAILABLE") from None

    def _save(self, document):
        if self.state_file.exists():
            checked_file(self.state_file)
        fd, name = tempfile.mkstemp(prefix=".operation-", dir=self.directory)
        path = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.state_file)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            path.unlink(missing_ok=True)

    def _snapshot(self, config_file):
        return self.snapshotter(config_file=config_file, output_root=self.snapshot_root)

    def _discard_snapshot(self, snapshot):
        # Delete only generations created inside this operation's own output.
        # Baseline lineage/hash state is sufficient for future fresh snapshots.
        try:
            generation = Path(snapshot["manifest"]).parent
            if (generation.parent == self.snapshot_root and generation.name.startswith("receipt-")
                    and generation.resolve(strict=True) == generation and not generation.is_symlink()):
                shutil.rmtree(generation)
        except (KeyError, OSError, ValueError):
            pass

    def _create_baseline(self, snapshot, destination):
        return verify_sent.create_baseline(snapshot["databases"], destination,
            self.expected["recipient_id"], self.expected["self_wxid"],
            self.expected["payload_hash"], self.expected["operation_id"],
            snapshot_id=snapshot["snapshot_id"], manifest=snapshot["manifest"])

    def _create_or_recover_baseline(self, snapshot, *, recover_allowed):
        """Recover only a baseline stranded before any submission intent.

        A crash/disk error may occur after the verifier atomically writes its
        baseline but before this coordinator saves PREPARED. Recreate through
        the strict verifier, then replace the stranded baseline only while both
        the operation lock (held by prepare) and its verifier lock are held.
        Account/recipient/body and encrypted-source lineage must remain exact.
        Submitted, verified, conflicting or malformed states are never reset.
        """
        if not self.baseline_file.exists() or not recover_allowed:
            return self._create_baseline(snapshot, self.baseline_file)
        recovery = Path(tempfile.mkdtemp(prefix=".baseline-recovery-", dir=self.directory))
        recovery.chmod(0o700)
        candidate = recovery / "baseline.json"
        try:
            created = self._create_baseline(snapshot, candidate)
            if created.get("status") != "baseline_created":
                raise ReceiptError("BASELINE_RECOVERY_UNAVAILABLE")
            replacement = verify_sent._load_state(candidate)
            with verify_sent._locked(self.baseline_file):
                stranded = verify_sent._load_state(self.baseline_file)
                if (any(stranded.get(key) != value for key, value in self.expected.items())
                        or type(stranded.get("version")) is not int
                        or type(stranded.get("message_type")) is not int
                        or stranded["message_type"] != 1
                        or stranded.get("lineage_mode") != "manifest"
                        or stranded.get("committed_at") is not None
                        or type(stranded.get("verification_attempts")) is not int
                        or stranded["verification_attempts"] != 0
                        or stranded.get("verified_result") is not None
                        or stranded.get("last_result") is not None
                        or type(stranded.get("created_at")) not in {int, float}
                        or not math.isfinite(stranded["created_at"])):
                    raise ReceiptError("BASELINE_RECOVERY_CONFLICT")
                previous_sources = {verify_sent._source_key(part["lineage"])
                                    for part in stranded["databases"]}
                current_sources = {verify_sent._source_key(part["lineage"])
                                   for part in replacement["databases"]}
                if (not previous_sources or previous_sources != current_sources
                        or len(previous_sources) != len(stranded["databases"])
                        or len(current_sources) != len(replacement["databases"])):
                    raise ReceiptError("BASELINE_RECOVERY_SOURCE_CHANGED")
                verify_sent._save_state(self.baseline_file, replacement)
            return {**created, "recovered": True}
        finally:
            shutil.rmtree(recovery)

    def prepare(self, config_file=DEFAULT_CONFIG):
        """Read a new baseline once. A repeat never permits another UI attempt."""
        with self._lock():
            state = self._load(missing_ok=True)
            if state is not None and state["phase"] not in {"BASELINE_FAILED", "PREPARING"}:
                return {"status": "operation_exists", "operation_id": self.expected["operation_id"],
                        "phase": state["phase"], "retry_allowed": False, "send_allowed": False}
            recover_allowed = state is not None
            state = {"version": 1, **self.expected, "phase": "PREPARING",
                     "created_at": state["created_at"] if state else time.time(),
                     "committed_at": None, "verification_attempts": 0}
            self._save(state)
            snapshot = None
            try:
                snapshot = self._snapshot(config_file)
                result = self._create_or_recover_baseline(snapshot, recover_allowed=recover_allowed)
                if result.get("status") != "baseline_created":
                    raise ReceiptError("BASELINE_NOT_CREATED")
                state.update(phase="PREPARED", baseline_at=result["baseline_at"],
                             baseline_result=result)
                self._save(state)
                return result
            except Exception:
                state["phase"] = "BASELINE_FAILED"
                self._save(state)
                raise ReceiptError("BASELINE_PREPARATION_FAILED") from None
            finally:
                if snapshot is not None:
                    self._discard_snapshot(snapshot)

    @contextmanager
    def submission(self):
        """Journal one submit attempt and hold its cross-process lock.

        Call the UI effect once inside this context. Exceptions or crashes leave
        a non-retryable COMMIT_STARTED/SUBMIT_UNCERTAIN state. Do not call verify
        inside the context; it needs the same operation lock after exit.
        """
        with self._lock():
            state = self._load()
            if state.get("phase") != "PREPARED" or state.get("committed_at") is not None:
                raise ReceiptError("SUBMISSION_ALREADY_ATTEMPTED_OR_NOT_PREPARED")
            committed_at = time.time()
            if committed_at < state["baseline_at"]:
                raise ReceiptError("CLOCK_BEFORE_BASELINE")
            state.update(phase="COMMIT_STARTED", committed_at=committed_at)
            self._save(state)
            try:
                yield SubmissionTicket(self.expected["operation_id"], committed_at)
            except BaseException:
                state["phase"] = "SUBMIT_UNCERTAIN"
                self._save(state)
                raise
            else:
                state["phase"] = "SUBMIT_ATTEMPTED"
                self._save(state)

    def verify(self, config_file=DEFAULT_CONFIG):
        """Verify fresh evidence once, then return the durable historical proof.

        A later sampling failure cannot revoke an already verified local send.
        The cache remains bound to this exact operation/account/recipient/body
        through _load(); it does not claim a new observation or delivery.
        """
        with self._lock():
            state = self._load()
            if state.get("phase") not in {"COMMIT_STARTED", "SUBMIT_UNCERTAIN", "SUBMIT_ATTEMPTED", "UNCERTAIN", "VERIFIED"}:
                raise ReceiptError("SUBMISSION_NOT_RECORDED")
            if state["phase"] == "VERIFIED":
                # last_result supports sessions verified before the dedicated
                # immutable success field was introduced.
                proved = state.get("verified_result") or state.get("last_result")
                if (not isinstance(proved, dict) or proved.get("status") != "verified"
                        or proved.get("operation_id") != self.expected["operation_id"]
                        or proved.get("committed_at") != state.get("committed_at")
                        or proved.get("matching_new_rows") != 1
                        or not isinstance(proved.get("records"), list)
                        or len(proved["records"]) != 1):
                    raise ReceiptError("VERIFIED_RECEIPT_UNAVAILABLE")
                return {**proved, "cached": True, "retry_allowed": False,
                        "delivery_confirmed": False}
            snapshot = None
            try:
                snapshot = self._snapshot(config_file)
                result = verify_sent.verify(snapshot["databases"], self.baseline_file,
                    self.expected["recipient_id"], self.expected["operation_id"],
                    state["committed_at"], snapshot_id=snapshot["snapshot_id"],
                    manifest=snapshot["manifest"])
            except Exception:
                result = {"status": "uncertain", "operation_id": self.expected["operation_id"],
                          "reason": "RECEIPT_SNAPSHOT_FAILED", "retry_allowed": False}
            finally:
                if snapshot is not None:
                    self._discard_snapshot(snapshot)
            state["verification_attempts"] += 1
            state["last_result"] = result
            state["phase"] = "VERIFIED" if result.get("status") == "verified" else "UNCERTAIN"
            if state["phase"] == "VERIFIED":
                state["verified_result"] = result
            self._save(state)
            return result

    def public_status(self):
        with self._lock():
            state = self._load()
            return {"operation_id": self.expected["operation_id"], "phase": state["phase"],
                    "committed_at": state.get("committed_at"), "retry_allowed": False,
                    "verification_attempts": state["verification_attempts"],
                    "recipient_hash": hashlib.sha256(self.expected["recipient_id"].encode()).hexdigest(),
                    "payload_hash": self.expected["payload_hash"],
                    "last_result": state.get("last_result")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "mark-submit", "verify", "status"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--recipient-id", required=True)
    parser.add_argument("--self-wxid", required=True)
    parser.add_argument("--payload-hash", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)
    try:
        session = ReceiptSession(args.operation_id, args.recipient_id, args.self_wxid,
                                 args.payload_hash, state_root=args.state_root)
        if args.command == "prepare":
            result = session.prepare(args.config)
        elif args.command == "mark-submit":
            with session.submission() as ticket:
                result = {"status": "submit_recorded", "operation_id": ticket.operation_id,
                          "committed_at": ticket.committed_at, "retry_allowed": False}
        elif args.command == "verify":
            result = session.verify(args.config)
        else:
            result = session.public_status()
    except ReceiptError as error:
        result = {"status": "blocked", "code": error.code, "retry_allowed": False}
    except Exception:
        result = {"status": "blocked", "code": "RECEIPT_SESSION_FAILED", "retry_allowed": False}
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    success = result.get("status") in {"baseline_created", "submit_recorded", "verified"}
    return 0 if success or (args.command == "status" and "phase" in result) else 2


if __name__ == "__main__":
    raise SystemExit(main())
