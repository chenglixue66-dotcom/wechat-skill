#!/usr/bin/env python3
"""Exact, read-only contact-catalog resolution; never UI identity proof.

The explicit input must be an independent plaintext SQLite copy. This module
does not decrypt, read a key, inspect chat tables, access WeChat UI or send.
Catalog tokens bind the requested/resolved path, device/inode, schema metadata,
file metadata and SHA256. Every lookup rejects a changed or replaced source;
the caller must explicitly load a new catalog and obtain a new token.

Raw identifiers/aliases are returned only in Python objects or the explicitly
named to_private_dict() method. The CLI always uses hash-only public results.
Exact means no stripping, case folding, Unicode normalization, fuzzy names,
partial matches or removal of hidden characters. A display label uses nonempty
remark before nickname, even if that remark is unusable. Identifier kinds are
catalog conventions, not proof of the current UI's selected conversation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unicodedata


_COLUMNS = {"username", "remark", "nick_name", "alias"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_@.\-]{1,512}\Z")
_SYSTEM = frozenset({"filehelper", "weixin", "fmessage", "medianote", "floatbottle",
                     "newsapp", "qqmail", "tmessage", "qmessage", "weibo",
                     "weixinreminder", "notification_messages", "brandsessionholder", "masssendapp"})
_SIDECARS = ("-wal", "-shm", "-journal")


class CatalogError(RuntimeError):
    """Fixed error code; never contains an identifier, a path or source text."""

    @property
    def code(self) -> str:
        return str(self)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def _hidden(value: str) -> bool:
    for character in value:
        number = ord(character)
        if unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            return True
        # Default-ignorable characters outside Unicode's format category.
        if (number in {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0}
                or 0x180B <= number <= 0x180F or 0xFE00 <= number <= 0xFE0F
                or 0xE0000 <= number <= 0xE0FFF):
            return True
    return False


def _valid_text(value) -> bool:
    return isinstance(value, str) and bool(value) and bool(value.strip()) and not _hidden(value)


def _valid_identifier(value) -> bool:
    return _valid_text(value) and _IDENTIFIER.fullmatch(value) is not None


def _kind(username: str) -> str:
    if username.endswith("@chatroom") and len(username) > len("@chatroom"):
        return "group"
    if username.startswith("gh_") and len(username) > 3:
        return "official"
    if username in _SYSTEM:
        return "system"
    if "@" in username or username in {"gh_", "wxid_"}:
        return "unknown"
    return "person"


def _metadata(path: Path) -> tuple[int, ...]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise CatalogError("REGULAR_DATABASE_REQUIRED")
    if any(Path(str(path) + suffix).exists() for suffix in _SIDECARS):
        raise CatalogError("STANDALONE_DATABASE_REQUIRED")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class CatalogSource:
    requested_path: str
    resolved_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    schema_version: int
    user_version: int
    contact_schema_hash: str
    content_sha256: str


@dataclass(frozen=True)
class ContactIdentity:
    username: str
    alias: str
    remark: str
    nickname: str
    display_name: str | None
    kind: str
    catalog_token: str
    evidence: str = "catalog"
    ui_identity_confirmed: bool = False
    classification_basis: str = "identifier_convention"

    @property
    def username_hash(self) -> str:
        return _hash(self.username)

    def to_private_dict(self) -> dict:
        """For trusted in-process consumers; never used by the CLI."""
        return asdict(self)


@dataclass(frozen=True)
class CatalogResult:
    status: str
    contacts: tuple[ContactIdentity, ...]
    error_code: str | None
    catalog_token: str
    query_kind: str
    evidence: str = "catalog"
    ui_identity_confirmed: bool = False

    @property
    def matched(self) -> bool:
        return self.status == "matched" and len(self.contacts) == 1

    @property
    def identity(self) -> ContactIdentity | None:
        return self.contacts[0] if self.matched else None

    def to_public_dict(self) -> dict:
        return {"status": self.status, "matched": self.matched,
                "match_count": len(self.contacts), "error_code": self.error_code,
                "catalog_token": self.catalog_token, "query_kind": self.query_kind,
                "identities": [{"username_hash": item.username_hash, "kind": item.kind,
                                "alias_present": bool(item.alias),
                                "display_name_usable": item.display_name is not None}
                               for item in self.contacts],
                "evidence": "catalog", "ui_identity_confirmed": False}

    def to_private_dict(self) -> dict:
        return {**self.to_public_dict(), "contacts": [item.to_private_dict() for item in self.contacts]}


class IdentityCatalog:
    def __init__(self, source: CatalogSource, contacts: tuple[ContactIdentity, ...], catalog_token: str):
        self.source, self.contacts, self.catalog_token = source, contacts, catalog_token

    @classmethod
    def from_db(cls, db_path) -> IdentityCatalog:
        try:
            requested = Path(db_path).expanduser().absolute()
            resolved = requested.resolve(strict=True)
            before = _metadata(resolved)
            with resolved.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    raise CatalogError("PLAINTEXT_SQLITE_REQUIRED")
            digest = _file_hash(resolved)
            # immutable avoids SQLite creating WAL/SHM files for a readonly copy.
            with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", ("contact",)).fetchone()
                if schema is None:
                    raise CatalogError("CONTACT_TABLE_MISSING")
                columns = {row[1].lower() for row in connection.execute('PRAGMA table_info("contact")')}
                if not _COLUMNS <= columns:
                    raise CatalogError("CONTACT_COLUMNS_MISSING")
                schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                rows = connection.execute('SELECT username,remark,nick_name,alias FROM "contact"').fetchall()
            if before != _metadata(resolved) or requested.resolve(strict=True) != resolved:
                raise CatalogError("SOURCE_CHANGED_DURING_READ")
            source = CatalogSource(str(requested), str(resolved), *before, int(schema_version),
                                   int(user_version), _hash(schema[0] or ""), digest)
            token = _hash(json.dumps(asdict(source), separators=(",", ":"), sort_keys=True))
            contacts, seen = [], set()
            for username, remark, nickname, alias in rows:
                if not _valid_identifier(username):
                    raise CatalogError("INVALID_CATALOG_USERNAME")
                if username in seen:
                    raise CatalogError("DUPLICATE_CATALOG_USERNAME")
                seen.add(username)
                values = ["" if value is None else value for value in (remark, nickname, alias)]
                if any(not isinstance(value, str) for value in values):
                    raise CatalogError("INVALID_CATALOG_FIELD")
                remark, nickname, alias = values
                if alias and not _valid_identifier(alias):
                    raise CatalogError("INVALID_CATALOG_ALIAS")
                selected = remark if remark != "" else nickname
                display_name = selected if _valid_text(selected) else None
                contacts.append(ContactIdentity(username, alias, remark, nickname,
                                                display_name, _kind(username), token))
            return cls(source, tuple(sorted(contacts, key=lambda item: item.username)), token)
        except CatalogError:
            raise
        except FileNotFoundError:
            raise CatalogError("DATABASE_UNAVAILABLE") from None
        except (OSError, sqlite3.Error, ValueError, UnicodeError):
            raise CatalogError("CATALOG_READ_FAILED") from None

    def assert_current(self) -> None:
        """Reject changes; never silently replace an operation's bound catalog."""
        try:
            requested = Path(self.source.requested_path)
            resolved = Path(self.source.resolved_path)
            expected = (self.source.device, self.source.inode, self.source.size,
                        self.source.mtime_ns, self.source.ctime_ns)
            if (requested.resolve(strict=True) != resolved or _metadata(resolved) != expected
                    or _file_hash(resolved) != self.source.content_sha256
                    or _metadata(resolved) != expected):
                raise CatalogError("CATALOG_SOURCE_CHANGED")
        except (OSError, ValueError, CatalogError):
            raise CatalogError("CATALOG_SOURCE_CHANGED") from None

    def _resolve(self, query, query_kind: str) -> CatalogResult:
        try:
            self.assert_current()
        except CatalogError:
            return CatalogResult("stale_catalog", (), "CATALOG_SOURCE_CHANGED", self.catalog_token, query_kind)
        valid = _valid_text(query) if query_kind in {"display_name", "account_display"} else _valid_identifier(query)
        if not valid:
            return CatalogResult("invalid_query", (), "INVALID_QUERY", self.catalog_token, query_kind)
        if query_kind == "username":
            matches = [item for item in self.contacts if item.username == query]
        elif query_kind == "display_name":
            matches = [item for item in self.contacts if item.display_name == query]
        elif query_kind == "weixin_id":
            matches = [item for item in self.contacts if item.username == query or item.alias == query]
        elif query_kind == "account_display":
            # WeChat 4.1.15 Account presents the complete nickname + space +
            # alias field. Reverse-match that whole value over every contact;
            # never split it or prefer a caller's expected account.
            matches = [item for item in self.contacts
                       if item.kind == "person" and item.alias and _valid_text(item.nickname)
                       and item.nickname + " " + item.alias == query]
            if len(matches) == 1:
                candidate = matches[0]
                stable = self.match_weixin_id(candidate.alias)
                if not stable.matched:
                    return CatalogResult(stable.status, stable.contacts, stable.error_code,
                                         self.catalog_token, query_kind)
                if stable.identity.username != candidate.username:
                    # Nicknames must never disambiguate a stable-ID collision.
                    contenders = {item.username: item for item in (*matches, *stable.contacts)}
                    return CatalogResult("ambiguous", tuple(contenders[key] for key in sorted(contenders)),
                                         "AMBIGUOUS_IDENTITY", self.catalog_token, query_kind)
        else:
            raise CatalogError("INVALID_QUERY_KIND")
        # No priority between username and alias: two different users stay ambiguous.
        identities = {item.username: item for item in matches}
        resolved = tuple(identities[key] for key in sorted(identities))
        status, error = ("matched", None) if len(resolved) == 1 else (("ambiguous", "AMBIGUOUS_IDENTITY") if resolved else ("not_found", "IDENTITY_NOT_FOUND"))
        return CatalogResult(status, resolved, error, self.catalog_token, query_kind)

    def lookup_username(self, username: str) -> CatalogResult:
        return self._resolve(username, "username")

    def resolve_display_name(self, display_name: str) -> CatalogResult:
        return self._resolve(display_name, "display_name")

    def match_weixin_id(self, gui_weixin_id: str) -> CatalogResult:
        return self._resolve(gui_weixin_id, "weixin_id")

    def match_account_display(self, raw_display: str) -> CatalogResult:
        """Require whole-field match and a unique alias-to-user mapping; catalog only."""
        return self._resolve(raw_display, "account_display")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CatalogError("INVALID_ARGUMENTS")


def main(argv=None) -> int:
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("--db", required=True)
        commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
        for command in ("lookup-username", "resolve-display-name", "match-weixin-id", "match-account-display"):
            child = commands.add_parser(command, add_help=False)
            child.add_argument("--value", required=True)
        args = parser.parse_args(argv)
        catalog = IdentityCatalog.from_db(args.db)
        action = {"lookup-username": catalog.lookup_username,
                  "resolve-display-name": catalog.resolve_display_name,
                  "match-weixin-id": catalog.match_weixin_id,
                  "match-account-display": catalog.match_account_display}[args.command]
        output = action(args.value).to_public_dict()
    except CatalogError as error:
        output = {"status": "unavailable", "matched": False, "error_code": error.code,
                  "evidence": "catalog", "ui_identity_confirmed": False}
    except Exception:
        output = {"status": "unavailable", "matched": False, "error_code": "CATALOG_FAILED",
                  "evidence": "catalog", "ui_identity_confirmed": False}
    print(json.dumps(output, separators=(",", ":"), ensure_ascii=True))
    return 0 if output["status"] == "matched" else 2


if __name__ == "__main__":
    raise SystemExit(main())
