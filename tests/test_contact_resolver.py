"""Synthetic recipient collisions and freshness; no live contacts or UI."""
from pathlib import Path
from contextlib import contextmanager
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from contact_resolver import load_alias_map, open_resolved_contact, resolve_from_catalog
from identity_catalog import CatalogError, IdentityCatalog


class FakeFresh:
    account_id = "wxid_self_fixture"
    source_token = "synthetic_source_token"

    def __init__(self, catalog):
        self.catalog = catalog
        self.stale = False

    def assert_current(self):
        if self.stale:
            raise CatalogError("FRESH_CATALOG_EXPIRED")
        self.catalog.assert_current()


class ContactResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-recipient-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "contacts.db"
        self.config = self.root / "config.json"
        self.aliases = self.root / "contact-aliases.json"

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self, rows):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT)")
            db.executemany("INSERT INTO contact VALUES(?,?,?,?)", rows)
        return FakeFresh(IdentityCatalog.from_db(self.path))

    def write_aliases(self, mapping):
        self.aliases.write_text(json.dumps({"version": 1, "aliases": mapping}))
        self.aliases.chmod(0o600)

    def test_all_exact_fields_and_unicode_are_supported(self):
        fresh = self.catalog([("wxid_peer_fixture", "犇犇", "Peer Nick", "peer_fixture")])
        for field in ("wxid_peer_fixture", "peer_fixture", "犇犇", "Peer Nick"):
            with self.subTest(query=field):
                result = resolve_from_catalog(fresh, field)
                self.assertTrue(result.matched)
                self.assertEqual(result.recipient_id, "wxid_peer_fixture")
                private = result.to_private_dict()
                self.assertEqual(private["search_text"], field)
                self.assertFalse(private["ui_identity_confirmed"])
                public = result.to_public_dict()
                self.assertNotIn("wxid_peer_fixture", str(public))
        self.assertFalse(resolve_from_catalog(fresh, " peer_fixture").matched)

    def test_nickname_remains_resolvable_when_remark_exists(self):
        fresh = self.catalog([("wxid_peer_fixture", "Saved Remark", "Original Nick", "")])
        self.assertTrue(resolve_from_catalog(fresh, "Original Nick").matched)

    def test_alias_name_collision_never_prioritizes_one_person(self):
        fresh = self.catalog([("wxid_a_fixture", "Target", "A", "alias_a"),
                              ("wxid_b_fixture", "B", "B", "Target")])
        result = resolve_from_catalog(fresh, "Target")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.contacts), 2)
        self.assertIsNone(result.recipient_id)
        with self.assertRaisesRegex(CatalogError, "AMBIGUOUS_RECIPIENT"):
            result.to_private_dict()

    def test_username_alias_collision_remains_ambiguous(self):
        fresh = self.catalog([("wxid_a_fixture", "A", "A", "wxid_b_fixture"),
                              ("wxid_b_fixture", "B", "B", "")])
        self.assertEqual(resolve_from_catalog(fresh, "wxid_b_fixture").status, "ambiguous")

    def test_same_contact_matching_multiple_fields_is_one_identity(self):
        fresh = self.catalog([("wxid_peer_fixture", "Same", "Same", "Same")])
        result = resolve_from_catalog(fresh, "Same")
        self.assertTrue(result.matched)
        self.assertEqual(result.matched_fields, ("alias", "nickname", "remark"))

    def test_official_accounts_require_explicit_kind_opt_in(self):
        fresh = self.catalog([("gh_fixture", "Official", "Official", "")])
        result = resolve_from_catalog(fresh, "Official")
        self.assertEqual(result.status, "unsupported_kind")
        self.assertTrue(resolve_from_catalog(fresh, "Official", allowed_kinds={"official"}).matched)

    def test_stale_or_modified_catalog_cannot_resolve(self):
        fresh = self.catalog([("wxid_peer_fixture", "Peer", "Peer", "")])
        fresh.stale = True
        with self.assertRaisesRegex(CatalogError, "EXPIRED"):
            resolve_from_catalog(fresh, "Peer")
        fresh.stale = False
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE contact SET remark='Changed'")
        with self.assertRaisesRegex(CatalogError, "SOURCE_CHANGED"):
            resolve_from_catalog(fresh, "Peer")

    def test_explicit_local_name_overrides_coincidental_nickname(self):
        fresh = self.catalog([("wxid_nickname_fixture", "", "Buddy", "nickname_alias"),
                              ("wxid_target_fixture", "Real Target", "Other Nick", "target_alias")])
        self.assertEqual(resolve_from_catalog(fresh, "Buddy").recipient_id, "wxid_nickname_fixture")
        result = resolve_from_catalog(fresh, "Buddy", alias_map={"Buddy": "wxid_target_fixture"})
        self.assertEqual(result.recipient_id, "wxid_target_fixture")
        self.assertTrue(result.alias_applied)
        self.assertEqual(result.search_terms[0], "target_alias")
        self.assertNotEqual(result.to_private_dict()["search_text"], "Buddy")
        self.assertEqual(result.matched_fields, ("configured_alias",))
        self.assertNotIn("wxid_target_fixture", str(result.to_public_dict()))

    def test_stale_configured_target_never_falls_back_to_matching_nickname(self):
        fresh = self.catalog([("wxid_nickname_fixture", "", "Buddy", "nickname_alias")])
        result = resolve_from_catalog(fresh, "Buddy", alias_map={"Buddy": "wxid_missing_fixture"})
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.error_code, "CONFIGURED_ALIAS_TARGET_NOT_FOUND")
        self.assertIsNone(result.recipient_id)
        self.assertEqual(result.search_terms, ())

    def test_wrong_kind_or_nonunique_configured_target_does_not_fall_back(self):
        fresh = self.catalog([("wxid_nickname_fixture", "", "Buddy", "nickname_alias"),
                              ("fixture@chatroom", "Group", "Group", "")])
        result = resolve_from_catalog(fresh, "Buddy", allowed_kinds={"person"},
                                      alias_map={"Buddy": "fixture@chatroom"})
        self.assertEqual(result.error_code, "CONFIGURED_ALIAS_TARGET_KIND_UNSUPPORTED")
        self.assertIsNone(result.recipient_id)
        # Corrupt in-memory provider fixtures still must not turn duplicate
        # username entries into an apparently unique configured target.
        item = fresh.catalog.contacts[0]
        fresh.catalog.contacts = (*fresh.catalog.contacts, item)
        result = resolve_from_catalog(fresh, "Buddy", alias_map={"Buddy": item.username})
        self.assertEqual(result.error_code, "CONFIGURED_ALIAS_TARGET_NOT_UNIQUE")

    def test_direct_username_and_weixin_alias_are_not_hijacked_by_name_map(self):
        fresh = self.catalog([("wxid_direct_fixture", "Direct", "Direct", "direct_alias"),
                              ("wxid_other_fixture", "Other", "Other", "other_alias")])
        for query in ("wxid_direct_fixture", "direct_alias"):
            with self.subTest(query=query):
                result = resolve_from_catalog(fresh, query,
                                              alias_map={query: "wxid_other_fixture"})
                self.assertEqual(result.recipient_id, "wxid_direct_fixture")
                self.assertFalse(result.alias_applied)

    def test_optional_alias_file_is_private_and_schema_versioned(self):
        self.assertEqual(load_alias_map(self.config), {})
        mapping = {"Buddy": "wxid_target_fixture"}
        self.write_aliases(mapping)
        self.assertEqual(load_alias_map(self.config), mapping)
        self.aliases.chmod(0o644)
        with self.assertRaisesRegex(CatalogError, "CONTACT_ALIASES_UNAVAILABLE"):
            load_alias_map(self.config)
        self.aliases.chmod(0o600)
        with patch("snapshot_recipient.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(CatalogError, "CONTACT_ALIASES_UNAVAILABLE"):
                load_alias_map(self.config)
        link = self.root / "alias-link.json"
        link.symlink_to(self.aliases)
        with self.assertRaisesRegex(CatalogError, "CONTACT_ALIASES_UNAVAILABLE"):
            load_alias_map(self.config, aliases_file=link)

    def test_invalid_or_duplicate_alias_configuration_is_not_silently_accepted(self):
        documents = [
            '{"version":true,"aliases":{}}',
            '{"version":2,"aliases":{}}',
            '{"version":1,"aliases":{"Buddy":"invalid target id"}}',
            '{"version":1,"aliases":{"Buddy":false}}',
            '{"version":1,"aliases":{},"extra":true}',
            '{"version":1,"aliases":{"Buddy":"wxid_a_fixture","Buddy":"wxid_b_fixture"}}',
        ]
        for document in documents:
            with self.subTest(document=document):
                self.aliases.write_text(document)
                self.aliases.chmod(0o600)
                with self.assertRaises(CatalogError):
                    load_alias_map(self.config)

    def test_open_context_loads_alias_and_binds_configuration_freshness(self):
        fresh = self.catalog([("wxid_nickname_fixture", "", "Buddy", "nickname_alias"),
                              ("wxid_target_fixture", "Real Target", "Target", "target_alias")])
        self.write_aliases({"Buddy": "wxid_target_fixture"})

        @contextmanager
        def provider(*args, **kwargs):
            yield fresh

        with patch("contact_resolver.open_fresh_catalog", provider):
            with open_resolved_contact("Buddy", self.config) as (result, bound):
                self.assertEqual(result.recipient_id, "wxid_target_fixture")
                bound.assert_current()
                self.write_aliases({"Buddy": "wxid_nickname_fixture"})
                with self.assertRaisesRegex(CatalogError, "CONTACT_ALIASES_CHANGED"):
                    bound.assert_current()

    def test_new_alias_file_during_operation_invalidates_absent_file_proof(self):
        fresh = self.catalog([("wxid_target_fixture", "Target", "Target", "target_alias")])

        @contextmanager
        def provider(*args, **kwargs):
            yield fresh

        with patch("contact_resolver.open_fresh_catalog", provider):
            with open_resolved_contact("Target", self.config) as (result, bound):
                self.assertFalse(result.alias_applied)
                self.write_aliases({"Target": "wxid_target_fixture"})
                with self.assertRaisesRegex(CatalogError, "CONTACT_ALIASES_CHANGED"):
                    bound.assert_current()


if __name__ == "__main__":
    unittest.main()
