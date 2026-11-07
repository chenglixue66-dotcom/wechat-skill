"""Synthetic-only tests: no WeChat process, real accounts, decryption or keys."""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_sent.py"
spec = importlib.util.spec_from_file_location("synthetic_verify_sent", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
SELF = "fixture_self"
RECIPIENT = "fixture_exact_recipient_a"
OTHER = "fixture_exact_recipient_b"
TEXT = "synthetic exact payload, never user content"
PAYLOAD = verifier.hash_text(TEXT)


def table(recipient):
    return "Msg_" + hashlib.md5(recipient.encode()).hexdigest()


def create_database(path, *, own_ids=(5, 7), peer_id=8):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        for own_id in own_ids:
            connection.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (own_id, SELF))
        connection.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (peer_id, RECIPIENT))
        # These deliberately share a display label: only IDs select a table.
        connection.execute("CREATE TABLE synthetic_contacts(user_name TEXT,remark TEXT)")
        connection.executemany("INSERT INTO synthetic_contacts VALUES(?,?)", [(RECIPIENT, "Same label"), (OTHER, "Same label")])
        for recipient in (RECIPIENT, OTHER):
            connection.execute(f'CREATE TABLE "{table(recipient)}"(create_time INT,real_sender_id INT,local_type INT,message_content BLOB,local_id INT,server_id INT)')
            connection.execute(f'INSERT INTO "{table(recipient)}" VALUES(?,?,?,?,?,?)',
                               (int(time.time()) - 100, own_ids[0], 1, TEXT, 1, 101))
        connection.commit()


