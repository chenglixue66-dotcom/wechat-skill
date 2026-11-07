"""Fail-closed sending policy; this module contains no real UI implementation.

Backend tokens must name control incarnations, never reusable selectors. A
backend must recheck the full BoundComposer/BoundSend preconditions immediately
before its object-bound effect; it must not use keyboard/coordinate fallbacks.
Mocks prove this policy, not WeChat accessibility support or atomic UI behavior.
No accepted action is reported as delivery. Journal files contain hashes and
identifiers only. CLI entry points always construct an in-memory MockBackend.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Protocol


class GuardError(RuntimeError):
    """Only a fixed diagnostic code; never include backend exception payloads."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def text_hash(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8", "strict")).hexdigest()
    except (AttributeError, UnicodeError):
        raise GuardError("INVALID_TEXT") from None


@dataclass(frozen=True)
class RecipientRequest:
    operation_id: str
    account_id: str
    recipient_id: str
    recipient_type: str  # "person" or "group", never a display name.
    payload: str


@dataclass(frozen=True)
class Binding:
    account_id: str
    recipient_id: str
    recipient_type: str
    pid: int
    launch_token: str
    bundle_path: str
    window_id: str
    composer_token: str
    send_token: str


@dataclass(frozen=True)
class Observation:
    binding: Binding | None = None
    draft: str | None = None
    read_errors: tuple[str, ...] | None = None
    identity_verified: bool | None = None
    account_matches: int | None = None
    recipient_matches: int | None = None
    composer_matches: int | None = None
    send_matches: int | None = None
    on_screen: bool | None = None
    focused: bool | None = None  # Both intended app and intended window.
    composer_focused: bool | None = None
    window_minimized: bool | None = None
    modal: bool | None = None
    locked: bool | None = None  # Optional observation; never infer False from a missing OS key.
    composer_editable: bool | None = None
    send_enabled: bool | None = None
    press_supported: bool | None = None


@dataclass(frozen=True)
class BoundComposer:
    binding: Binding
    expected_draft_hash: str


@dataclass(frozen=True)
class BoundSend:
    binding: Binding
    expected_draft_hash: str
    operation_id: str
    expires_at: float


