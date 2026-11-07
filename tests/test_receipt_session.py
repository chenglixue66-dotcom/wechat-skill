"""Real verifier with synthetic local databases; never live UI or messages."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from receipt_session import ReceiptError, ReceiptSession
from snapshot_recipient import private_directory
from verify_sent import hash_text


class SyntheticSampler:
    recipient = "wxid_peer_fixture"
    own = "wxid_self_fixture"

    def __init__(self, root):
        self.source = root / "source.db"
        self.table = "Msg_" + hashlib.md5(self.recipient.encode()).hexdigest()
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.executemany("INSERT INTO Name2Id VALUES(?)", [(self.own,), (self.recipient,)])
            db.execute('CREATE TABLE "' + self.table + '"(create_time INTEGER,real_sender_id INTEGER,local_type INTEGER,message_content TEXT,local_id INTEGER,server_id INTEGER)')
            db.execute('INSERT INTO "' + self.table + '" VALUES(?,?,?,?,?,?)',
                       (int(time.time()) - 20, 1, 1, "historical fixture", 1, 1001))
        self.source.chmod(0o600)
        self.count = 0
        self.fail_next = False

    def add(self, body):
        with sqlite3.connect(self.source) as db:
            local = db.execute('SELECT max(local_id) FROM "' + self.table + '"').fetchone()[0] + 1
            db.execute('INSERT INTO "' + self.table + '" VALUES(?,?,?,?,?,?)',
                       (int(time.time()), 1, 1, body, local, 1000 + local))

    def __call__(self, *, config_file, output_root):
        if self.fail_next:
            self.fail_next = False
            raise OSError("synthetic snapshot read failure")
        self.count += 1
        output = private_directory(Path(output_root))
        generation = Path(tempfile.mkdtemp(prefix="receipt-", dir=output))
        generation.chmod(0o700)
        target = generation / "message_0.db"
        shutil.copyfile(self.source, target)
        target.chmod(0o600)
        manifest = generation / "manifest.json"
        info = self.source.stat()
        snapshot_id = "synthetic-" + str(self.count)
        document = {"schema": 1, "snapshot_id": snapshot_id, "account_id": self.own,
                    "databases": [{"snapshot_path": str(target),
                                   "snapshot_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                   "source_path": str(self.source), "source_dev": info.st_dev,
                                   "source_ino": info.st_ino, "source_salt_sha256": "a" * 64}]}
        manifest.write_text(json.dumps(document))
        manifest.chmod(0o600)
        return {"manifest": str(manifest), "snapshot_id": snapshot_id,
                "databases": [str(target)], "database_count": 1}


class ReceiptSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-receipt-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.sampler = SyntheticSampler(self.root)
        self.state_root = self.root / "operations"
        self.payload = "synthetic body ✓"
        self.session = self.make()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, **changes):
        args = {"operation_id": "synthetic-op", "recipient_id": self.sampler.recipient,
                "self_wxid": self.sampler.own, "payload_hash": hash_text(self.payload),
                "state_root": self.state_root, "snapshotter": self.sampler}
        args.update(changes)
        return ReceiptSession(**args)

    def strand_baseline_before_prepared(self, session, error_type=OSError):
        original = session._save
        failed = False

        def fail_once(document):
            nonlocal failed
            if document["phase"] == "PREPARED" and not failed:
                failed = True
                raise error_type("synthetic coordinator state write failure")
            return original(document)

        with patch.object(session, "_save", side_effect=fail_once):
            with self.assertRaises(error_type if error_type is KeyboardInterrupt else ReceiptError):
                session.prepare()
        self.assertTrue(session.baseline_file.exists())
        return session.baseline_file.read_bytes()

    def test_new_exact_record_verifies_and_state_contains_no_body(self):
        self.assertEqual(self.session.prepare()["status"], "baseline_created")
        self.assertEqual(list(self.session.snapshot_root.iterdir()), [])
        with self.session.submission() as ticket:
            self.assertGreaterEqual(ticket.committed_at, json.loads(self.session.state_file.read_text())["baseline_at"])
            self.sampler.add(self.payload)
        result = self.session.verify()
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(result["matching_new_rows"], 1)
        self.assertEqual(self.session.public_status()["phase"], "VERIFIED")
        self.assertNotIn(self.payload, self.session.state_file.read_text())
        self.assertEqual(self.session.state_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.session.snapshot_root.iterdir()), [])

    def test_duplicate_prepare_and_cross_instance_submission_are_blocked(self):
        self.session.prepare()
        self.assertEqual(self.make().prepare()["status"], "operation_exists")
        with self.session.submission():
            with self.assertRaisesRegex(ReceiptError, "OPERATION_IN_USE"):
                with self.make().submission():
                    self.fail("concurrent submission admitted")
        with self.assertRaisesRegex(ReceiptError, "SUBMISSION_ALREADY_ATTEMPTED"):
            with self.make().submission():
                self.fail("duplicate submission admitted")

    def test_submit_timeout_is_nonretryable_but_receipt_can_still_verify(self):
        self.session.prepare()
        with self.assertRaisesRegex(TimeoutError, "synthetic timeout"):
            with self.session.submission():
                self.sampler.add(self.payload)
                raise TimeoutError("synthetic timeout")
        self.assertEqual(self.session.public_status()["phase"], "SUBMIT_UNCERTAIN")
        with self.assertRaises(ReceiptError):
            with self.make().submission():
                self.fail("uncertain submission retried")
        self.assertEqual(self.session.verify()["status"], "verified")

    def test_read_failure_can_retry_verification_without_new_submit(self):
        self.session.prepare()
        with self.session.submission():
            self.sampler.add(self.payload)
        self.sampler.fail_next = True
        first = self.session.verify()
        self.assertEqual(first["reason"], "RECEIPT_SNAPSHOT_FAILED")
        self.assertFalse(first["retry_allowed"])
        self.assertEqual(self.session.verify()["status"], "verified")
        with self.assertRaises(ReceiptError):
            with self.session.submission():
                self.fail("receipt retry enabled resubmit")

    def test_verified_result_is_durable_cached_and_not_downgraded_by_read_failure(self):
        self.session.prepare()
        with self.session.submission():
            self.sampler.add(self.payload)
        first = self.session.verify()
        self.assertEqual(first["status"], "verified")
        saved = self.session.state_file.read_bytes()
        previous_count = self.sampler.count
        self.sampler.fail_next = True
        cached = self.make().verify()
        self.assertEqual(cached["status"], "verified")
        self.assertTrue(cached["cached"])
        self.assertFalse(cached["retry_allowed"])
        self.assertFalse(cached["delivery_confirmed"])
        self.assertEqual(cached["records"], first["records"])
        self.assertEqual(self.sampler.count, previous_count)
        self.assertTrue(self.sampler.fail_next)
        self.assertEqual(self.session.state_file.read_bytes(), saved)
        self.assertEqual(self.session.public_status()["phase"], "VERIFIED")
        with self.assertRaises(ReceiptError):
            with self.session.submission():
                self.fail("cached verification enabled resubmit")
        for change in ({"recipient_id": "wxid_other_fixture"},
                       {"payload_hash": hash_text("different payload")}):
            with self.subTest(change=change), self.assertRaisesRegex(ReceiptError, "IDENTITY_CONFLICT"):
                self.make(**change).verify()

    def test_legacy_verified_last_result_can_be_returned_without_resampling(self):
        self.session.prepare()
        with self.session.submission():
            self.sampler.add(self.payload)
        original = self.session.verify()
        state = json.loads(self.session.state_file.read_text())
        state.pop("verified_result")
        self.session.state_file.write_text(json.dumps(state))
        previous_count = self.sampler.count
        cached = self.make().verify()
        self.assertEqual(cached["records"], original["records"])
        self.assertTrue(cached["cached"])
        self.assertEqual(self.sampler.count, previous_count)

    def test_old_same_body_or_new_wrong_body_does_not_verify(self):
        self.sampler.add(self.payload)
        self.session.prepare()
        with self.session.submission():
            self.sampler.add("different fixture body")
        result = self.session.verify()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["matching_new_rows"], 0)

    def test_same_operation_cannot_change_recipient_or_payload(self):
        self.session.prepare()
        for changes in ({"recipient_id": "wxid_other_fixture"}, {"payload_hash": hash_text("other")},
                        {"self_wxid": "wxid_other_self_fixture"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ReceiptError, "IDENTITY_CONFLICT"):
                self.make(**changes).public_status()

    def test_operation_ids_match_guard_and_controller_syntax(self):
        before = set(self.state_root.iterdir())
        for value in ("", ".", ":", "bad.operation", "bad:operation", "bad/path", "x" * 129):
            with self.subTest(operation_id=value), self.assertRaisesRegex(ReceiptError, "INVALID_OPERATION"):
                self.make(operation_id=value)
        self.assertEqual(set(self.state_root.iterdir()), before)
        self.make(operation_id="valid_Operation-0123")

    def test_prepare_snapshot_read_failure_can_retry_before_any_effect(self):
        self.sampler.fail_next = True
        with self.assertRaisesRegex(ReceiptError, "BASELINE_PREPARATION_FAILED"):
            self.session.prepare()
        self.assertEqual(self.session.prepare()["status"], "baseline_created")

    def test_coordinator_save_failure_after_baseline_can_recover_and_submit_once(self):
        self.strand_baseline_before_prepared(self.session)
        self.assertEqual(self.session.public_status()["phase"], "BASELINE_FAILED")
        self.sampler.add("synthetic manual message before any automation effect")
        recovered = self.session.prepare()
        self.assertEqual(recovered["status"], "baseline_created")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["target_row_count"], 2)
        with self.session.submission():
            self.sampler.add(self.payload)
        self.assertEqual(self.session.verify()["status"], "verified")
        self.assertEqual(self.session.prepare()["status"], "operation_exists")

    def test_process_interruption_leaving_preparing_can_recover_stranded_baseline(self):
        self.strand_baseline_before_prepared(self.session, KeyboardInterrupt)
        self.assertEqual(self.session.public_status()["phase"], "PREPARING")
        self.assertTrue(self.session.prepare()["recovered"])

    def test_recovery_rejects_changed_source_and_preserves_stranded_baseline(self):
        original = self.strand_baseline_before_prepared(self.session)
        replaced = self.root / "replacement-source.db"
        shutil.copyfile(self.sampler.source, replaced)
        replaced.replace(self.sampler.source)
        with self.assertRaisesRegex(ReceiptError, "BASELINE_PREPARATION_FAILED"):
            self.session.prepare()
        self.assertEqual(self.session.baseline_file.read_bytes(), original)

    def test_recovery_rejects_conflicting_or_committed_verifier_state(self):
        changes = {"recipient_id": "wxid_wrong_fixture", "self_wxid": "wxid_wrong_self_fixture",
                   "payload_hash": hash_text("conflicting payload"), "committed_at": time.time(),
                   "verification_attempts": 1, "verified_result": {"status": "verified"}}
        for key, value in changes.items():
            with self.subTest(field=key):
                session = self.make(operation_id="recovery_conflict_" + key)
                self.strand_baseline_before_prepared(session)
                baseline = json.loads(session.baseline_file.read_text())
                baseline[key] = value
                session.baseline_file.write_text(json.dumps(baseline))
                original = session.baseline_file.read_bytes()
                with self.assertRaisesRegex(ReceiptError, "BASELINE_PREPARATION_FAILED"):
                    session.prepare()
                self.assertEqual(session.baseline_file.read_bytes(), original)

    def test_orphan_baseline_without_coordinator_phase_cannot_be_reset(self):
        original = self.strand_baseline_before_prepared(self.session)
        self.session.state_file.unlink()
        with self.assertRaisesRegex(ReceiptError, "BASELINE_PREPARATION_FAILED"):
            self.session.prepare()
        self.assertEqual(self.session.baseline_file.read_bytes(), original)

    def test_submitted_phase_is_never_recovered_or_reset(self):
        self.session.prepare()
        with self.assertRaises(TimeoutError):
            with self.session.submission():
                raise TimeoutError("synthetic unknown effect")
        before = self.session.state_file.read_bytes()
        self.assertEqual(self.session.prepare()["status"], "operation_exists")
        self.assertEqual(self.session.state_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
