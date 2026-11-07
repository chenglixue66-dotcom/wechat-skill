"""Fixed local AX-bridge transport and fail-closed send_guard adapter.

Raw draft/identity data stays inside the local pipes and process memory. The
CLI prints only hashes, flags and fixed error codes. Production has no fixture,
binary-path or force flag. Draft writes require explicit library opt-in plus
fresh complete GUI identity evidence. Live submission additionally requires an
explicit library capability; the default probe and diagnostic CLI never send.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields
import json
import math
import os
from pathlib import Path
import select
import re
import stat
import subprocess
import threading
import time
from typing import Protocol
import uuid

from send_guard import (Binding, BoundComposer, BoundSend, GuardError, Observation,
                        PressOutcome, mock_observation, text_hash, validate_observation)

_BRIDGE = Path(__file__).absolute().with_name("native_ax_bridge")
_MAX_LINE = 1_048_576
_SAFE_CODES = {"DRAFT_WRITES_DISABLED", "LIVE_SEND_DISABLED", "INVALID_DRAFT_ARGUMENTS",
               "DRAFT_BINDING_OR_STATE_CHANGED", "DRAFT_WRITE_UNCERTAIN", "APP_INSTANCE_UNAVAILABLE",
               "OFFICIAL_SIGNATURE_NOT_VERIFIED", "OFFICIAL_TEAM_NOT_VERIFIED", "NATIVE_OPERATION_FAILED",
               "IPC_ENCODING_FAILED", "INVALID_JSONL_REQUEST", "INVALID_REQUEST", "UNSUPPORTED_OPERATION"}

_SAFE_CODES.update({'SEARCH_FOCUS_FAILED', 'PROFILE_MESSAGE_ACTION_UNAVAILABLE', 'COMPOSER_FOCUS_FAILED', 'RECIPIENT_GUI_MISMATCH', 'APP_INSTANCE_CHANGED', 'AX_PERMISSION_MISSING', 'DRAFT_BINDING_OR_STATE_CHANGED', 'NAVIGATION_ACTION_UNAVAILABLE', 'SEARCH_WRITE_FAILED', 'PROFILE_SCHEMA_UNSUPPORTED', 'NAVIGATION_TIMEOUT', 'APP_INSTANCE_UNAVAILABLE', 'OFFICIAL_TEAM_NOT_VERIFIED', 'PROFILE_FOCUS_UNKNOWN', 'LIVE_SEND_DISABLED', 'INVALID_NAVIGATION_ARGUMENTS', 'ACCOUNT_GUI_MISMATCH', 'DRAFT_WRITE_UNCERTAIN', 'ACCOUNT_WINDOW_CANNOT_CLOSE', 'NAVIGATION_ACTION_FAILED', 'RECIPIENT_PROFILE_TRIGGER_UNAVAILABLE', 'CHAT_BINDING_UNAVAILABLE', 'OFFICIAL_SIGNATURE_NOT_VERIFIED', 'ACCOUNT_MENU_UNAVAILABLE', 'UNSUPPORTED_OPERATION', 'DRAFT_WRITES_DISABLED', 'INVALID_REQUEST', 'AX_WINDOWS_UNREADABLE', 'NAVIGATION_TREE_UNREADABLE', 'PROFILE_PROCESS_CHANGED', 'INVALID_DRAFT_ARGUMENTS', 'PROFILE_SCOPE_MISSING_OR_AMBIGUOUS'})

_SAFE_CODES.update({"INVALID_SEND_ARGUMENTS", "SEND_ALREADY_ATTEMPTED", "SEND_BINDING_OR_STATE_CHANGED", "SETTINGS_WINDOW_UNAVAILABLE", "ACCOUNT_FIELD_UNAVAILABLE", "SEARCH_CONTROL_UNAVAILABLE", "SEARCH_RESULT_UNAVAILABLE_OR_AMBIGUOUS", "RECIPIENT_PROFILE_UNAVAILABLE", "COMPOSER_OR_HEADER_UNAVAILABLE", "MAIN_CHAT_WINDOW_UNAVAILABLE", "NAVIGATION_ACTION_DISABLED", "OFFICIAL_RUNNING_CODE_UNAVAILABLE", "OFFICIAL_REQUIREMENT_UNAVAILABLE"})

@dataclass(frozen=True)
class RecipientProfileEvidence:
    """Exact GUI field only; neither current-chat nor logged-in-account proof."""
    raw_weixin_id: str = field(repr=False)
    process_epoch: str
    root_token: str
    label_token: str
    value_token: str
    window_id: str | None
    window_association_verified: bool
    composer_token: str | None
    send_token: str | None
    evidence_token: str
    ui_identity_confirmed: bool = False

    def to_public_dict(self):
        return {"status": "parsed_gui_profile_field", "raw_id_hash": text_hash(self.raw_weixin_id),
                "process_hash": text_hash(self.process_epoch), "root_hash": text_hash(self.root_token),
                "window_association_verified": self.window_association_verified,
                "evidence_token": self.evidence_token, "ui_identity_confirmed": False,
                "account_binding": "unverified", "send_allowed": False}


@dataclass(frozen=True)
class ProfileCatalogEvidence:
    profile: RecipientProfileEvidence
    username: str = field(repr=False)
    kind: str
    catalog_token: str
    fresh_source_token: str
    ui_identity_confirmed: bool = False

    def to_public_dict(self):
        return {**self.profile.to_public_dict(), "catalog_match": "unique_exact",
                "username_hash": text_hash(self.username), "kind": self.kind,
                "catalog_token": self.catalog_token, "fresh_source_token": self.fresh_source_token,
                "ui_identity_confirmed": False, "send_allowed": False}


def _profile_string(field):
    if (not isinstance(field, dict) or type(field.get("status")) is not int or field["status"] != 0
            or field.get("type") != "CFString" or not isinstance(field.get("value"), str)):
        return None
    return field["value"]


def _parse_profile_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise GuardError("PROFILE_SNAPSHOT_UNKNOWN")
    epoch, root = snapshot.get("process_epoch"), snapshot.get("root_token")
    if not all(isinstance(value, str) and value for value in (epoch, root)):
        raise GuardError("PROFILE_SCOPE_UNKNOWN")
    children = snapshot.get("children")
    if not isinstance(children, list) or not 4 <= len(children) <= 256:
        raise GuardError("PROFILE_SCHEMA_UNSUPPORTED")
    tokens = []
    for child in children:
        if (not isinstance(child, dict) or child.get("process_epoch") != epoch
                or child.get("parent_root_token") != root or child.get("parent_matches_root") is not True
                or not isinstance(child.get("token"), str) or not child["token"]):
            raise GuardError("PROFILE_CHILD_OUTSIDE_SCOPE")
        tokens.append(child["token"])
    if len(set(tokens)) != len(tokens):
        raise GuardError("PROFILE_CHILD_AMBIGUOUS")
    identifiers = [_profile_string(child.get("identifier")) for child in children]
    counts = [sum(isinstance(value, str) and value.startswith("head_view_") for value in identifiers),
              identifiers.count("value_remark_view"), identifiers.count("chat_img_button")]
    semantic_actions = [child for child in children if child.get("public_action") == "message"
                        and _profile_string(child.get("role")) == "AXButton"]
    if counts != [1, 1, 1] and len(semantic_actions) != 1:
        raise GuardError("PROFILE_MARKERS_MISSING_OR_AMBIGUOUS")
    labels = [_profile_string(child.get("value")) for child in children]
    # Normalization is used only to reject non-exact public labels, never to
    # repair an ID or make an identifier match.
    accepted_labels = {"Weixin ID:", "WeChat ID:", "微信号:", "微信号："}
    if any(value not in accepted_labels and value.strip().casefold() in
           {label.casefold() for label in accepted_labels}
           for value in labels if isinstance(value, str)):
        raise GuardError("PROFILE_LABEL_NOT_EXACT")
    label_indices = [index for index, value in enumerate(labels) if value in accepted_labels]
    if len(label_indices) != 1:
        raise GuardError("PROFILE_LABEL_MISSING_OR_AMBIGUOUS")
    label_index = label_indices[0]
    if label_index + 1 >= len(children):
        raise GuardError("PROFILE_VALUE_MISSING")
    value_node = children[label_index + 1]
    if _profile_string(value_node.get("role")) != "AXStaticText":
        raise GuardError("PROFILE_VALUE_ROLE_UNSUPPORTED")
    raw_identifier = _profile_string(value_node.get("value"))
    if not isinstance(raw_identifier, str) or re.fullmatch(r"[A-Za-z0-9_@.\-]{1,512}", raw_identifier) is None:
        raise GuardError("PROFILE_RAW_ID_INVALID")
    associated = snapshot.get("window_association_verified")
    window = snapshot.get("window_id")
    if type(associated) is not bool or (associated and (not isinstance(window, str) or not window)) or (not associated and window is not None):
        raise GuardError("PROFILE_WINDOW_ASSOCIATION_UNKNOWN")
    for name in ("composer_token", "send_token"):
        if snapshot.get(name) is not None and (not isinstance(snapshot[name], str) or not snapshot[name]):
            raise GuardError("PROFILE_CONTROL_CONTEXT_UNKNOWN")
    return {"raw_weixin_id": raw_identifier, "process_epoch": epoch, "root_token": root,
            "label_token": children[label_index]["token"], "value_token": value_node["token"],
            "window_id": window, "window_association_verified": associated,
            "composer_token": snapshot.get("composer_token"), "send_token": snapshot.get("send_token"),
            "child_tokens": tokens, "identifiers": identifiers}


def parse_recipient_profile_pair(document, *, expected_process_epoch, expected_root_token=None):
    """Parse only the observed schema; no expected recipient/account is inferred."""
    if not isinstance(document, dict) or document.get("schema") not in {"recipient-profile-v1-28", "recipient-profile-v2"}:
        raise GuardError("PROFILE_UNAVAILABLE")
    first = _parse_profile_snapshot(document.get("first"))
    second = _parse_profile_snapshot(document.get("second"))
    if not isinstance(expected_process_epoch, str) or not expected_process_epoch or first["process_epoch"] != expected_process_epoch or second["process_epoch"] != expected_process_epoch:
        raise GuardError("PROFILE_PROCESS_CHANGED")
    if expected_root_token is not None and first["root_token"] != expected_root_token:
        raise GuardError("PROFILE_ROOT_CHANGED")
    if first["root_token"] != second["root_token"]:
        raise GuardError("PROFILE_ROOT_CHANGED")
    if first != second:
        raise GuardError("PROFILE_FIELDS_CHANGED")
    token = text_hash(json.dumps(first, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    selected = {key: value for key, value in first.items() if key not in {"child_tokens", "identifiers"}}
    return RecipientProfileEvidence(**selected, evidence_token=token)


def match_profile_catalog(profile: RecipientProfileEvidence, fresh_catalog) -> ProfileCatalogEvidence:
    """Match exact raw field inside a live FreshIdentityCatalog context only.

    This partial reader never promotes identity by itself. open_chat uses a
    separately verified account/profile-to-chat navigation chain for Binding.
    """
    if not isinstance(profile, RecipientProfileEvidence):
        raise GuardError("PROFILE_EVIDENCE_REQUIRED")
    try:
        fresh_catalog.assert_current()
        result = fresh_catalog.resolve(profile.raw_weixin_id, kind="weixin_id")
        identity = result.identity
        if not result.matched or identity is None:
            raise GuardError("PROFILE_CATALOG_NOT_UNIQUE")
        if (profile.raw_weixin_id not in {identity.username, identity.alias}
                or result.catalog_token != fresh_catalog.catalog.catalog_token
                or identity.catalog_token != result.catalog_token
                or not isinstance(fresh_catalog.source_token, str) or not fresh_catalog.source_token):
            raise GuardError("PROFILE_CATALOG_PROOF_CHANGED")
        fresh_catalog.assert_current()
        return ProfileCatalogEvidence(profile, identity.username, identity.kind,
                                      result.catalog_token, fresh_catalog.source_token)
    except GuardError:
        raise
    except Exception:
        raise GuardError("PROFILE_CATALOG_UNAVAILABLE") from None


class Transport(Protocol):
    def request(self, operation: str, arguments: dict) -> dict: ...
    def close(self) -> None: ...


class _ProcessTransport:
    """One child, serialized JSONL, bounded reads; no retry after IPC failure."""
    def __init__(self, *, allow_draft_writes=False, allow_live_send=False, timeout=10.0):
        if not 0 < timeout <= 30:
            raise GuardError("INVALID_NATIVE_TIMEOUT")
        if _BRIDGE.is_symlink() or not _BRIDGE.is_file():
            raise GuardError("NATIVE_BRIDGE_NOT_BUILT")
        info = _BRIDGE.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or not os.access(_BRIDGE, os.X_OK)):
            raise GuardError("NATIVE_BRIDGE_FILE_UNSAFE")
        arguments = [str(_BRIDGE), "--serve"]
        if allow_draft_writes:
            arguments.append("--allow-draft-writes")
        if allow_live_send:
            arguments.append("--allow-live-send")
        try:
            self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            raise GuardError("NATIVE_BRIDGE_START_FAILED") from None
        self.timeout = float(timeout)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        self._mutex = threading.RLock()
        self._buffer = b""
        self._closed = False

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            if self.process.stdin:
                self.process.stdin.close()
            try:
                # EOF lets the bridge restore any application-local AX override.
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if self.process.stdout:
                self.process.stdout.close()

    def _abort(self, code):
        self.close()
        raise GuardError(code)

    def request(self, operation, arguments):
        with self._mutex:
            if self._closed or self.process.poll() is not None:
                self._abort("NATIVE_BRIDGE_UNAVAILABLE")
            identifier = str(uuid.uuid4())
            try:
                line = json.dumps({"id": identifier, "op": operation, "args": arguments},
                                  ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            except (ValueError, TypeError, UnicodeError):
                raise GuardError("INVALID_LOCAL_IPC_ARGUMENTS") from None
            if len(line) > _MAX_LINE:
                raise GuardError("LOCAL_IPC_ARGUMENTS_TOO_LARGE")
            deadline = time.monotonic() + self.timeout
            try:
                pending = memoryview(line)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._abort("NATIVE_BRIDGE_TIMEOUT")
                    _, writable, _ = select.select([], [self.process.stdin.fileno()], [], remaining)
                    if not writable:
                        self._abort("NATIVE_BRIDGE_TIMEOUT")
                    try:
                        count = os.write(self.process.stdin.fileno(), pending)
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        self._abort("NATIVE_BRIDGE_EXITED")
                    pending = pending[count:]
                while b"\n" not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._abort("NATIVE_BRIDGE_TIMEOUT")
                    readable, _, _ = select.select([self.process.stdout.fileno()], [], [], remaining)
                    if not readable:
                        self._abort("NATIVE_BRIDGE_TIMEOUT")
                    block = os.read(self.process.stdout.fileno(), 65536)
                    if not block:
                        self._abort("NATIVE_BRIDGE_EXITED")
                    self._buffer += block
                    if len(self._buffer) > _MAX_LINE:
                        self._abort("NATIVE_BRIDGE_RESPONSE_TOO_LARGE")
                response_line, self._buffer = self._buffer.split(b"\n", 1)
                response = json.loads(response_line)
            except GuardError:
                raise
            except (OSError, ValueError, UnicodeError):
                self._abort("NATIVE_BRIDGE_IPC_FAILED")
            if not isinstance(response, dict) or response.get("id") != identifier:
                self._abort("NATIVE_BRIDGE_RESPONSE_MISMATCH")
            if response.get("ok") is not True:
                code = response.get("code")
                raise GuardError(code if isinstance(code, str) and code in _SAFE_CODES else "NATIVE_OPERATION_FAILED")
            result = response.get("result")
            if not isinstance(result, dict):
                self._abort("NATIVE_BRIDGE_RESPONSE_MALFORMED")
            return result


def observation_from_wire(document: dict) -> Observation:
    """Typed unknowns remain unknown; no failed read is an empty draft."""
    if not isinstance(document, dict):
        raise GuardError("NATIVE_OBSERVATION_MALFORMED")
    values = {}
    schema_error = False
    raw_binding = document.get("binding")
    binding = None
    if document.get("identity_verified") is True and isinstance(raw_binding, dict):
        expected = {field.name for field in fields(Binding)}
        if (set(raw_binding) == expected and type(raw_binding.get("pid")) is int
                and raw_binding["pid"] > 0
                and all(isinstance(raw_binding[name], str) and raw_binding[name] for name in expected - {"pid"})
                and raw_binding["recipient_type"] in {"person", "group"}
                and raw_binding["bundle_path"] == "/Applications/WeChat.app"):
            binding = Binding(**raw_binding)
        else:
            schema_error = True
    elif document.get("identity_verified") is True:
        schema_error = True
    values["binding"] = binding
    values["draft"] = document.get("draft") if isinstance(document.get("draft"), str) else None
    if values["draft"] is None:
        schema_error = True
    errors = document.get("read_errors")
    if not isinstance(errors, list) or any(not isinstance(value, str) for value in errors):
        errors = ["NATIVE_READ_ERRORS_UNKNOWN"]
    else:
        # Codes are consumed locally; public reporting does not echo them.
        errors = list(errors)
    for field in fields(Observation):
        name = field.name
        if name in {"binding", "draft", "read_errors"}:
            continue
        value = document.get(name)
        if name.endswith("_matches"):
            values[name] = value if type(value) is int else None
        else:
            values[name] = value if type(value) is bool else None
        if name == "locked":
            if document.get(name) is not None and type(document.get(name)) is not bool:
                schema_error = True
        elif values[name] is None:
            schema_error = True
    if binding is None:
        values["identity_verified"] = False
        errors.append("NATIVE_IDENTITY_UNAVAILABLE")
    if schema_error:
        errors.append("NATIVE_SCHEMA_UNKNOWN")
    values["read_errors"] = tuple(errors)
    return Observation(**values)


class NativeBackend:
    def __init__(self, *, transport: Transport | None = None, allow_draft_writes=False, allow_live_send=False):
        if type(allow_draft_writes) is not bool or type(allow_live_send) is not bool:
            raise GuardError("INVALID_DRAFT_POLICY")
        self.allow_draft_writes = allow_draft_writes
        self.allow_live_send = allow_live_send
        self._fresh_catalog = None
        self._submitted_operations = set()
        # Injection is a library-only seam for in-memory tests. The CLI never
        # accepts a transport, fixture, binary path, or draft-write opt-in.
        self.transport = transport if transport is not None else _ProcessTransport(allow_draft_writes=allow_draft_writes, allow_live_send=allow_live_send)
        self._mutex = threading.RLock()

    def close(self):
        self.transport.close()

    def _check_fresh(self):
        if self._fresh_catalog is not None:
            try:
                self._fresh_catalog.assert_current()
            except Exception:
                raise GuardError("FRESH_CATALOG_UNAVAILABLE") from None

    def open_chat(self, resolution, fresh_catalog, *, timeout=20.0):
        """Navigate a unique fresh contact through GUI identity evidence.

        Catalog data supplies possible exact fields; the native bridge must
        independently read the account/profile and follow Message to a composer.
        It returns no Binding merely because a requested ID was supplied.
        """
        if not isinstance(resolution, dict) or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 25:
            raise GuardError("INVALID_NAVIGATION_ARGUMENTS")
        try:
            fresh_catalog.assert_current()
            target_id = resolution["recipient_id"]
            account_id = resolution["account_id"]
            if resolution.get("kind") != "person":
                raise GuardError("UNSUPPORTED_RECIPIENT_KIND")
            if account_id != fresh_catalog.account_id or resolution.get("source_token") != fresh_catalog.source_token:
                raise GuardError("CATALOG_PROOF_CHANGED")
            recipient = fresh_catalog.resolve(target_id, kind="username")
            account = fresh_catalog.resolve(account_id, kind="username")
            if not recipient.matched or not account.matched:
                raise GuardError("CATALOG_IDENTITY_NOT_UNIQUE")
            item, self_item = recipient.identity, account.identity
            if self_item.kind != "person":
                raise GuardError("CATALOG_ACCOUNT_INVALID")
            raw_ids = []
            for value in (item.username, item.alias):
                if not value:
                    continue
                matched = fresh_catalog.resolve(value, kind="weixin_id")
                if matched.matched and matched.identity.username == item.username:
                    raw_ids.append(value)
            account_fields = []
            for value, kind in ((self_item.username, "weixin_id"), (self_item.alias, "weixin_id"),
                                (self_item.nickname + " " + self_item.alias if self_item.alias and self_item.nickname else "", "account_display")):
                if value:
                    matched = fresh_catalog.resolve(value, kind=kind)
                    if matched.matched and matched.identity.username == account_id:
                        account_fields.append(value)
            if not raw_ids or not account_fields:
                raise GuardError("CATALOG_RAW_FIELDS_UNAVAILABLE")
            displays = list(dict.fromkeys(value for value in (item.display_name, item.remark, item.nickname) if isinstance(value, str) and value))
            searches = list(dict.fromkeys(value for value in (item.alias, *resolution.get("search_terms", []), item.username) if isinstance(value, str) and value))
            arguments = {"account_id": account_id, "recipient_id": item.username, "kind": item.kind,
                         "recipient_raw_ids": raw_ids, "display_names": displays, "search_terms": searches,
                         "account_fields": account_fields, "catalog_token": fresh_catalog.catalog.catalog_token,
                         "source_token": fresh_catalog.source_token, "timeout": float(timeout)}
            self._fresh_catalog = fresh_catalog
            with self._mutex:
                previous_timeout = getattr(self.transport, "timeout", None)
                if previous_timeout is not None:
                    self.transport.timeout = max(previous_timeout, timeout + 3)
                try:
                    result = self.transport.request("open_chat", arguments)
                finally:
                    if previous_timeout is not None:
                        self.transport.timeout = previous_timeout
                self._check_fresh()
                observation = observation_from_wire(result)
                binding = observation.binding
                if binding is None or (binding.account_id, binding.recipient_id, binding.recipient_type) != (account_id, item.username, item.kind):
                    raise GuardError("CHAT_BINDING_UNAVAILABLE")
                validate_observation(observation)
                return observation
        except GuardError:
            raise
        except Exception:
            raise GuardError("CONTACT_NAVIGATION_FAILED") from None

    def inspect_structure(self):
        """Redacted AX/WindowServer topology; no navigation, draft or send."""
        with self._mutex:
            return self.transport.request("inspect_structure", {})

    def enable_app_accessibility(self):
        """Request supported app-local AX flags, restored on bridge close."""
        with self._mutex:
            return self.transport.request("enable_app_accessibility", {})

    def restore_app_accessibility(self):
        with self._mutex:
            return self.transport.request("restore_app_accessibility", {})

    def read_observation(self):
        with self._mutex:
            try:
                self._check_fresh()
                return observation_from_wire(self.transport.request("read_observation", {}))
            except GuardError:
                raise
            except Exception:
                raise GuardError("NATIVE_OBSERVATION_UNAVAILABLE") from None

    def read_recipient_profile(self, *, expected_process_epoch=None, expected_root_token=None):
        """Return local-only partial evidence; never upgrades Observation identity."""
        with self._mutex:
            try:
                document = self.transport.request("read_observation", {})
                scope = document.get("native_scope")
                actual_epoch = scope.get("launch_token") if isinstance(scope, dict) else None
                if not isinstance(actual_epoch, str) or not actual_epoch:
                    raise GuardError("PROFILE_PROCESS_UNKNOWN")
                if expected_process_epoch is not None and expected_process_epoch != actual_epoch:
                    raise GuardError("PROFILE_PROCESS_CHANGED")
                return parse_recipient_profile_pair(document.get("recipient_profile"),
                    expected_process_epoch=actual_epoch, expected_root_token=expected_root_token)
            except GuardError:
                raise
            except Exception:
                raise GuardError("PROFILE_UNAVAILABLE") from None

    def set_draft(self, bound_composer_token: BoundComposer, text: str):
        if not self.allow_draft_writes:
            raise GuardError("DRAFT_WRITES_DISABLED")
        if not isinstance(bound_composer_token, BoundComposer) or not isinstance(text, str):
            raise GuardError("INVALID_DRAFT_ARGUMENTS")
        try:
            encoded_size = len(text.encode("utf-8", "strict"))
        except UnicodeError:
            raise GuardError("INVALID_DRAFT_ARGUMENTS") from None
        if encoded_size > 262_144:
            raise GuardError("INVALID_DRAFT_ARGUMENTS")
        with self._mutex:
            validate_observation(self.read_observation(), binding=bound_composer_token.binding,
                                 draft_hash=bound_composer_token.expected_draft_hash, ready=False)
            try:
                result = self.transport.request("set_draft", {"binding": asdict(bound_composer_token.binding),
                    "expected_draft_hash": bound_composer_token.expected_draft_hash, "text": text})
                if result.get("applied") is not True:
                    raise GuardError("DRAFT_WRITE_UNCERTAIN")
                after = observation_from_wire(result.get("observation"))
                # A successful object-bound edit is not a Prepared/send-ready
                # result. SendGuard's nonempty prepare readback uses ready=True.
                validate_observation(after, binding=bound_composer_token.binding,
                                     draft_hash=text_hash(text), ready=False)
            except Exception:
                # A write might already have happened. Never retry or guess.
                raise GuardError("DRAFT_WRITE_UNCERTAIN") from None

    def press_send(self, bound_send_token: BoundSend) -> PressOutcome:
        if not self.allow_live_send:
            return PressOutcome.REJECTED
        if not isinstance(bound_send_token, BoundSend):
            return PressOutcome.REJECTED
        if (isinstance(bound_send_token.expires_at, bool) or not isinstance(bound_send_token.expires_at, (int, float))
                or not math.isfinite(bound_send_token.expires_at) or not time.monotonic() < bound_send_token.expires_at):
            raise GuardError("PREPARED_EXPIRED")
        if not isinstance(bound_send_token.operation_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", bound_send_token.operation_id) is None:
            raise GuardError("INVALID_SEND_ARGUMENTS")
        with self._mutex:
            if bound_send_token.operation_id in self._submitted_operations:
                raise GuardError("SEND_ALREADY_ATTEMPTED")
            self._check_fresh()
            validate_observation(self.read_observation(), binding=bound_send_token.binding,
                                 draft_hash=bound_send_token.expected_draft_hash, ready=True)
            # No retry after crossing this boundary; SendGuard journals it first.
            self._submitted_operations.add(bound_send_token.operation_id)
            result = self.transport.request("press_send", asdict(bound_send_token))
            outcome = result.get("outcome")
            if outcome == "ACCEPTED":
                return PressOutcome.ACCEPTED
            if outcome == "REJECTED":
                return PressOutcome.REJECTED
            return PressOutcome.UNKNOWN

    def probe_report(self):
        try:
            observation = self.read_observation()
        except GuardError:
            return {"ok": False, "code": "NATIVE_OBSERVATION_UNAVAILABLE", "live_send_permitted": False,
                    "send_attempts": 0, "writes_performed": 0}
        binding = observation.binding
        return {"ok": not bool(observation.read_errors), "mode": "native_read_only_probe",
            "capabilities_observed_only": True, "send_preflight_passed": False,
            "live_send_permitted": False, "draft_writes_enabled": self.allow_draft_writes,
            "send_attempts": 0, "writes_performed": 0,
            "identity_verified": observation.identity_verified is True,
            "binding_available": binding is not None,
            "account_hash": text_hash(binding.account_id) if binding else None,
            "recipient_hash": text_hash(binding.recipient_id) if binding else None,
            "launch_hash": text_hash(binding.launch_token) if binding else None,
            "window_hash": text_hash(binding.window_id) if binding else None,
            "draft_known": observation.draft is not None,
            "draft_empty": observation.draft == "" if observation.draft is not None else None,
            "draft_hash": text_hash(observation.draft) if observation.draft is not None else None,
            "read_error_count": len(observation.read_errors or ()),
            "on_screen": observation.on_screen, "focused": observation.focused,
            "composer_focused": observation.composer_focused, "locked": observation.locked,
            "composer_matches": observation.composer_matches, "send_matches": observation.send_matches,
            "composer_editable": observation.composer_editable, "press_supported": observation.press_supported}


class _SyntheticTransport:
    def __init__(self):
        self.document = json.loads(json.dumps(asdict(mock_observation())))
        self.calls = []
    def request(self, operation, arguments):
        self.calls.append(operation)
        if operation == "read_observation":
            return self.document
        raise GuardError("UNSUPPORTED_OPERATION")
    def close(self):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Native AX diagnostics; these CLI modes never send.")
    parser.add_argument("mode", choices=("probe", "self-test", "inspect", "accessibility-diagnose", "activate-and-probe"))
    args = parser.parse_args(argv)
    if args.mode == "self-test":
        transport = _SyntheticTransport()
        backend = NativeBackend(transport=transport)
        report = backend.probe_report()
        assert report["binding_available"] and backend.press_send(None) is PressOutcome.REJECTED
        assert transport.calls == ["read_observation"]
        print('{"ok":true,"synthetic_only":true,"ui_reads":0,"ui_writes":0,"send_attempts":0}')
        return 0
    backend = None
    try:
        backend = NativeBackend()
        if args.mode == "activate-and-probe":
            report = {"ok": True, **backend.transport.request("activate_and_probe", {})}
        elif args.mode == "inspect":
            report = {"ok": True, **backend.inspect_structure()}
        elif args.mode == "accessibility-diagnose":
            before = backend.inspect_structure()
            activated = backend.enable_app_accessibility()
            after = backend.inspect_structure()
            restored = backend.restore_app_accessibility()
            report = {"ok": True, "before": before, "activation": activated,
                      "after": after, "restoration": restored, "send_attempts": 0}
        else:
            report = backend.probe_report()
    except GuardError:
        report = {"ok": False, "code": "NATIVE_BRIDGE_UNAVAILABLE", "live_send_permitted": False,
                  "send_attempts": 0, "writes_performed": 0}
    finally:
        if backend is not None:
            backend.close()
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["ok"] else 78


if __name__ == "__main__":
    raise SystemExit(main())
