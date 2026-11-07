#!/usr/bin/env python3
"""One-request WeChat controller: resolve, navigate, draft, submit once, verify.

The native adapter owns all UI identity/control evidence. This coordinator does
not manufacture an identity from the configured account or contact directory.
No fallback silently sends into whichever window happens to be focused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from contact_resolver import open_resolved_contact
from identity_catalog import CatalogError
from native_backend import NativeBackend
from receipt_session import DEFAULT_STATE_ROOT, ReceiptError, ReceiptSession
from send_guard import GuardError, RecipientRequest, SendGuard, text_hash
from snapshot_recipient import DEFAULT_CONFIG


class _FreshBackend:
    """Keep the independently sampled contact catalog live at every UI effect."""
    def __init__(self, backend, fresh):
        self.backend, self.fresh = backend, fresh
        self.send_attempts = 0

    def read_observation(self):
        self.fresh.assert_current()
        observation = self.backend.read_observation()
        self.fresh.assert_current()
        return observation

    def set_draft(self, token, text):
        self.fresh.assert_current()
        self.backend.set_draft(token, text)
        self.fresh.assert_current()

    def press_send(self, token):
        self.fresh.assert_current()
        self.send_attempts += 1
        return self.backend.press_send(token)


def execute(recipient, payload, operation_id, *, dry=False, config_file=DEFAULT_CONFIG,
            state_root=DEFAULT_STATE_ROOT, resolver=None, backend_factory=None,
            receipt_factory=None):
    """Execute a fixed request. Injection seams are library-only, for tests.

    --dry performs the real navigation and reversible draft prepare/cleanup,
    never submit. A real attempt needs a caller-supplied stable operation ID.
    Re-running a submitted operation cannot authorize another message.
    """
    if (not isinstance(operation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id) is None
            or not isinstance(recipient, str) or not recipient
            or not isinstance(payload, str) or not payload
            or len(payload.encode("utf-8", "strict")) > 262144):
        raise GuardError("INVALID_REQUEST")
    resolver = resolver or open_resolved_contact
    backend_factory = backend_factory or NativeBackend
    receipt_factory = receipt_factory or ReceiptSession
    result = {"operation_id": operation_id, "send_attempts": 0,
              "delivery_confirmed": False, "retry_allowed": False}
    stage, backend, prepared, guard, receipt = "resolve_contact", None, None, None, None
    guarded_backend = None
    submitted = False
    try:
        with resolver(recipient, config_file, ttl_seconds=30.0,
                      allowed_kinds={"person"}) as (resolution, fresh):
            target = resolution.to_private_dict()
            request = RecipientRequest(operation_id, resolution.account_id,
                resolution.recipient_id, resolution.identity.kind, payload)
            result["recipient_hash"] = hashlib.sha256(request.recipient_id.encode()).hexdigest()
            receipt = receipt_factory(operation_id, request.recipient_id,
                request.account_id, text_hash(payload), state_root=state_root)
            if not dry:
                stage = "receipt_baseline"
                baseline = receipt.prepare(config_file)
                if baseline.get("status") != "baseline_created":
                    return {**result, "status": "operation_exists", "stage": stage,
                            "phase": baseline.get("phase"), "send_allowed": False}
            stage = "open_chat"
            backend = backend_factory(allow_draft_writes=True, allow_live_send=not dry)
            backend.open_chat(target, fresh, timeout=20.0)
            fresh.assert_current()
            guarded_backend = _FreshBackend(backend, fresh)
            guard = SendGuard(guarded_backend, Path(state_root) / "native-send-journal.json",
                              ttl_seconds=20.0)
            stage = "prepare_draft"
            prepared = guard.prepare(request)
            if dry:
                stage = "cleanup_draft"
                cleaned = guard.cleanup(prepared)
                return {**result, "status": "dry_verified" if cleaned else "cleanup_uncertain",
                        "stage": stage, "draft_cleaned": cleaned,
                        "navigation_verified": True, "send_allowed": False}
            stage = "submit_once"
            with receipt.submission():
                # The receipt lock is durable before the guarded native action.
                # Mark locally before entering it as exceptions can follow an effect.
                submitted = True
                commit = guard.commit(prepared)
                result["send_attempts"] = guarded_backend.send_attempts
            result["native_commit_state"] = commit.state
            stage = "verify_receipt"
            verified = receipt.verify(config_file)
            return {**result, **verified, "stage": stage,
                    "native_commit_state": commit.state}
    except (GuardError, CatalogError, ReceiptError) as error:
        if guarded_backend is not None:
            result["send_attempts"] = guarded_backend.send_attempts
        # A failed receipt or a lost response must never cause a second submit.
        if submitted and receipt is not None:
            try:
                verified = receipt.verify(config_file)
                if verified.get("status") == "verified":
                    return {**result, **verified, "stage": "verify_receipt"}
            except Exception:
                pass
        cleaned = None
        if prepared is not None and guard is not None and not submitted:
            try:
                cleaned = guard.cleanup(prepared)
            except Exception:
                cleaned = False
        return {**result, "status": "uncertain" if submitted else "blocked",
                "stage": stage, "code": error.code, "draft_cleaned": cleaned}
    except Exception:
        # Do not echo raw UI/key/body-bearing exception text.
        if guarded_backend is not None:
            result["send_attempts"] = guarded_backend.send_attempts
        if submitted and receipt is not None:
            try:
                verified = receipt.verify(config_file)
                if verified.get("status") == "verified":
                    return {**result, **verified, "stage": "verify_receipt"}
            except Exception:
                pass
        cleaned = None
        if prepared is not None and guard is not None and not submitted:
            try:
                cleaned = guard.cleanup(prepared)
            except Exception:
                cleaned = False
        return {**result, "status": "uncertain" if submitted else "blocked",
                "stage": stage, "code": "AUTOMATION_UNAVAILABLE", "draft_cleaned": cleaned}
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass  # Cleanup cannot change an independently verified receipt.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipient")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--text")
    body.add_argument("--text-file", type=Path,
                      help="UTF-8 body file; use '-' to read stdin")
    parser.add_argument("--operation-id", help="Stable identifier for this one user request")
    parser.add_argument("--dry", action="store_true", help="Navigate, prepare and clean; never send")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)
    if not args.operation_id and not args.dry:
        parser.error("real sending requires --operation-id; keep it unchanged for this request")
    try:
        payload = args.text if args.text is not None else (
            sys.stdin.read() if str(args.text_file) == "-" else args.text_file.read_text(encoding="utf8"))
        result = execute(args.recipient, payload, args.operation_id or "dry_" + uuid.uuid4().hex,
            dry=args.dry, config_file=args.config, state_root=args.state_root)
    except Exception:
        result = {"status": "blocked", "code": "REQUEST_UNAVAILABLE",
                  "send_attempts": 0, "retry_allowed": False, "delivery_confirmed": False}
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    return 0 if result.get("status") in {"verified", "dry_verified"} else 78


if __name__ == "__main__":
    raise SystemExit(main())
