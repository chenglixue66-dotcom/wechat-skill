"""Synthetic contact-only tests. Never read real contacts, messages, keys or UI."""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/identity_catalog.py"
spec = importlib.util.spec_from_file_location("synthetic_identity_catalog", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def database(path, rows, *, columns="username TEXT,remark TEXT,nick_name TEXT,alias TEXT"):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE contact(" + columns + ")")
        if rows:
            connection.executemany("INSERT INTO contact VALUES(?,?,?,?)", rows)
        connection.commit()


class IdentityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechat-catalog-synthetic-")
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "contact.db"
        self.rows = [
            ("wxid_fixture_a", "Same Label", "Unused Nick A", "fixture_alias_a"),
            ("12345@chatroom", "Same Label", "Unused Group Nick", ""),
            ("wxid_fixture_b", "Preferred Remark", "Hidden By Remark", "fixture_alias_b"),
            ("wxid_fixture_c", "", "Nickname Fallback", ""),
            ("gh_fixture", "Official", "", "official_alias"),
            ("filehelper", "Files", "", ""),
            ("fixture@openim", "Unclassified", "", ""),
        ]
        database(self.db, self.rows)

    def tearDown(self):
        self.temporary.cleanup()

    def load(self):
        return module.IdentityCatalog.from_db(self.db)

    def test_exact_username_and_same_name_friend_group_ambiguity(self):
        catalog = self.load()
        result = catalog.lookup_username("wxid_fixture_a")
        self.assertTrue(result.matched)
        self.assertEqual(result.identity.kind, "person")
        ambiguous = catalog.resolve_display_name("Same Label")
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual({item.kind for item in ambiguous.contacts}, {"person", "group"})
        self.assertIsNone(ambiguous.identity)
        self.assertEqual(catalog.lookup_username("WXID_FIXTURE_A").status, "not_found")

    def test_remark_has_priority_over_nickname(self):
        catalog = self.load()
        self.assertEqual(catalog.resolve_display_name("Preferred Remark").identity.username, "wxid_fixture_b")
        self.assertEqual(catalog.resolve_display_name("Hidden By Remark").status, "not_found")
        self.assertEqual(catalog.resolve_display_name("Nickname Fallback").identity.username, "wxid_fixture_c")
        self.assertEqual(catalog.resolve_display_name(" Preferred Remark").status, "not_found")

    def test_gui_weixin_id_matches_username_or_alias_exactly(self):
        catalog = self.load()
        for value in ("wxid_fixture_a", "fixture_alias_a"):
            result = catalog.match_weixin_id(value)
            self.assertTrue(result.matched)
            self.assertEqual(result.identity.username, "wxid_fixture_a")
            self.assertEqual(result.evidence, "catalog")
            self.assertFalse(result.ui_identity_confirmed)
        self.assertEqual(catalog.match_weixin_id("fixture_alias").status, "not_found")
        self.assertEqual(catalog.match_weixin_id("Weixin ID: fixture_alias_a").status, "invalid_query")

    def test_alias_conflicts_remain_ambiguous(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_fixture_conflict", "Another", "", "fixture_alias_a"))
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_fixture_cross", "Cross", "", "wxid_fixture_b"))
            connection.commit()
        catalog = self.load()
        self.assertEqual(catalog.match_weixin_id("fixture_alias_a").status, "ambiguous")
        self.assertEqual(catalog.match_weixin_id("wxid_fixture_b").status, "ambiguous")
        self.assertEqual(catalog.lookup_username("wxid_fixture_b").status, "matched")

    def test_same_username_and_alias_is_one_identity(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE contact SET alias=username WHERE username=?", ("wxid_fixture_a",))
            connection.commit()
        result = self.load().match_weixin_id("wxid_fixture_a")
        self.assertTrue(result.matched)
        self.assertEqual(len(result.contacts), 1)

    def test_kind_classification_does_not_assert_ui_identity(self):
        catalog = self.load()
        for username, kind in [("12345@chatroom", "group"), ("gh_fixture", "official"), ("filehelper", "system"), ("fixture@openim", "unknown")]:
            result = catalog.lookup_username(username)
            self.assertEqual(result.identity.kind, kind)
            self.assertFalse(result.identity.ui_identity_confirmed)
            self.assertEqual(result.identity.classification_basis, "identifier_convention")

    def test_empty_fields_and_unusable_remark_do_not_guess_nickname(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executemany("INSERT INTO contact VALUES(?,?,?,?)", [
                ("wxid_empty", None, None, None),
                ("wxid_hidden_remark", "\u200b", "Do Not Fall Back", ""),
            ])
            connection.commit()
        catalog = self.load()
        self.assertTrue(catalog.lookup_username("wxid_empty").matched)
        self.assertIsNone(catalog.lookup_username("wxid_empty").identity.display_name)
        self.assertEqual(catalog.resolve_display_name("Do Not Fall Back").status, "not_found")
        for value in (None, "", " ", "\u200b"):
            self.assertEqual(catalog.match_weixin_id(value).status, "invalid_query")
            self.assertEqual(catalog.resolve_display_name(value).status, "invalid_query")

    def test_hidden_unicode_is_rejected_without_normalization(self):
        catalog = self.load()
        for hidden in ("\u200b", "\u200e", "\u2060", "\ufeff", "\u034f", "\ufe0f"):
            self.assertEqual(catalog.match_weixin_id("fixture_alias_a" + hidden).status, "invalid_query")
            self.assertEqual(catalog.resolve_display_name("Preferred Remark" + hidden).status, "invalid_query")

    def test_missing_columns_or_wrong_database_are_errors(self):
        missing = self.root / "missing-columns.db"
        database(missing, [], columns="username TEXT,remark TEXT")
        with self.assertRaisesRegex(module.CatalogError, "CONTACT_COLUMNS_MISSING"):
            module.IdentityCatalog.from_db(missing)
        wrong = self.root / "wrong-schema.db"
        with closing(sqlite3.connect(wrong)) as connection:
            connection.execute("CREATE TABLE unrelated(id INT)")
            connection.commit()
        with self.assertRaisesRegex(module.CatalogError, "CONTACT_TABLE_MISSING"):
            module.IdentityCatalog.from_db(wrong)
        encrypted = self.root / "not-sqlite.db"
        encrypted.write_bytes(b"synthetic opaque bytes")
        with self.assertRaisesRegex(module.CatalogError, "PLAINTEXT_SQLITE_REQUIRED"):
            module.IdentityCatalog.from_db(encrypted)

    def test_malformed_catalog_identifiers_are_not_guessed(self):
        for i, row in enumerate([("", "Label", "", ""), ("wxid_hidden\u200b", "Label", "", ""), ("wxid_valid", "Label", "", "alias\u200b")]):
            path = self.root / f"malformed-{i}.db"
            database(path, [row])
            with self.assertRaises(module.CatalogError):
                module.IdentityCatalog.from_db(path)

    def test_source_is_readonly_and_nonexistent_file_is_not_created(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.db.chmod(0o400)
        catalog = self.load()
        catalog.lookup_username("wxid_fixture_a")
        catalog.resolve_display_name("Same Label")
        catalog.match_weixin_id("fixture_alias_a")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o400)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.db) + suffix).exists())
        missing = self.root / "absent.db"
        with self.assertRaisesRegex(module.CatalogError, "DATABASE_UNAVAILABLE"):
            module.IdentityCatalog.from_db(missing)
        self.assertFalse(missing.exists())

    def test_replacement_or_content_change_requires_new_token(self):
        catalog = self.load()
        replacement = self.root / "replacement.db"
        shutil.copy2(self.db, replacement)
        os.replace(replacement, self.db)
        self.assertEqual(catalog.match_weixin_id("fixture_alias_a").status, "stale_catalog")
        next_catalog = self.load()
        self.assertNotEqual(catalog.catalog_token, next_catalog.catalog_token)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE contact SET remark=? WHERE username=?", ("Changed Fixture", "wxid_fixture_a"))
            connection.commit()
        self.assertEqual(next_catalog.lookup_username("wxid_fixture_a").status, "stale_catalog")
        self.assertNotEqual(next_catalog.catalog_token, self.load().catalog_token)

    def test_generation_symlink_rebinding_is_not_silently_accepted(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        shutil.copy2(self.db, first / "contact.db")
        shutil.copy2(self.db, second / "contact.db")
        pointer = self.root / "current"
        pointer.symlink_to(first, target_is_directory=True)
        catalog = module.IdentityCatalog.from_db(pointer / "contact.db")
        pointer.unlink()
        pointer.symlink_to(second, target_is_directory=True)
        self.assertEqual(catalog.match_weixin_id("fixture_alias_a").status, "stale_catalog")

    def test_cli_redacts_all_raw_identity_and_name_fields(self):
        process = subprocess.run([sys.executable, str(SCRIPT), "--db", str(self.db),
                                  "match-weixin-id", "--value", "fixture_alias_a"],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0)
        result = json.loads(process.stdout)
        self.assertTrue(result["matched"])
        self.assertEqual(result["evidence"], "catalog")
        self.assertFalse(result["ui_identity_confirmed"])
        self.assertEqual(process.stderr, "")
        for secret in ("wxid_fixture_a", "fixture_alias_a", "Same Label", "Unused Nick A", str(self.db)):
            self.assertNotIn(secret, process.stdout)
        invalid = subprocess.run([sys.executable, str(SCRIPT), "--db", str(self.root / "absent.db"),
                                  "lookup-username", "--value", "wxid_fixture_a"],
                                 capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["error_code"], "DATABASE_UNAVAILABLE")
        self.assertEqual(invalid.stderr, "")

    def test_account_display_is_full_nickname_alias_not_remark(self):
        catalog = self.load()
        result = catalog.match_account_display("Unused Nick A fixture_alias_a")
        self.assertTrue(result.matched)
        self.assertEqual(result.identity.username, "wxid_fixture_a")
        self.assertEqual(result.evidence, "catalog")
        self.assertFalse(result.ui_identity_confirmed)
        self.assertEqual(catalog.match_account_display("Same Label fixture_alias_a").status, "not_found")
        self.assertEqual(catalog.match_account_display("Unused Nick A fixture_alias").status, "not_found")
        self.assertEqual(catalog.match_account_display("Unused Nick A FIXTURE_ALIAS_A").status, "not_found")

    def test_account_display_requires_alias_valid_nickname_and_person(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executemany("INSERT INTO contact VALUES(?,?,?,?)", [
                ("wxid_no_alias", "", "Visible Nick", ""),
                ("wxid_no_nick", "Remark Must Not Replace Nick", "", "visible_alias"),
                ("another@chatroom", "", "Visible Group", "group_alias"),
            ])
            connection.execute("UPDATE contact SET nick_name=?,alias=? WHERE username=?", ("Official Nick", "official_alias", "gh_fixture"))
            connection.execute("UPDATE contact SET nick_name=?,alias=? WHERE username=?", ("System Nick", "system_alias", "filehelper"))
            connection.commit()
        catalog = self.load()
        for raw in ("Visible Nick ", " visible_alias", "Visible Group group_alias",
                    "Official Nick official_alias", "System Nick system_alias"):
            self.assertEqual(catalog.match_account_display(raw).status, "not_found")
        for raw in (None, "", " "):
            self.assertEqual(catalog.match_account_display(raw).status, "invalid_query")

    def test_account_display_nickname_spaces_are_not_split_or_trimmed(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_spaced", "Ignored", "  Given Middle Family ", "spaced_alias"))
            connection.commit()
        catalog = self.load()
        raw = "  Given Middle Family  spaced_alias"
        self.assertEqual(catalog.match_account_display(raw).identity.username, "wxid_spaced")
        for guessed in (raw.strip(), "Given Middle Family spaced_alias", "Family spaced_alias"):
            self.assertEqual(catalog.match_account_display(guessed).status, "not_found")

    def test_account_display_hidden_characters_never_get_removed(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_hidden_nick", "", "Hidden\u200bNick", "hidden_alias"))
            connection.commit()
        catalog = self.load()
        self.assertEqual(catalog.match_account_display("Hidden\u200bNick hidden_alias").status, "invalid_query")
        self.assertEqual(catalog.match_account_display("HiddenNick hidden_alias").status, "not_found")
        self.assertEqual(catalog.match_account_display("Unused Nick A fixture_alias_a\u2060").status, "invalid_query")

    def test_account_display_collisions_are_ambiguous_across_all_contacts(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_same_account_field", "Different Remark", "Unused Nick A", "fixture_alias_a"))
            connection.commit()
        result = self.load().match_account_display("Unused Nick A fixture_alias_a")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.contacts), 2)
        self.assertIsNone(result.identity)

    def test_account_display_same_nickname_different_aliases_stay_distinct(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_same_nick_other_alias", "", "Unused Nick A", "other_alias"))
            connection.commit()
        catalog = self.load()
        self.assertEqual(catalog.match_account_display("Unused Nick A other_alias").identity.username, "wxid_same_nick_other_alias")
        self.assertEqual(catalog.match_account_display("Unused Nick A fixture_alias_a").identity.username, "wxid_fixture_a")

    def test_account_display_different_nickname_cannot_disambiguate_shared_alias(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_shared_alias", "", "Different Nick", "fixture_alias_a"))
            connection.commit()
        result = self.load().match_account_display("Unused Nick A fixture_alias_a")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({item.username for item in result.contacts}, {"wxid_fixture_a", "wxid_shared_alias"})
        self.assertIsNone(result.identity)
        self.assertEqual(result.evidence, "catalog")
        self.assertFalse(result.ui_identity_confirmed)

    def test_account_display_alias_username_cross_collision_is_ambiguous(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("fixture_alias_a", "", "Different Nick", "different_alias"))
            connection.commit()
        result = self.load().match_account_display("Unused Nick A fixture_alias_a")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({item.username for item in result.contacts}, {"wxid_fixture_a", "fixture_alias_a"})
        self.assertIsNone(result.identity)
        self.assertFalse(result.ui_identity_confirmed)


if __name__ == "__main__":
    unittest.main()