class PressOutcome(Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class Backend(Protocol):
    def read_observation(self) -> Observation: ...
    def set_draft(self, bound_composer_token: BoundComposer, text: str) -> None: ...
    def press_send(self, bound_send_token: BoundSend) -> PressOutcome: ...


@dataclass(frozen=True)
class Prepared:
    operation_id: str
    binding: Binding
    payload_hash: str
    issued_at: float
    expires_at: float
    nonce: str


@dataclass(frozen=True)
class CommitResult:
    state: str  # ATTEMPTED, REJECTED, UNKNOWN; never DELIVERED.
    delivered: bool = False


def _no_links(path: Path, missing_ok=False):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise GuardError("JOURNAL_PATH_MISSING") from None
        if stat.S_ISLNK(info.st_mode):
            raise GuardError("JOURNAL_SYMLINK_REJECTED")


def _private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise GuardError("JOURNAL_FILE_UNSAFE")


class _Journal:
    def __init__(self, path: Path):
        path = Path(path).expanduser()
        if ".." in path.parts:
            raise GuardError("JOURNAL_PATH_INVALID")
        self.path = path.absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _parent(self):
        _no_links(self.path.parent, missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _no_links(self.path.parent)
        info = self.path.parent.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise GuardError("JOURNAL_DIRECTORY_UNSAFE")

    def _load(self):
        _no_links(self.path, missing_ok=True)
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {"version": 1, "operations": {}}
        with os.fdopen(fd, "rb") as stream:
            _private_file(stream.fileno())
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise GuardError("JOURNAL_INVALID")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise GuardError("JOURNAL_INVALID") from None
        if (not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1
                or not isinstance(document.get("operations"), dict)
                or any(not isinstance(v, dict) for v in document["operations"].values())):
            raise GuardError("JOURNAL_INVALID")
        states = {"PREPARING", "PREPARED", "PREPARE_FAILED", "COMMIT_STARTED", "ABORTED",
                  "ABORTED_PRECOMMIT", "ATTEMPTED", "REJECTED", "UNKNOWN", "CLEANED", "CLEANUP_BLOCKED"}
        for operation_id, entry in document["operations"].items():
            if (not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id)
                    or not isinstance(entry.get("state"), str) or entry["state"] not in states
                    or not isinstance(entry.get("binding"), dict)
                    or set(entry["binding"]) != set(Binding.__dataclass_fields__)
                    or not isinstance(entry.get("payload_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["payload_hash"])
                    or not isinstance(entry.get("nonce"), str) or not re.fullmatch(r"[0-9a-f]{32}", entry["nonce"])
                    or set(entry) - {"state", "binding", "payload_hash", "nonce", "draft_cleaned"}):
                raise GuardError("JOURNAL_INVALID")
        return document

    @contextmanager
    def transaction(self):
        fd = None
        locked = False
        try:
            self._parent()
            _no_links(self.lock_path, missing_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            _private_file(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                raise GuardError("BUSY") from None
            yield self._load()
        except OSError:
            raise GuardError("JOURNAL_IO_FAILED") from None
        finally:
            if fd is not None:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def write(self, document):
        temporary = None
        try:
            self._parent()
            _no_links(self.path, missing_ok=True)
            if self.path.exists():
                with os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as check:
                    _private_file(check.fileno())
            fd, name = tempfile.mkstemp(prefix=".send-journal-", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(document, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            _no_links(self.path, missing_ok=True)
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise GuardError("JOURNAL_IO_FAILED") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def validate_observation(observation, *, expected_bundle_path="/Applications/WeChat.app",
                         request=None, binding=None, draft_hash=None, ready=False):
    """Check edit safety, plus executable Send only for ready=True stages.

    A disabled empty composer may expose a known empty action list. Editing or
    owned cleanup may accept that explicit False, but never an unknown value.
    Prepare readback and every commit still require enabled Send and AXPress.
    """
    if not isinstance(observation, Observation):
        raise GuardError("OBSERVATION_UNKNOWN")
    if not isinstance(observation.read_errors, tuple) or observation.read_errors:
        raise GuardError("OBSERVATION_READ_ERROR")
    observed = observation.binding
    if not isinstance(observed, Binding):
        raise GuardError("IDENTITY_UNKNOWN")
    if (type(observed.pid) is not int or observed.pid <= 0
            or any(not isinstance(value, str) or not value for key, value in asdict(observed).items() if key != "pid")
            or observed.recipient_type not in {"person", "group"}
            or observed.bundle_path != expected_bundle_path):
        raise GuardError("IDENTITY_UNKNOWN")
    for count in (observation.account_matches, observation.recipient_matches,
                  observation.composer_matches, observation.send_matches):
        if type(count) is not int or count != 1:
            raise GuardError("IDENTITY_AMBIGUOUS")
    # macOS does not expose a supported, universally present lock-state getter
    # to an ordinary foreground automation process. Keep unknown as unknown;
    # explicit locked=True always stops. This policy is foreground-only: the
    # independently rechecked app/window/composer focus below remains required.
    if observation.locked is not None and type(observation.locked) is not bool:
        raise GuardError("LOCK_STATE_INVALID")
    if observation.locked is True:
        raise GuardError("UI_STATE_UNSAFE")
    expected_flags = {"identity_verified": True, "on_screen": True, "focused": True,
                      "composer_focused": True, "window_minimized": False, "modal": False,
                      "composer_editable": True}
    if any(getattr(observation, name) is not expected for name, expected in expected_flags.items()):
        raise GuardError("UI_STATE_UNSAFE")
    if type(observation.send_enabled) is not bool or (ready and not observation.send_enabled):
        raise GuardError("SEND_CONTROL_UNAVAILABLE")
    if type(observation.press_supported) is not bool:
        raise GuardError("SEND_ACTION_UNKNOWN")
    if ready and not observation.press_supported:
        raise GuardError("SEND_ACTION_UNAVAILABLE")
    if binding is not None and observed != binding:
        raise GuardError("BINDING_CHANGED")
    if request is not None and (observed.account_id, observed.recipient_id, observed.recipient_type) != (
            request.account_id, request.recipient_id, request.recipient_type):
        raise GuardError("RECIPIENT_MISMATCH")
    if not isinstance(observation.draft, str):
        raise GuardError("DRAFT_UNKNOWN")
    if draft_hash is not None and text_hash(observation.draft) != draft_hash:
        raise GuardError("DRAFT_CHANGED")
    return observed

class SendGuard:
    def __init__(self, backend: Backend, journal_path: Path, *, ttl_seconds=5.0,
                 clock=time.monotonic, expected_bundle_path="/Applications/WeChat.app"):
        if not 0 < ttl_seconds <= 30:
            raise GuardError("INVALID_TTL")
        self.backend = backend
        self.journal = _Journal(journal_path)
        self.ttl = float(ttl_seconds)
        self.clock = clock
        self.expected_bundle_path = expected_bundle_path
        self._mutex = threading.RLock()
        self._prepared: dict[str, Prepared] = {}
        self._states: dict[str, str] = {}

    def _read(self):
        try:
            observation = self.backend.read_observation()
        except Exception:
            raise GuardError("OBSERVATION_FAILED") from None
        if not isinstance(observation, Observation):
            raise GuardError("OBSERVATION_UNKNOWN")
        return observation

    def _validate(self, observation, *, request=None, binding=None, draft_hash=None, ready=False):
        return validate_observation(observation, expected_bundle_path=self.expected_bundle_path,
                                    request=request, binding=binding, draft_hash=draft_hash, ready=ready)
    def _alive(self, prepared):
        now = self.clock()
        if not prepared.issued_at <= now < prepared.expires_at:
            raise GuardError("PREPARED_EXPIRED")

    def _owned(self, prepared):
        if not isinstance(prepared, Prepared) or self._prepared.get(prepared.nonce) is not prepared:
            raise GuardError("PREPARED_NOT_ISSUED_HERE")

    def _cleanup_owned(self, binding, payload_hash):
        try:
            self._validate(self._read(), binding=binding, draft_hash=payload_hash)
            self.backend.set_draft(BoundComposer(binding, payload_hash), "")
            self._validate(self._read(), binding=binding, draft_hash=text_hash(""))
            return True
        except Exception:
            return False

    def prepare(self, request: RecipientRequest) -> Prepared:
        if (not isinstance(request, RecipientRequest)
                or not isinstance(request.operation_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request.operation_id)
                or not isinstance(request.recipient_type, str)
                or request.recipient_type not in {"person", "group"}
                or any(not isinstance(v, str) or not v for v in (
                    request.account_id, request.recipient_id, request.payload))):
            raise GuardError("INVALID_REQUEST")
        payload_hash = text_hash(request.payload)
        with self._mutex, self.journal.transaction() as document:
            if request.operation_id in document["operations"]:
                raise GuardError("OPERATION_ALREADY_EXISTS")
            issued_at = self.clock()
            binding = self._validate(self._read(), request=request, draft_hash=text_hash(""))
            prepared = Prepared(request.operation_id, binding, payload_hash, issued_at,
                                issued_at + self.ttl, secrets.token_hex(16))
            self._alive(prepared)
            entry = {"state": "PREPARING", "binding": asdict(binding), "payload_hash": payload_hash,
                     "nonce": prepared.nonce}
            document["operations"][request.operation_id] = entry
            self.journal.write(document)
            write_returned = False
            try:
                self.backend.set_draft(BoundComposer(binding, text_hash("")), request.payload)
                write_returned = True
                self._validate(self._read(), binding=binding, draft_hash=payload_hash, ready=True)
                self._alive(prepared)
                entry["state"] = "PREPARED"
                self.journal.write(document)
            except BaseException as error:
                # A timed-out/failed write may not have happened. Matching text
                # alone cannot prove that a new user draft belongs to us.
                cleaned = write_returned and self._cleanup_owned(binding, payload_hash)
                entry.update(state="PREPARE_FAILED", draft_cleaned=cleaned)
                self.journal.write(document)
                if not isinstance(error, Exception):
                    raise
                raise error if isinstance(error, GuardError) else GuardError("DRAFT_WRITE_UNCERTAIN") from None
            self._prepared[prepared.nonce] = prepared
            self._states[prepared.nonce] = "PREPARED"
            return prepared

    def commit(self, prepared: Prepared) -> CommitResult:
        with self._mutex:
            self._owned(prepared)
            if self._states.get(prepared.nonce) != "PREPARED":
                raise GuardError("OPERATION_NOT_COMMITTABLE")
            with self.journal.transaction() as document:
                entry = document["operations"].get(prepared.operation_id)
                if (not entry or entry.get("state") != "PREPARED" or entry.get("nonce") != prepared.nonce
                        or entry.get("binding") != asdict(prepared.binding)
                        or entry.get("payload_hash") != prepared.payload_hash):
                    raise GuardError("OPERATION_NOT_COMMITTABLE")
                try:
                    self._alive(prepared)
                    self._validate(self._read(), binding=prepared.binding,
                                   draft_hash=prepared.payload_hash, ready=True)
                    self._alive(prepared)
                except Exception as error:
                    self._states[prepared.nonce] = "ABORTED"
                    entry["state"] = "ABORTED"
                    self.journal.write(document)
                    raise error if isinstance(error, GuardError) else GuardError("COMMIT_CHECK_FAILED") from None
                # Durable intent precedes the irreversible backend call. Any
                # record left here by process death makes this operation spent.
                entry["state"] = "COMMIT_STARTED"
                self._states[prepared.nonce] = "COMMIT_STARTED"
                self.journal.write(document)
                try:
                    self._alive(prepared)
                    self._validate(self._read(), binding=prepared.binding,
                                   draft_hash=prepared.payload_hash, ready=True)
                    self._alive(prepared)
                except Exception as error:
                    entry["state"] = "ABORTED_PRECOMMIT"
                    self._states[prepared.nonce] = "ABORTED_PRECOMMIT"
                    self.journal.write(document)
                    raise error if isinstance(error, GuardError) else GuardError("COMMIT_CHECK_FAILED") from None
                try:
                    outcome = self.backend.press_send(BoundSend(
                        prepared.binding, prepared.payload_hash, prepared.operation_id, prepared.expires_at))
                    state = {PressOutcome.ACCEPTED: "ATTEMPTED", PressOutcome.REJECTED: "REJECTED"}.get(outcome, "UNKNOWN")
                except BaseException as error:
                    entry["state"] = "UNKNOWN"
                    self._states[prepared.nonce] = "UNKNOWN"
                    try:
                        self.journal.write(document)
                    except GuardError:
                        pass  # Durable COMMIT_STARTED still prevents a retry.
                    if not isinstance(error, Exception):
                        raise
                    return CommitResult("UNKNOWN")
                self._states[prepared.nonce] = state
                entry["state"] = state
                try:
                    self.journal.write(document)
                except GuardError:
                    self._states[prepared.nonce] = "UNKNOWN"
                    return CommitResult("UNKNOWN")
                return CommitResult(state)

    def cleanup(self, prepared: Prepared) -> bool:
        with self._mutex:
            self._owned(prepared)
            if self._states.get(prepared.nonce) not in {"PREPARED", "ABORTED", "ABORTED_PRECOMMIT", "CLEANUP_BLOCKED"}:
                raise GuardError("CLEANUP_AFTER_SUBMISSION_FORBIDDEN")
            with self.journal.transaction() as document:
                entry = document["operations"].get(prepared.operation_id)
                if (not entry or entry.get("nonce") != prepared.nonce
                        or entry.get("binding") != asdict(prepared.binding)
                        or entry.get("payload_hash") != prepared.payload_hash
                        or entry.get("state") not in {"PREPARED", "ABORTED", "ABORTED_PRECOMMIT", "CLEANUP_BLOCKED"}):
                    raise GuardError("CLEANUP_AFTER_SUBMISSION_FORBIDDEN")
                self._states[prepared.nonce] = "CLEANUP_BLOCKED"
                cleaned = self._cleanup_owned(prepared.binding, prepared.payload_hash)
                state = "CLEANED" if cleaned else "CLEANUP_BLOCKED"
                self._states[prepared.nonce] = state
                entry["state"] = state
                self.journal.write(document)
                return cleaned

    def operation_state(self, operation_id: str) -> str | None:
        with self._mutex, self.journal.transaction() as document:
            entry = document["operations"].get(operation_id)
            if entry is None:
                return None
            state = entry.get("state")
            return "UNKNOWN" if state in {"COMMIT_STARTED", "UNKNOWN"} else state


def mock_observation() -> Observation:
    return Observation(
        Binding("mock_account", "mock_person", "person", 100, "mock_launch",
                "/Applications/WeChat.app", "mock_window", "mock_composer", "mock_send"),
        "", (), True, 1, 1, 1, 1, True, True, True, False, False, False, True, False, True)


class MockBackend:
    """Memory-only backend. No system/app/UI actions, including in CLI demos."""
    def __init__(self, observation=None, *, clock=time.monotonic):
        self.observation = observation or mock_observation()
        self.events = []
        self.presses = 0
        self.clock = clock

    def read_observation(self):
        self.events.append("read")
        return self.observation

    def set_draft(self, bound_composer_token, text):
        validate_observation(self.observation, binding=bound_composer_token.binding,
                             draft_hash=bound_composer_token.expected_draft_hash)
        self.events.append("set_draft")
        self.observation = replace(self.observation, draft=text, send_enabled=bool(text))

    def press_send(self, bound_send_token):
        if self.clock() >= bound_send_token.expires_at:
            raise GuardError("MOCK_PREPARED_EXPIRED")
        validate_observation(self.observation, binding=bound_send_token.binding,
                             draft_hash=bound_send_token.expected_draft_hash, ready=True)
        self.events.append("press_send")
        self.presses += 1
        self.observation = replace(self.observation, draft="", send_enabled=False)
        return PressOutcome.ACCEPTED


def main(argv=None):
    parser = argparse.ArgumentParser(description="Memory-only send guard demonstration; no real backend is available.")
    parser.add_argument("mode", choices=("self-test", "mock"))
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="wechat-guard-mock-") as directory:
        backend = MockBackend()
        guard = SendGuard(backend, Path(directory).resolve() / "operations.json")
        request = RecipientRequest("mock_operation", "mock_account", "mock_person", "person", "mock payload")
        prepared = guard.prepare(request)
        if args.mode == "self-test":
            assert guard.cleanup(prepared) and backend.presses == 0
            state = "CLEANED"
        else:
            result = guard.commit(prepared)
            assert result.state == "ATTEMPTED" and result.delivered is False
            state = result.state
        print(json.dumps({"ok": True, "mock_only": True, "state": state,
                          "mock_presses": backend.presses, "delivered": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
