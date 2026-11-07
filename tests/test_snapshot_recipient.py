"""Synthetic encrypted-source -> manifest -> exact-recipient receipt pipeline."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import snapshot_recipient as sampler
import verify_sent

SOURCE_ROOT = Path.home() / "WeChatDataAnalysis"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


@unittest.skipUnless((SOURCE_ROOT / "tools/test_skill_local_setup.py").is_file(), "Local decryptor fixture helpers required")
class SnapshotIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-receipt-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.fixtures = module("receipt_fixture_helpers", SOURCE_ROOT / "tools/test_skill_local_setup.py")
        self.adapter = self.fixtures.load("receipt_fixture_export", "export_wechat_archive.py")
        self.old_output = os.environ.get("WECHAT_TOOL_OUTPUT_DIR")
        self.old_console = os.environ.get("WECHAT_TOOL_ENABLE_CONSOLE_LOG")
        os.environ["WECHAT_TOOL_OUTPUT_DIR"] = str(self.root / "logs")
        os.environ["WECHAT_TOOL_ENABLE_CONSOLE_LOG"] = "0"
        self.key = bytes(range(32))
        self.keyfile = self.root / "fixture-key.txt"
        self.keyfile.write_text(self.key.hex())
        self.keyfile.chmod(0o600)
        self.account = self.root / "synthetic-account"
        self.source = self.account / "db_storage/message/message_0.db"
        self.plain = self.root / "synthetic-plaintext.db"
        self.table = "Msg_" + hashlib.md5(b"fixture_peer").hexdigest()
        c = self.fixtures.plain_database(self.plain, [
            ("CREATE TABLE Name2Id(user_name TEXT)", ()),
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(7,'fixture_self')", ()),
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(8,'fixture_peer')", ()),
            (f'CREATE TABLE "{self.table}"(local_id INTEGER PRIMARY KEY,server_id INT,create_time INT,real_sender_id INT,local_type INT,message_content BLOB)', ()),
            (f'INSERT INTO "{self.table}" VALUES(1,101,?,7,1,?)', (int(time.time())-100,b"existing fixture")),
        ])
        c.close()
        self.source.parent.mkdir(parents=True)
        shutil.copy2(self.plain, self.source)
        self.fixtures.encrypt_fixture(self.source, self.key)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"account_dir":str(self.account), "self_wxid":"fixture_self", "key_file":str(self.keyfile)}))
        self.config.chmod(0o600)

    def tearDown(self):
        for key, old in [("WECHAT_TOOL_OUTPUT_DIR", self.old_output),("WECHAT_TOOL_ENABLE_CONSOLE_LOG", self.old_console)]:
            if old is None: os.environ.pop(key, None)
            else: os.environ[key] = old
        self.temp.cleanup()

    def sample(self):
        return sampler.create_snapshot(self.config, self.root / "snapshots", adapter=self.adapter)

    def test_atomic_copies_share_source_lineage_and_verify_only_new_target(self):
        before = self.sample()
        self.assertEqual(before["database_count"],1)
        self.assertEqual(before["send_attempts"],0)
        baseline = self.root / "baseline.json"
        plan = verify_sent.create_baseline(before["databases"],baseline,"fixture_peer","fixture_self",
            verify_sent.hash_text("new synthetic fixture"),"synthetic-operation",snapshot_id=before["snapshot_id"],manifest=before["manifest"])
        self.assertEqual(plan["status"],"baseline_created",plan)
        committed = time.time()
        with sqlite3.connect(self.plain) as c:
            c.execute(f'INSERT INTO "{self.table}" VALUES(2,102,?,7,1,?)', (int(committed),b"new synthetic fixture"))
        self.fixtures.encrypt_fixture(self.source,self.key,main=self.plain.read_bytes())
        after = self.sample()
        self.assertNotEqual(before["databases"],after["databases"])
        result = verify_sent.verify(after["databases"],baseline,"fixture_peer","synthetic-operation",committed,
            snapshot_id=after["snapshot_id"],manifest=after["manifest"])
        self.assertEqual(result["status"],"verified",result)
        self.assertFalse(result["delivery_confirmed"])
        self.assertFalse(result["retry_allowed"])

    def test_no_new_message_does_not_report_success(self):
        before = self.sample()
        baseline = self.root / "baseline.json"
        verify_sent.create_baseline(before["databases"],baseline,"fixture_peer","fixture_self",
            verify_sent.hash_text("not sent anywhere"),"no-message",snapshot_id=before["snapshot_id"],manifest=before["manifest"])
        committed=time.time()
        after=self.sample()
        result=verify_sent.verify(after["databases"],baseline,"fixture_peer","no-message",committed,
            snapshot_id=after["snapshot_id"],manifest=after["manifest"])
        self.assertEqual(result["status"],"uncertain")
        self.assertFalse(result["retry_allowed"])

    def test_configuration_symlink_and_key_permissions_are_rejected(self):
        link=self.root/"config-link.json"
        link.symlink_to(self.config)
        with self.assertRaises(ValueError):
            sampler.create_snapshot(link,self.root/"snapshots",adapter=self.adapter)
        self.keyfile.chmod(0o644)
        with self.assertRaises(ValueError): self.sample()

    def test_source_overlap_is_rejected_without_changing_source_permissions(self):
        before=self.account.stat().st_mode
        for output in [self.account,self.account/'should-not-exist',self.root]:
            with self.assertRaises(ValueError):
                sampler.create_snapshot(self.config,output,adapter=self.adapter)
        self.assertEqual(self.account.stat().st_mode,before)
        self.assertFalse((self.account/'should-not-exist').exists())

    def test_existing_output_permissions_are_not_silently_changed(self):
        output=self.root/'non-private-output'
        output.mkdir(mode=0o755)
        output.chmod(0o755)
        with self.assertRaises(ValueError):
            sampler.create_snapshot(self.config,output,adapter=self.adapter)
        self.assertEqual(output.stat().st_mode&0o777,0o755)


if __name__=="__main__":
    unittest.main()
