#!/usr/bin/env python3
"""Exact recipient resolution from a fresh, local contact-only catalog.

The result identifies a catalog entry, never the current GUI conversation.
No UI actions, message-table access, key discovery, or sending take place here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from fresh_identity_catalog import open_fresh_catalog
from identity_catalog import CatalogError, ContactIdentity, _valid_identifier, _valid_text
from snapshot_recipient import DEFAULT_CONFIG, checked_file


_KINDS = frozenset({"person", "group", "system"})


def _validate_alias_map(value):
    if (not isinstance(value, dict)
            or any(not _valid_text(query) or not _valid_identifier(target)
                   for query, target in value.items())):
        raise CatalogError("INVALID_CONTACT_ALIAS_MAP")
    return dict(value)


def _unique_json_object(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise CatalogError("DUPLICATE_CONTACT_ALIAS_KEY")
        document[key] = value
    return document


def _alias_fingerprint(path):
    try:
        checked_file(path)
        before = path.stat(follow_symlinks=False)
        if before.st_size > 1_048_576:
            raise CatalogError("CONTACT_ALIASES_TOO_LARGE")
        raw = path.read_bytes()
        checked_file(path)
        after = path.stat(follow_symlinks=False)
        shape = lambda info: (info.st_dev, info.st_ino, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns)
        if shape(before) != shape(after):
            raise CatalogError("CONTACT_ALIASES_CHANGED")
        return (*shape(after), hashlib.sha256(raw).hexdigest()), raw
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("CONTACT_ALIASES_UNAVAILABLE") from None


@dataclass(frozen=True)
class _AliasConfiguration:
    path: Path
    aliases: dict
    fingerprint: tuple | None

    def assert_current(self):
        if self.fingerprint is None:
            try:
                self.path.lstat()
            except FileNotFoundError:
                return
            except OSError:
                raise CatalogError("CONTACT_ALIASES_UNAVAILABLE") from None
            raise CatalogError("CONTACT_ALIASES_CHANGED")
        current, _ = _alias_fingerprint(self.path)
        if current != self.fingerprint:
            raise CatalogError("CONTACT_ALIASES_CHANGED")


def _load_alias_configuration(config_file=DEFAULT_CONFIG, aliases_file=None):
    path = (Path(aliases_file).expanduser().absolute() if aliases_file is not None
            else Path(config_file).expanduser().absolute().parent / "contact-aliases.json")
    try:
        path.lstat()
    except FileNotFoundError:
        return _AliasConfiguration(path, {}, None)
    except OSError:
        raise CatalogError("CONTACT_ALIASES_UNAVAILABLE") from None
    fingerprint, raw = _alias_fingerprint(path)
    try:
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
        if (not isinstance(document, dict) or set(document) != {"version", "aliases"}
                or type(document["version"]) is not int or document["version"] != 1):
            raise CatalogError("INVALID_CONTACT_ALIASES_SCHEMA")
        aliases = _validate_alias_map(document["aliases"])
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("INVALID_CONTACT_ALIASES_SCHEMA") from None
    result = _AliasConfiguration(path, aliases, fingerprint)
    result.assert_current()
    return result


def load_alias_map(config_file=DEFAULT_CONFIG, *, aliases_file=None):
    """Read an optional user-maintained 0600 alias file; never write aliases."""
    return _load_alias_configuration(config_file, aliases_file).aliases


class _AliasBoundCatalog:
    """Preserve alias-file freshness alongside the existing contact proof."""
    def __init__(self, fresh, configuration):
        self._fresh, self._configuration = fresh, configuration
        self.catalog, self.account_id = fresh.catalog, fresh.account_id
        self.source_token = hashlib.sha256(json.dumps({"contact": fresh.source_token,
            "alias_file": str(configuration.path), "alias_fingerprint": configuration.fingerprint},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def assert_current(self):
        self._fresh.assert_current()
        self._configuration.assert_current()

    def resolve(self, query, *, kind="weixin_id"):
        self.assert_current()
        result = self._fresh.resolve(query, kind=kind)
        self.assert_current()
        return result


@dataclass(frozen=True)
class ContactResolution:
    query: str
    account_id: str
    source_token: str
    contacts: tuple[ContactIdentity, ...]
    matched_fields: tuple[str, ...]
    status: str
    error_code: str | None
    alias_applied: bool = False

    @property
    def matched(self):
        return self.status == "matched" and len(self.contacts) == 1

    @property
    def identity(self):
        return self.contacts[0] if self.matched else None

    @property
    def recipient_id(self):
        return self.identity.username if self.identity else None

    @property
    def search_terms(self):
        if self.identity is None:
            return ()
        item = self.identity
        # A configured ordinary name may belong to someone else in WeChat.
        # Search the mapped contact's actual fields, not that colliding name.
        values = ((item.alias, item.remark, item.username, item.nickname)
                  if self.alias_applied else
                  (self.query, item.remark, item.alias, item.nickname, item.username))
        return tuple(dict.fromkeys(value for value in values if _valid_text(value)))

    def to_private_dict(self):
        """Trusted local controller input; do not put this in public logs."""
        if not self.matched:
            raise CatalogError(self.error_code or "RECIPIENT_NOT_RESOLVED")
        item = self.identity
        return {"recipient_id": item.username, "kind": item.kind,
                "display_name": item.display_name, "nickname": item.nickname,
                "alias": item.alias, "remark": item.remark,
                "account_id": self.account_id, "search_text": self.search_terms[0],
                "search_terms": list(self.search_terms), "source_token": self.source_token,
                "evidence": "fresh_contact_catalog", "alias_applied": self.alias_applied,
                "ui_identity_confirmed": False}

    def to_public_dict(self):
        return {"status": self.status, "matched": self.matched,
                "error_code": self.error_code, "match_count": len(self.contacts),
                "matched_fields": list(self.matched_fields),
                "source_token": self.source_token, "evidence": "fresh_contact_catalog",
                "ui_identity_confirmed": False, "alias_applied": self.alias_applied,
                "identities": [{"recipient_hash": item.username_hash, "kind": item.kind}
                               for item in self.contacts]}


def resolve_from_catalog(fresh, query: str, *, allowed_kinds=_KINDS,
                         alias_map=None) -> ContactResolution:
    """Union exact matches across IDs, alias, remark and nickname.

    There is no priority that silently discards a different matching account.
    For example, one person's remark matching another person's alias is
    ambiguous. No trim, fuzzy match or Unicode normalization is performed.
    """
    if not _valid_text(query):
        raise CatalogError("INVALID_QUERY")
    allowed = frozenset(allowed_kinds)
    if not allowed or not allowed <= {"person", "group", "system", "official"}:
        raise CatalogError("INVALID_RECIPIENT_KINDS")
    aliases = _validate_alias_map({} if alias_map is None else alias_map)
    fresh.assert_current()
    direct_id = any(query == item.username or (item.alias and query == item.alias)
                    for item in fresh.catalog.contacts)
    if query in aliases and not direct_id:
        # Explicit local naming overrides display-name coincidences only. Raw
        # usernames/Weixin aliases remain governed by the exact catalog lookup.
        identities = tuple(item for item in fresh.catalog.contacts
                           if item.username == aliases[query])
        fresh.assert_current()
        if len(identities) != 1:
            status, code = ("not_found", "CONFIGURED_ALIAS_TARGET_NOT_FOUND") if not identities else (
                "ambiguous", "CONFIGURED_ALIAS_TARGET_NOT_UNIQUE")
        elif identities[0].kind not in allowed:
            status, code = "unsupported_kind", "CONFIGURED_ALIAS_TARGET_KIND_UNSUPPORTED"
        else:
            status, code = "matched", None
        return ContactResolution(query, fresh.account_id, fresh.source_token,
            identities, ("configured_alias",), status, code, alias_applied=True)
    matches, fields = {}, set()
    for item in fresh.catalog.contacts:
        hit = {name for name, value in (("username", item.username), ("alias", item.alias),
                                       ("remark", item.remark), ("nickname", item.nickname))
               if value and value == query}
        if hit:
            matches[item.username] = item
            fields.update(hit)
    identities = tuple(matches[key] for key in sorted(matches))
    fresh.assert_current()
    if len(identities) > 1:
        status, code = "ambiguous", "AMBIGUOUS_RECIPIENT"
    elif not identities:
        status, code = "not_found", "RECIPIENT_NOT_FOUND"
    elif identities[0].kind not in allowed:
        status, code = "unsupported_kind", "UNSUPPORTED_RECIPIENT_KIND"
    else:
        status, code = "matched", None
    return ContactResolution(query, fresh.account_id, fresh.source_token,
                             identities, tuple(sorted(fields)), status, code)


@contextmanager
def open_resolved_contact(query, config_file=DEFAULT_CONFIG, *, ttl_seconds=30.0,
                          allowed_kinds=_KINDS, output_root=None, adapter=None,
                          aliases_file=None):
    """Hold a fresh catalog for the operation; yield (resolution, freshness).

    Call freshness.assert_current() before each UI effect. A controller taking
    longer than the bounded lifetime must stop and start a new operation rather
    than silently replacing an in-flight identity proof.
    """
    kwargs = {"ttl_seconds": ttl_seconds, "adapter": adapter}
    if output_root is not None:
        kwargs["output_root"] = output_root
    with open_fresh_catalog(config_file, **kwargs) as fresh:
        aliases = _load_alias_configuration(config_file, aliases_file)
        bound = _AliasBoundCatalog(fresh, aliases)
        result = resolve_from_catalog(bound, query, allowed_kinds=allowed_kinds,
                                      alias_map=aliases.aliases)
        if not result.matched:
            raise CatalogError(result.error_code or "RECIPIENT_NOT_RESOLVED")
        yield result, bound


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        with open_fresh_catalog(args.config, ttl_seconds=30.0) as fresh:
            aliases = _load_alias_configuration(args.config)
            bound = _AliasBoundCatalog(fresh, aliases)
            result = resolve_from_catalog(bound, args.query, alias_map=aliases.aliases).to_public_dict()
    except CatalogError as error:
        result = {"status": "unavailable", "matched": False, "error_code": error.code,
                  "ui_identity_confirmed": False}
    except Exception:
        result = {"status": "unavailable", "matched": False,
                  "error_code": "CONTACT_RESOLUTION_FAILED", "ui_identity_confirmed": False}
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    return 0 if result.get("matched") else 2


if __name__ == "__main__":
    raise SystemExit(main())