def insert(path, recipient=RECIPIENT, *, sender=5, body=TEXT, message_type=1, local_id=2, server_id=102, timestamp=None):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f'INSERT INTO "{table(recipient)}" VALUES(?,?,?,?,?,?)',
                           (int(time.time()) if timestamp is None else timestamp,
                            sender, message_type, body, local_id, server_id))
        connection.commit()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="wechat-verifier-synthetic-")
        self.root = Path(self.directory.name).resolve()
        self.db = self.root / "message_0.db"
        self.state = self.root / "operation.json"
        self.operation = "fixture-operation-001"
        create_database(self.db)

    def tearDown(self):
        self.directory.cleanup()

    def baseline(self, **overrides):
        arguments = dict(databases=[self.db], baseline=self.state, recipient_id=RECIPIENT,
                         self_wxid=SELF, payload_hash=PAYLOAD, operation_id=self.operation,
                         snapshot_id="fixture-snapshot")
        arguments.update(overrides)
        result = verifier.create_baseline(**arguments)
        self.assertEqual(result["status"], "baseline_created", result)
        self.commit = time.time()
        return result

    def verify(self, **overrides):
        arguments = dict(databases=[self.db], baseline=self.state, recipient_id=RECIPIENT,
                         operation_id=self.operation, committed_at=self.commit,
                         snapshot_id="fixture-snapshot")
        arguments.update(overrides)
        result = verifier.verify(**arguments)
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn(TEXT, json.dumps(result))
        return result

    def test_old_identical_text_is_not_new_success(self):
        self.baseline()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TEXT, self.state.read_text())
        original = self.db.read_bytes()
        self.assertEqual(self.verify()["status"], "uncertain")
        self.assertEqual(self.db.read_bytes(), original)
        self.assertFalse(any(Path(str(self.db) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")))

    def test_exact_recipient_and_new_self_message_only(self):
        self.baseline()
        insert(self.db, OTHER)  # The other conversation has the same display label.
        insert(self.db, sender=8, local_id=2, server_id=102)
        self.assertEqual(self.verify()["matching_new_rows"], 0)
        insert(self.db, sender=7, local_id=3, server_id=103)
        result = self.verify()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["matching_new_rows"], 1)
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(result["records"][0]["local_id"], "3")
        wrong_recipient = self.verify(recipient_id=OTHER)
        self.assertEqual(wrong_recipient["reason"], "OPERATION_OR_RECIPIENT_MISMATCH")

    def test_sender_ids_are_per_shard(self):
        second = self.root / "message_2.db"
        create_database(second, own_ids=(9,), peer_id=5)
        self.baseline(databases=[self.db, second])
        insert(second, sender=5)
        result = self.verify(databases=[self.db, second])
        self.assertEqual(result["matching_new_rows"], 0)
        insert(second, sender=9, local_id=3, server_id=103)
        result = self.verify(databases=[self.db, second])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["records"][0]["database_id"], hashlib.sha256(str(second).encode()).hexdigest()[:24])

    def test_type_must_match(self):
        self.baseline()
        insert(self.db, message_type=3)
        self.assertEqual(self.verify()["matching_new_rows"], 0)
        insert(self.db, message_type=1 | (49 << 32), local_id=3, server_id=103)
        self.assertEqual(self.verify()["status"], "verified")

    def test_multiple_identical_new_messages_are_ambiguous(self):
        self.baseline()
        insert(self.db)
        insert(self.db, local_id=3, server_id=103)
        result = self.verify()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["matching_new_rows"], 2)

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "optional Zstd decoder unavailable")
    def test_compressed_body_and_decode_failure(self):
        import zstandard
        self.baseline()
        insert(self.db, body=zstandard.ZstdCompressor().compress(TEXT.encode()))
        self.assertEqual(self.verify()["status"], "verified")
        # A different operation starts before a malformed compressed new row.
        next_state = self.root / "next-operation.json"
        self.baseline(baseline=next_state, operation_id="fixture-operation-002")
        insert(self.db, body=b"\x28\xb5\x2f\xfdinvalid", local_id=3, server_id=103)
        result = self.verify(baseline=next_state, operation_id="fixture-operation-002")
        self.assertEqual(result["reason"], "BODY_DECODE_UNCERTAIN")

    def test_optional_local_and_server_id_columns(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(f'DROP TABLE "{table(RECIPIENT)}"')
            connection.execute(f'CREATE TABLE "{table(RECIPIENT)}"(create_time INT,real_sender_id INT,local_type INT,message_content BLOB)')
            connection.commit()
        self.baseline()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(f'INSERT INTO "{table(RECIPIENT)}" VALUES(?,?,?,?)',
                               (int(time.time()), 5, 1, TEXT))
            connection.commit()
        result = self.verify()
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["records"][0]["local_id"])
        self.assertIsNone(result["records"][0]["server_id"])

    def test_replaced_database_is_uncertain(self):
        self.baseline()
        replacement = self.root / "replacement.db"
        create_database(replacement)
        insert(replacement)
        os.replace(replacement, self.db)
        result = self.verify()
        self.assertEqual(result["reason"], "DATABASE_REPLACED")

    def test_rebuilt_same_inode_is_uncertain(self):
        self.baseline()
        inode = self.db.stat().st_ino
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(f'DROP TABLE "{table(RECIPIENT)}"')
            connection.execute(f'CREATE TABLE "{table(RECIPIENT)}"(create_time INT,real_sender_id INT,local_type INT,message_content BLOB,local_id INT,server_id INT)')
            connection.commit()
        self.assertEqual(self.db.stat().st_ino, inode)
        self.assertEqual(self.verify()["reason"], "SCHEMA_OR_DATABASE_REBUILT")

    def test_reused_local_id_is_uncertain(self):
        self.baseline()
        insert(self.db, local_id=1, server_id=102)
        self.assertEqual(self.verify()["reason"], "LOCAL_ID_REUSED_OR_RESET")

    def test_history_mutation_is_uncertain(self):
        self.baseline()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(f'UPDATE "{table(RECIPIENT)}" SET message_content=? WHERE local_id=1', ("changed fixture",))
            connection.commit()
        insert(self.db)
        self.assertEqual(self.verify()["reason"], "BASELINE_HISTORY_CHANGED")

    def test_snapshot_set_or_token_change_is_uncertain(self):
        self.baseline()
        self.assertEqual(self.verify(snapshot_id="different-snapshot")["reason"], "SNAPSHOT_SET_CHANGED")
        other = self.root / "another-copy.db"
        create_database(other)
        self.assertEqual(self.verify(databases=[other])["reason"], "SNAPSHOT_SET_CHANGED")

    def test_operation_and_success_are_idempotent(self):
        self.baseline()
        saved_baseline = self.state.read_bytes()
        again = verifier.create_baseline([self.db], self.state, RECIPIENT, SELF, PAYLOAD,
                                         self.operation, snapshot_id="fixture-snapshot")
        self.assertEqual(again["status"], "baseline_exists")
        self.assertFalse(again["send_allowed"])
        self.assertEqual(self.state.read_bytes(), saved_baseline)
        conflict = verifier.create_baseline([self.db], self.state, RECIPIENT, SELF,
                                            verifier.hash_text("other payload"), self.operation,
                                            snapshot_id="fixture-snapshot")
        self.assertEqual(conflict["status"], "uncertain")
        insert(self.db)
        first, second = self.verify(), self.verify()
        self.assertEqual(first["status"], "verified")
        self.assertEqual(second["status"], "verified")
        self.assertTrue(second["cached"])
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(self.verify(committed_at=self.commit + 0.001)["reason"], "COMMIT_TIME_CHANGED")

    def test_missing_database_is_not_created(self):
        missing = self.root / "does-not-exist.db"
        result = verifier.create_baseline([missing], self.state, RECIPIENT, SELF, PAYLOAD, self.operation)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(missing.exists())

    def test_match_before_commit_is_uncertain(self):
        self.baseline()
        insert(self.db, timestamp=math.floor(self.commit) - 1)
        self.assertEqual(self.verify()["reason"], "MATCH_OUTSIDE_COMMIT_WINDOW")

    def test_cli_is_json_only(self):
        process = subprocess.run([
            sys.executable, str(SCRIPT), "baseline", "--db", str(self.db),
            "--baseline", str(self.state), "--recipient-id", RECIPIENT,
            "--self-wxid", SELF, "--payload-hash", PAYLOAD, "--operation-id", self.operation,
        ], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)["status"], "baseline_created")
        self.assertEqual(process.stderr, "")
        invalid = subprocess.run([sys.executable, str(SCRIPT), "verify", "--db", str(self.db)],
                                 capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["status"], "uncertain")
        self.assertEqual(invalid.stderr, "")


