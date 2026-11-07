"""Synthetic encrypted contacts: freshness survives no account/source drift."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fresh_identity_catalog as fresh
from identity_catalog import CatalogError

ROOT = Path.home() / "WeChatDataAnalysis"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


@unittest.skipUnless((ROOT / "tools/test_skill_local_setup.py").is_file(), "Local decryptor fixtures required")
class FreshCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-catalog-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.fixture = module("fresh_catalog_fixture", ROOT / "tools/test_skill_local_setup.py")
        self.adapter = self.fixture.load("fresh_catalog_adapter", "export_wechat_archive.py")
        self.original_paths = self.adapter.SNAPSHOT_DIR, self.adapter.DECRYPTED_DIR
        self.key = bytes(range(32))
        self.keyfile = self.root / "key.txt"
        self.keyfile.write_text(self.key.hex())
        self.keyfile.chmod(0o600)
        self.account = self.root / "account"
        self.source = self.account / "db_storage/contact/contact.db"
        self.source.parent.mkdir(parents=True)
        connection = self.fixture.plain_database(self.source, [
            ("CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT)", ()),
            ("INSERT INTO contact VALUES('wxid_fixture_self','','Synthetic Self','fixture_self_alias')", ()),
            ("INSERT INTO contact VALUES('wxid_fixture_peer','Target','Synthetic Peer','fixture_peer_alias')", ())])
        connection.close()
        self.fixture.encrypt_fixture(self.source, self.key)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"account_dir": str(self.account),
            "self_wxid": "wxid_fixture_self", "key_file": str(self.keyfile)}))
        self.config.chmod(0o600)
        self.output = self.root / "private-output"

    def tearDown(self):
        self.temp.cleanup()

    def open(self, **kwargs):
        return fresh.open_fresh_catalog(self.config, self.output, adapter=self.adapter, **kwargs)

    def assert_clean(self):
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((self.adapter.SNAPSHOT_DIR, self.adapter.DECRYPTED_DIR), self.original_paths)

    def test_exact_private_mapping_and_lifetime_cleanup(self):
        with self.open() as catalog:
            result = catalog.resolve("fixture_peer_alias")
            self.assertTrue(result.matched)
            self.assertEqual(result.identity.username, "wxid_fixture_peer")
            self.assertFalse(result.ui_identity_confirmed)
            self.assertFalse(catalog.resolve(" fixture_peer_alias").matched)
            self.assertEqual(catalog.account_id, "wxid_fixture_self")
        self.assert_clean()
        with self.assertRaisesRegex(CatalogError, "CLOSED"):
            catalog.assert_current()

    def test_source_database_mutation_stops_mapping(self):
        with self.open() as catalog:
            with self.source.open("r+b") as stream:
                stream.seek(128)
                stream.write(b"changed!")
            with self.assertRaisesRegex(CatalogError, "SOURCE_CHANGED"):
                catalog.resolve("fixture_peer_alias")
        self.assert_clean()

    def test_new_wal_is_not_ignored(self):
        with self.open() as catalog:
            Path(str(self.source) + "-wal").write_bytes(b"new source WAL")
            with self.assertRaisesRegex(CatalogError, "SOURCE_CHANGED"):
                catalog.assert_current()
        self.assert_clean()

    def test_config_mutation_and_permissions_stop_mapping(self):
        for change in ("content", "permissions"):
            with self.subTest(change=change):
                with self.open() as catalog:
                    if change == "content":
                        self.config.write_text(self.config.read_text() + " ")
                    else:
                        self.config.chmod(0o644)
                    with self.assertRaises(CatalogError):
                        catalog.assert_current()
                self.config.chmod(0o600)
        self.assert_clean()

    def test_expiry_rejects_stale_catalog(self):
        with self.open() as catalog:
            with patch.object(fresh.time, "monotonic", return_value=catalog._expires):
                with self.assertRaisesRegex(CatalogError, "EXPIRED"):
                    catalog.assert_current()
        self.assert_clean()

    def test_context_exception_removes_plaintext(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with self.open():
                raise RuntimeError("synthetic failure")
        self.assert_clean()

    def test_output_overlap_rejected_before_modification(self):
        original = self.account.stat().st_mode
        with self.assertRaisesRegex(CatalogError, "OVERLAP"):
            with fresh.open_fresh_catalog(self.config, self.account / "unused", adapter=self.adapter):
                self.fail("overlap accepted")
        self.assertFalse((self.account / "unused").exists())
        self.assertEqual(self.account.stat().st_mode, original)

    def test_bad_ttl_and_key_rejected_without_retaining_copy(self):
        for value in (False, 0, -1, 31, float("nan"), float("inf")):
            with self.assertRaisesRegex(CatalogError, "INVALID_CATALOG_TTL"):
                with self.open(ttl_seconds=value):
                    self.fail("invalid TTL accepted")
        self.keyfile.write_text("invalid synthetic key")
        with self.assertRaises(CatalogError):
            with self.open():
                self.fail("invalid key accepted")
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
