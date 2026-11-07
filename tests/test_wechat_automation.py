"""Offline integration of resolver -> real guard -> receipt verification.

All UI observations/actions are memory-only MockBackend operations. Databases
contain synthetic rows in temporary directories; no live application is used.
"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from contact_resolver import ContactResolution
from identity_catalog import CatalogError, ContactIdentity
from receipt_session import ReceiptSession
import send_guard as sg
import wechat_automation as automation
from test_receipt_session import SyntheticSampler


class FreshFixture:
    def __init__(self):
        self.failure = None
        self.checks = 0

    def assert_current(self):
        self.checks += 1
        if self.failure:
            raise CatalogError(self.failure)


class AutomationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-controller-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.sampler = SyntheticSampler(self.root)
        self.state_root = self.root / "operations"
        self.body = "controller synthetic payload ✓"
        self.events = []
        self.backends = []
        self.receipts = []
        self.fresh = FreshFixture()
        self.open_hook = None
        self.read_hook = None
        self.initial_changes = {}
        self.press_mode = "accepted"
        self.close_raises = False
        item = ContactIdentity(self.sampler.recipient, "peer_fixture_alias", "Target",
                               "Fixture Peer", "Target", "person", "catalog_fixture")
        self.resolution = ContactResolution("Target", self.sampler.own, "source_fixture",
            (item,), ("remark",), "matched", None)

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def resolver(self, query, config_file, **kwargs):
        self.events.append("resolve")
        self.assertEqual(query, "Target")
        self.assertEqual(kwargs["allowed_kinds"], {"person"})
        self.fresh.assert_current()
        yield self.resolution, self.fresh

    def backend_factory(self, **kwargs):
        case = self

        class Backend(sg.MockBackend):
            def __init__(self):
                original = sg.mock_observation()
                binding = replace(original.binding, account_id=case.sampler.own,
                                  recipient_id=case.sampler.recipient)
                super().__init__(replace(original, **{"binding": binding, **case.initial_changes}))
                self.options = kwargs
                self.writes = []
                self.attempts = 0
                self.read_count = 0
                self.closed = False

            def open_chat(self, target, fresh, timeout):
                fresh.assert_current()
                case.events.append("open_chat")
                case.assertEqual(target["recipient_id"], case.sampler.recipient)
                case.assertEqual(timeout, 20.0)
                if case.open_hook:
                    case.open_hook(self, fresh)

            def read_observation(self):
                self.read_count += 1
                if case.read_hook:
                    case.read_hook(self, self.read_count)
                return super().read_observation()

            def set_draft(self, token, text):
                super().set_draft(token, text)
                self.writes.append(text)
                case.events.append("draft" if text else "cleanup")

            def press_send(self, token):
                self.attempts += 1
                case.events.append("press_send")
                case.assertTrue(self.options["allow_live_send"])
                if case.press_mode == "raise_before_effect":
                    raise TimeoutError("synthetic send response failure")
                body = self.observation.draft
                outcome = super().press_send(token)
                case.sampler.add(body)
                if case.press_mode == "raise_after_effect":
                    raise TimeoutError("synthetic send response lost after effect")
                if case.press_mode == "unknown_after_effect":
                    return sg.PressOutcome.UNKNOWN
                return outcome

            def close(self):
                self.closed = True
                case.events.append("close")
                if case.close_raises:
                    raise RuntimeError("synthetic cleanup exception")

        backend = Backend()
        self.backends.append(backend)
        return backend

    def receipt_factory(self, *args, **kwargs):
        case = self

        class Receipt(ReceiptSession):
            verifications = 0

            def prepare(self, config_file):
                case.events.append("baseline")
                return super().prepare(config_file)

            def verify(self, config_file):
                self.verifications += 1
                case.events.append("verify")
                return super().verify(config_file)

        receipt = Receipt(*args, **kwargs, snapshotter=self.sampler)
        self.receipts.append(receipt)
        return receipt

    def execute(self, *, dry=False, operation_id="controller_fixture"):
        return automation.execute("Target", self.body, operation_id, dry=dry,
            state_root=self.state_root, resolver=self.resolver,
            backend_factory=self.backend_factory, receipt_factory=self.receipt_factory)

    def test_real_guard_prepares_commits_once_and_real_receipt_verifies(self):
        result = self.execute()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["native_commit_state"], "ATTEMPTED")
        self.assertEqual(result["send_attempts"], 1)
        self.assertFalse(result["delivery_confirmed"])
        backend = self.backends[0]
        self.assertEqual(backend.writes, [self.body])
        self.assertEqual(backend.attempts, 1)
        self.assertTrue(backend.closed)
        self.assertLess(self.events.index("baseline"), self.events.index("open_chat"))
        self.assertLess(self.events.index("draft"), self.events.index("press_send"))
        self.assertLess(self.events.index("press_send"), self.events.index("verify"))

    def test_dry_navigates_writes_and_cleans_without_send_or_receipt_scan(self):
        result = self.execute(dry=True)
        self.assertEqual(result["status"], "dry_verified")
        self.assertTrue(result["navigation_verified"])
        self.assertTrue(result["draft_cleaned"])
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(self.backends[0].writes, [self.body, ""])
        self.assertEqual(self.backends[0].attempts, 0)
        self.assertEqual(self.backends[0].observation.draft, "")
        self.assertEqual(self.sampler.count, 0)
        self.assertNotIn("baseline", self.events)
        self.assertNotIn("verify", self.events)

    def test_observed_identity_mismatch_never_writes_or_sends(self):
        mismatch = replace(sg.mock_observation().binding,
                           account_id=self.sampler.own, recipient_id="wxid_wrong_fixture")
        self.initial_changes = {"binding": mismatch}
        result = self.execute()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "RECIPIENT_MISMATCH")
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(self.backends[0].writes, [])
        self.assertEqual(self.backends[0].attempts, 0)

    def test_existing_user_draft_is_preserved(self):
        self.initial_changes = {"draft": "synthetic existing user draft", "send_enabled": True}
        result = self.execute()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.backends[0].writes, [])
        self.assertEqual(self.backends[0].observation.draft, "synthetic existing user draft")

    def test_baseline_failure_does_not_construct_or_navigate_backend(self):
        self.sampler.fail_next = True
        result = self.execute()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stage"], "receipt_baseline")
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(self.backends, [])
        self.assertNotIn("open_chat", self.events)

    def test_unknown_and_lost_send_responses_only_verify_without_resend(self):
        for mode in ("raise_before_effect", "raise_after_effect", "unknown_after_effect"):
            with self.subTest(mode=mode):
                self.press_mode = mode
                result = self.execute(operation_id="response_" + mode)
                self.assertEqual(self.backends[-1].attempts, 1)
                self.assertEqual(self.receipts[-1].verifications, 1)
                self.assertEqual(result["send_attempts"], 1)
                self.assertEqual(result["native_commit_state"], "UNKNOWN")
                self.assertEqual(result["status"], "uncertain" if mode == "raise_before_effect" else "verified")

    def test_exception_after_commit_still_verifies_and_does_not_resubmit(self):
        original = automation.SendGuard.commit

        def effect_then_fail(guard, prepared):
            original(guard, prepared)
            raise RuntimeError("synthetic coordinator exception after effect")

        with patch.object(automation.SendGuard, "commit", effect_then_fail):
            result = self.execute()
        self.assertEqual(self.backends[0].attempts, 1)
        self.assertEqual(self.receipts[0].verifications, 1)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["send_attempts"], 1)

    def test_generic_commit_failure_before_effect_only_verifies_and_counts_zero(self):
        with patch.object(automation.SendGuard, "commit", side_effect=RuntimeError("synthetic pre-effect exception")):
            result = self.execute()
        self.assertEqual(self.backends[0].attempts, 0)
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(self.receipts[0].verifications, 1)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.execute()["status"], "operation_exists")
        self.assertEqual(len(self.backends), 1)

    def test_repeat_operation_never_navigates_or_submits_again(self):
        self.assertEqual(self.execute()["status"], "verified")
        before = len(self.backends), self.events.count("open_chat"), self.sampler.count
        result = self.execute()
        self.assertEqual(result["status"], "operation_exists")
        self.assertFalse(result["send_allowed"])
        self.assertEqual((len(self.backends), self.events.count("open_chat"), self.sampler.count), before)
        self.assertEqual(self.backends[0].attempts, 1)

    def test_source_change_or_catalog_expiry_during_navigation_stops_before_draft(self):
        for code in ("CONTACT_SOURCE_CHANGED", "FRESH_CATALOG_EXPIRED"):
            with self.subTest(code=code):
                self.fresh.failure = None
                self.open_hook = lambda backend, fresh: setattr(fresh, "failure", code)
                result = self.execute(operation_id="freshness_" + code)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["code"], code)
                self.assertEqual(self.backends[-1].writes, [])
                self.assertEqual(self.backends[-1].attempts, 0)

    def test_source_change_during_commit_is_not_ignored_or_retried(self):
        def change_on_commit_read(backend, count):
            if count == 3:
                self.fresh.failure = "CONTACT_SOURCE_CHANGED"

        self.read_hook = change_on_commit_read
        result = self.execute()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(self.backends[0].attempts, 0)
        self.assertEqual(self.receipts[0].verifications, 1)
        result2 = self.execute()
        self.assertEqual(result2["status"], "blocked")
        self.assertEqual(self.backends[0].attempts, 0)

    def test_backend_close_failure_does_not_override_verified_result(self):
        self.close_raises = True
        result = self.execute()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.backends[0].attempts, 1)


if __name__ == "__main__":
    unittest.main()