class ManifestVerificationTests(unittest.TestCase):
    tearDown = VerificationTests.tearDown
    baseline = VerificationTests.baseline
    verify = VerificationTests.verify

    def setUp(self):
        VerificationTests.setUp(self)
        self.source = self.root / "synthetic-encrypted-source.db"
        self.source.write_bytes(b"fixture-salt-001!" + b"synthetic encrypted fixture bytes")
        self.source.chmod(0o600)

    def manifest(self, databases, *, snapshot_id, account_id=SELF, source=None, directory=None):
        source = source or self.source
        entries = []
        for path in databases:
            selected_source = source[path] if isinstance(source, dict) else source
            source_info = selected_source.stat()
            entries.append({
                "snapshot_path": str(path.resolve()),
                "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_path": str(selected_source.resolve()),
                "source_dev": source_info.st_dev,
                "source_ino": source_info.st_ino,
                "source_salt_sha256": hashlib.sha256(selected_source.read_bytes()[:16]).hexdigest(),
            })
        target = (directory or self.root) / ("manifest-" + snapshot_id + ".json")
        target.write_text(json.dumps({"schema": 1, "snapshot_id": snapshot_id,
                                      "account_id": account_id, "databases": entries}))
        target.chmod(0o600)
        return target

    def start(self):
        before = self.manifest([self.db], snapshot_id="before")
        self.baseline(manifest=before, snapshot_id="before")
        directory = self.root / "new-generation"
        directory.mkdir(mode=0o700)
        new = directory / "message_0.db"
        shutil.copy2(self.db, new)
        insert(new)
        after = self.manifest([new], snapshot_id="after")
        return new, after

    def rewrite(self, path, mutate):
        document = json.loads(path.read_text())
        mutate(document)
        path.write_text(json.dumps(document))

    def test_stable_source_allows_new_immutable_copies_and_cached_replay(self):
        new, after = self.start()
        self.assertNotEqual(new.stat().st_ino, self.db.stat().st_ino)
        result = self.verify(databases=[new], manifest=after, snapshot_id="after")
        self.assertEqual(result["status"], "verified", result)
        newest = self.root / "yet-another-copy.db"
        shutil.copy2(new, newest)
        again = self.manifest([newest], snapshot_id="after-again")
        cached = self.verify(databases=[newest], manifest=again, snapshot_id="after-again")
        self.assertEqual(cached["status"], "verified", cached)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["records"], result["records"])
        self.assertNotIn(TEXT, self.state.read_text())

    def test_different_source_path_is_rejected(self):
        new, _ = self.start()
        different = self.root / "different-source.db"
        different.write_bytes(self.source.read_bytes())
        after = self.manifest([new], snapshot_id="different-source", source=different)
        result = self.verify(databases=[new], manifest=after, snapshot_id="different-source")
        self.assertEqual(result["reason"], "LINEAGE_SOURCE_SET_CHANGED")

    def test_multiple_sources_pair_by_identity_and_changed_set_is_rejected(self):
        second = self.root / "zz-message-2.db"
        create_database(second, own_ids=(9,), peer_id=5)
        second_source = self.root / "second-encrypted-source.db"
        second_source.write_bytes(b"second-salt-001!" + b"synthetic second source")
        before = self.manifest([self.db, second], snapshot_id="before-multiple",
                               source={self.db: self.source, second: second_source})
        self.baseline(databases=[self.db, second], manifest=before, snapshot_id="before-multiple")
        # Deliberately reverse lexical path order relative to baseline sources.
        first_copy, second_copy = self.root / "z-new.db", self.root / "a-new.db"
        shutil.copy2(self.db, first_copy)
        shutil.copy2(second, second_copy)
        insert(second_copy, sender=9)
        sources = {first_copy: self.source, second_copy: second_source}
        after = self.manifest([first_copy, second_copy], snapshot_id="after-multiple", source=sources)
        result = self.verify(databases=[second_copy, first_copy], manifest=after, snapshot_id="after-multiple")
        self.assertEqual(result["status"], "verified", result)
        extra = self.root / "extra-copy.db"
        create_database(extra)
        extra_source = self.root / "extra-encrypted-source.db"
        extra_source.write_bytes(b"extra-salt-001!!" + b"synthetic third source")
        sources[extra] = extra_source
        after = self.manifest([first_copy, second_copy, extra], snapshot_id="extra-source", source=sources)
        result = self.verify(databases=[first_copy, second_copy, extra], manifest=after, snapshot_id="extra-source")
        self.assertEqual(result["reason"], "LINEAGE_SOURCE_SET_CHANGED")

    def test_source_replacement_is_rejected(self):
        new, _ = self.start()
        replacement = self.root / "replaced-source.db"
        replacement.write_bytes(self.source.read_bytes())
        os.replace(replacement, self.source)
        after = self.manifest([new], snapshot_id="replaced-source")
        result = self.verify(databases=[new], manifest=after, snapshot_id="replaced-source")
        self.assertEqual(result["reason"], "LINEAGE_SOURCE_SET_CHANGED")

    def test_changed_salt_or_account_is_rejected(self):
        new, after = self.start()
        self.rewrite(after, lambda value: value["databases"][0].update(source_salt_sha256="f" * 64))
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="after")["reason"], "LINEAGE_SOURCE_SET_CHANGED")
        after = self.manifest([new], snapshot_id="wrong-account", account_id="another_fixture_self")
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="wrong-account")["reason"], "MANIFEST_ACCOUNT_MISMATCH")

    def test_incorrect_snapshot_hash_is_rejected(self):
        new, after = self.start()
        self.rewrite(after, lambda value: value["databases"][0].update(snapshot_sha256="0" * 64))
        result = self.verify(databases=[new], manifest=after, snapshot_id="after")
        self.assertEqual(result["reason"], "MANIFEST_SNAPSHOT_HASH_MISMATCH")

    def test_manifest_snapshot_id_must_match_cli(self):
        new, after = self.start()
        result = self.verify(databases=[new], manifest=after, snapshot_id="wrong-id")
        self.assertEqual(result["reason"], "MANIFEST_SNAPSHOT_ID_MISMATCH")

    def test_missing_identity_fields_or_source_file_are_rejected(self):
        new, after = self.start()
        self.rewrite(after, lambda value: value["databases"][0].pop("source_salt_sha256"))
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="after")["reason"], "INCOMPLETE_MANIFEST_ENTRY")
        after = self.manifest([new], snapshot_id="missing-source")
        self.source.unlink()
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="missing-source")["status"], "uncertain")

    def test_non_private_file_or_parent_is_rejected(self):
        new, after = self.start()
        after.chmod(0o644)
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="after")["reason"], "PRIVATE_MANIFEST_AND_DIRECTORY_REQUIRED")
        directory = self.root / "non-private-manifests"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        after = self.manifest([new], snapshot_id="public-parent", directory=directory)
        self.assertEqual(self.verify(databases=[new], manifest=after, snapshot_id="public-parent")["reason"], "PRIVATE_MANIFEST_AND_DIRECTORY_REQUIRED")

    def test_symlink_manifest_or_parent_is_rejected(self):
        new, after = self.start()
        link = self.root / "manifest-link.json"
        link.symlink_to(after)
        self.assertEqual(self.verify(databases=[new], manifest=link, snapshot_id="after")["reason"], "MANIFEST_SYMLINK_FORBIDDEN")
        directory = self.root / "private-manifests"
        directory.mkdir(mode=0o700)
        after = self.manifest([new], snapshot_id="linked-parent", directory=directory)
        linked_directory = self.root / "linked-manifests"
        linked_directory.symlink_to(directory, target_is_directory=True)
        linked = linked_directory / after.name
        self.assertEqual(self.verify(databases=[new], manifest=linked, snapshot_id="linked-parent")["reason"], "MANIFEST_SYMLINK_FORBIDDEN")

    def test_manifest_does_not_bypass_history_or_schema_checks(self):
        new, after = self.start()
        with closing(sqlite3.connect(new)) as connection:
            connection.execute(f'UPDATE "{table(RECIPIENT)}" SET message_content=? WHERE local_id=1', ("changed fixture history",))
            connection.commit()
        after = self.manifest([new], snapshot_id="changed-history")
        result = self.verify(databases=[new], manifest=after, snapshot_id="changed-history")
        self.assertEqual(result["reason"], "BASELINE_HISTORY_CHANGED")

    def test_manifest_mode_cannot_be_added_or_removed_mid_operation(self):
        self.baseline()
        new_manifest = self.manifest([self.db], snapshot_id="fixture-snapshot")
        result = self.verify(manifest=new_manifest)
        self.assertEqual(result["reason"], "MANIFEST_MODE_CHANGED")
        self.state.unlink()
        self.baseline(manifest=new_manifest)
        self.assertEqual(self.verify()["reason"], "MANIFEST_MODE_CHANGED")


if __name__ == "__main__":
    unittest.main()
