#!/usr/bin/env python3
"""Conservative target-conversation verification over independent SQLite copies.

This module never sends, decrypts, opens WeChat, or reads a key. It verifies only
an observed local message record, not network delivery. The caller supplies an
exact recipient ID and a SHA256 of the UTF-8 outgoing text (without normalizing
whitespace). Zstd text is decoded if the optional zstandard module is installed.

Without --manifest, baseline and verify must use the same canonical file paths
and device/inode identities, with unchanged schema and historical target rows.
Replacing an export, rebuilding a database, reusing local IDs, changing a token
or changing the shard set yields uncertain. A private manifest from the trusted
local sampler can instead bind each copy's SHA256 to a stable source identity
(source path/device/inode/encrypted-header salt hash) and the account. This
permits different immutable plaintext copies, while all message-history checks
remain mandatory. The manifest is a local trust contract, not a signature or a
way to accept arbitrary source identities on the command line. Do not mutate a
read-only snapshot just to satisfy this verifier.

Both CLI commands return JSON only. Every result has retry_allowed=false. A
missing observation is uncertain and must never trigger an automatic resend.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time


VERSION = 1
MAX_BODY = 4_000_000
_OPERATION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TABLE = re.compile(r"Msg_[a-f0-9]{32}\Z")
_REQUIRED = {"create_time", "real_sender_id", "local_type", "message_content"}
_SIDECARS = ("-wal", "-shm", "-journal")


class VerificationError(RuntimeError):
    """Contains a fixed diagnostic code, never database values or SQL text."""


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _result(status: str, operation_id: str | None, **values) -> dict:
    return {"status": status, "operation_id": operation_id, "retry_allowed": False, **values}


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise VerificationError("INVALID_IDENTITY")
    return value


def _paths(databases) -> list[Path]:
    paths = []
    for value in databases:
        path = Path(value).expanduser()
        if path.is_symlink():
            raise VerificationError("SYMLINK_DATABASE")
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise VerificationError("DATABASE_UNAVAILABLE") from None
        if path in paths:
            raise VerificationError("DUPLICATE_DATABASE")
        paths.append(path)
    if not paths:
        raise VerificationError("DATABASE_REQUIRED")
    return sorted(paths, key=str)


def _metadata(path: Path) -> dict:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError("REGULAR_DATABASE_REQUIRED")
    if any(Path(str(path) + suffix).exists() for suffix in _SIDECARS):
        raise VerificationError("STANDALONE_SNAPSHOT_REQUIRED")
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _canonical_manifest_path(value) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise VerificationError("MANIFEST_ABSOLUTE_PATH_REQUIRED")
    path = Path(value)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise VerificationError("MANIFEST_SYMLINK_FORBIDDEN")
    if path.resolve(strict=True) != path:
        raise VerificationError("MANIFEST_CANONICAL_PATH_REQUIRED")
    return path


def _source_key(entry: dict) -> tuple:
    return tuple(entry[key] for key in ("source_path", "source_dev", "source_ino", "source_salt_sha256"))


def _lineage_id(entry: dict) -> str:
    return hashlib.sha256(json.dumps(_source_key(entry), separators=(",", ":")).encode()).hexdigest()[:24]


def _snapshot_hash(path: Path) -> tuple[str, dict]:
    before = _metadata(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if _metadata(path) != before:
        raise VerificationError("MANIFEST_SNAPSHOT_CHANGED")
    return digest.hexdigest(), before


def _load_manifest(value, paths: list[Path], snapshot_id: str | None, self_wxid: str) -> dict:
    manifest_path = _canonical_manifest_path(str(Path(value).expanduser().absolute()))
    before = manifest_path.stat(follow_symlinks=False)
    parent = manifest_path.parent.stat(follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise VerificationError("PRIVATE_MANIFEST_AND_DIRECTORY_REQUIRED")
    with os.fdopen(os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf8") as stream:
        opened = os.fstat(stream.fileno())
        document = json.load(stream)
        after_open = os.fstat(stream.fileno())
    after = manifest_path.stat(follow_symlinks=False)
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if not identity(before) == identity(opened) == identity(after_open) == identity(after):
        raise VerificationError("MANIFEST_CHANGED_DURING_READ")
    if (not isinstance(document, dict) or set(document) != {"schema", "snapshot_id", "account_id", "databases"}
            or type(document["schema"]) is not int or document["schema"] != 1):
        raise VerificationError("UNSUPPORTED_MANIFEST")
    if not isinstance(document["snapshot_id"], str) or not document["snapshot_id"] or document["snapshot_id"] != snapshot_id:
        raise VerificationError("MANIFEST_SNAPSHOT_ID_MISMATCH")
    if document["account_id"] != self_wxid:
        raise VerificationError("MANIFEST_ACCOUNT_MISMATCH")
    entries = document["databases"]
    if not isinstance(entries, list) or len(entries) != len(paths):
        raise VerificationError("MANIFEST_DATABASE_SET_MISMATCH")
    fields = {"snapshot_path", "snapshot_sha256", "source_path", "source_dev", "source_ino", "source_salt_sha256"}
    by_path, source_keys, source_paths = {}, set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise VerificationError("INCOMPLETE_MANIFEST_ENTRY")
        if (not isinstance(entry["snapshot_sha256"], str) or not _HASH.fullmatch(entry["snapshot_sha256"])
                or not isinstance(entry["source_salt_sha256"], str) or not _HASH.fullmatch(entry["source_salt_sha256"])
                or type(entry["source_dev"]) is not int or entry["source_dev"] < 0
                or type(entry["source_ino"]) is not int or entry["source_ino"] <= 0):
            raise VerificationError("INVALID_MANIFEST_IDENTITY")
        snapshot_path = _canonical_manifest_path(entry["snapshot_path"])
        source_path = _canonical_manifest_path(entry["source_path"])
        source = source_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(source.st_mode) or source.st_dev != entry["source_dev"] or source.st_ino != entry["source_ino"]:
            raise VerificationError("MANIFEST_SOURCE_IDENTITY_MISMATCH")
        if snapshot_path == source_path:
            raise VerificationError("MANIFEST_SOURCE_MUST_BE_SEPARATE")
        key = _source_key(entry)
        if snapshot_path in by_path or key in source_keys or source_path in source_paths:
            raise VerificationError("DUPLICATE_MANIFEST_DATABASE")
        source_keys.add(key)
        source_paths.add(source_path)
        digest, checked = _snapshot_hash(snapshot_path)
        if digest != entry["snapshot_sha256"]:
            raise VerificationError("MANIFEST_SNAPSHOT_HASH_MISMATCH")
        by_path[snapshot_path] = {**entry, "checked_metadata": checked}
    if set(by_path) != set(paths):
        raise VerificationError("MANIFEST_DATABASE_SET_MISMATCH")
    return {"snapshot_id": snapshot_id, "account_id": self_wxid, "entries": by_path}


def _attach_manifest(snapshot: dict, entry: dict) -> None:
    if snapshot["file_state"] != entry["checked_metadata"]:
        raise VerificationError("MANIFEST_SNAPSHOT_CHANGED")
    snapshot["database_id"] = _lineage_id(entry)
    snapshot["lineage"] = {key: entry[key] for key in ("source_path", "source_dev", "source_ino", "source_salt_sha256")}
    snapshot["snapshot_sha256"] = entry["snapshot_sha256"]


def _assert_manifest_stable(manifest: dict | None) -> None:
    if manifest:
        for path, entry in manifest["entries"].items():
            if _metadata(path) != entry["checked_metadata"]:
                raise VerificationError("MANIFEST_SNAPSHOT_CHANGED")
            source_path = _canonical_manifest_path(entry["source_path"])
            source = source_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(source.st_mode) or (source.st_dev, source.st_ino) != (entry["source_dev"], entry["source_ino"]):
                raise VerificationError("MANIFEST_SOURCE_IDENTITY_MISMATCH")


def _number(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise VerificationError("INVALID_RECORD_ID")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,30}", value):
        return int(value)
    raise VerificationError("INVALID_RECORD_ID")


def _raw_hash(value) -> str:
    if value is None:
        encoded = b"N"
    elif isinstance(value, str):
        encoded = b"T" + value.encode("utf8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        encoded = b"B" + bytes(value)
    else:
        raise VerificationError("UNSUPPORTED_BODY_ENCODING")
    return hashlib.sha256(encoded).hexdigest()


def _body_hash(value) -> str:
    if value is None:
        return hash_text("")
    if isinstance(value, str):
        return hash_text(value)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise VerificationError("UNSUPPORTED_BODY_ENCODING")
    data = bytes(value)
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard
            size = zstandard.frame_content_size(data)
            if size > MAX_BODY:
                raise VerificationError("BODY_DECODE_UNCERTAIN")
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_BODY)
        except Exception:
            # Do not surface the decoder's data-dependent diagnostic.
            raise VerificationError("BODY_DECODE_UNCERTAIN") from None
    if len(data) > MAX_BODY:
        raise VerificationError("BODY_DECODE_UNCERTAIN")
    try:
        return hash_text(data.decode("utf8"))
    except UnicodeDecodeError:
        raise VerificationError("BODY_DECODE_UNCERTAIN") from None


def _read_snapshot(path: Path, recipient_id: str, self_wxid: str) -> dict:
    before = _metadata(path)
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise VerificationError("PLAINTEXT_SQLITE_REQUIRED")
    table = "Msg_" + hashlib.md5(recipient_id.encode("utf8")).hexdigest()
    if not _TABLE.fullmatch(table):
        raise VerificationError("INVALID_TABLE_IDENTIFIER")
    # immutable=1 prevents SQLite creating sidecars even if a header says WAL.
    # A before/after state comparison rejects a copy changed during our read.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        own_ids = {row[0] for row in connection.execute(
            'SELECT rowid FROM "Name2Id" WHERE user_name=?', (self_wxid,)
        )}
        rows = []
        columns = set()
        if schema is not None:
            columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
            if not _REQUIRED <= columns:
                raise VerificationError("UNSUPPORTED_TARGET_SCHEMA")
            local = '"local_id"' if "local_id" in columns else "NULL"
            server = '"server_id"' if "server_id" in columns else "NULL"
            # The table identifier comes only from the strictly checked MD5.
            query = ('SELECT rowid,create_time,real_sender_id,local_type,message_content,'
                     + local + "," + server + ' FROM "' + table + '" ORDER BY rowid')
            for rowid, timestamp, sender, message_type, body, local_id, server_id in connection.execute(query):
                rows.append({"rowid": int(rowid), "timestamp": _number(timestamp),
                             "sender": _number(sender), "type": _number(message_type),
                             "body": body, "raw_hash": _raw_hash(body),
                             "local_id": _number(local_id), "server_id": _number(server_id)})
    finally:
        connection.close()
    if _metadata(path) != before:
        raise VerificationError("SNAPSHOT_CHANGED_DURING_READ")
    return {"path": str(path), "file_state": before,
            "identity": {key: before[key] for key in ("device", "inode")},
            "database_id": hashlib.sha256(str(path).encode()).hexdigest()[:24],
            "schema_version": schema_version, "user_version": user_version,
            "schema_hash": hash_text(schema[0] or "") if schema else None,
            "has_local_id": "local_id" in columns, "has_server_id": "server_id" in columns,
            "own_ids": sorted(own_ids), "rows": rows}


def _history_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        value = [row[key] for key in ("rowid", "timestamp", "sender", "type", "local_id", "server_id", "raw_hash")]
        digest.update(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()


def _baseline_part(snapshot: dict) -> dict:
    rows = snapshot["rows"]
    return {key: value for key, value in snapshot.items() if key not in {"rows", "file_state"}} | {
        "row_count": len(rows), "max_rowid": max((row["rowid"] for row in rows), default=0),
        "history_hash": _history_hash(rows),
        "local_ids": sorted({str(row["local_id"]) for row in rows if (row["local_id"] or 0) > 0}),
        "server_ids": sorted({str(row["server_id"]) for row in rows if (row["server_id"] or 0) > 0}),
    }


def _state_path(value) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise VerificationError("SYMLINK_BASELINE")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@contextmanager
def _locked(path: Path):
    lock = Path(str(path) + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise VerificationError("OPERATION_IN_USE") from None
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _load_state(path: Path) -> dict:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise VerificationError("PRIVATE_BASELINE_REQUIRED")
    with path.open("r", encoding="utf8") as stream:
        state = json.load(stream)
    if state.get("version") != VERSION:
        raise VerificationError("UNSUPPORTED_BASELINE")
    return state


def _save_state(path: Path, state: dict) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".verify-state-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump(state, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise VerificationError("SYMLINK_BASELINE")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_baseline(databases, baseline, recipient_id: str, self_wxid: str,
                    payload_hash: str, operation_id: str, message_type: int = 1,
                    snapshot_id: str | None = None, manifest=None) -> dict:
    try:
        if not _OPERATION.fullmatch(operation_id or "") or not _HASH.fullmatch(payload_hash or ""):
            raise VerificationError("INVALID_OPERATION_OR_PAYLOAD_HASH")
        recipient_id, self_wxid = _identity(recipient_id), _identity(self_wxid)
        if not isinstance(message_type, int) or isinstance(message_type, bool) or not 0 <= message_type <= 0xffffffff:
            raise VerificationError("INVALID_MESSAGE_TYPE")
        paths = _paths(databases)
        lineage = _load_manifest(manifest, paths, snapshot_id, self_wxid) if manifest is not None else None
        path = _state_path(baseline)
        if path.resolve() in paths:
            raise VerificationError("BASELINE_CANNOT_REPLACE_DATABASE")
        with _locked(path):
            expected = {"operation_id": operation_id, "recipient_id": recipient_id,
                        "self_wxid": self_wxid, "payload_hash": payload_hash,
                        "message_type": message_type, "snapshot_id": snapshot_id,
                        "lineage_mode": "manifest" if lineage else None}
            if path.exists():
                state = _load_state(path)
                if any(state.get(key) != value for key, value in expected.items()) or [part["path"] for part in state["databases"]] != [str(p) for p in paths]:
                    raise VerificationError("BASELINE_OPERATION_CONFLICT")
                return _result("baseline_exists", operation_id, baseline_at=state["created_at"],
                               send_allowed=False, already_verified=bool(state.get("verified_result")))
            snapshots = [_read_snapshot(item, recipient_id, self_wxid) for item in paths]
            if lineage:
                for snapshot, item in zip(snapshots, paths):
                    _attach_manifest(snapshot, lineage["entries"][item])
                _assert_manifest_stable(lineage)
            if not any(item["schema_hash"] is not None and item["own_ids"] for item in snapshots):
                raise VerificationError("TARGET_OR_SELF_IDENTITY_UNAVAILABLE")
            state = {"version": VERSION, **expected, "created_at": time.time(),
                     "databases": [_baseline_part(item) for item in snapshots],
                     "committed_at": None, "verification_attempts": 0}
            _save_state(path, state)
            return _result("baseline_created", operation_id, baseline_at=state["created_at"],
                           database_count=len(paths), target_row_count=sum(len(item["rows"]) for item in snapshots))
    except VerificationError as error:
        return _result("uncertain", operation_id, reason=str(error))
    except Exception:
        return _result("uncertain", operation_id, reason="BASELINE_READ_FAILED")


def _verify_state(state: dict, paths: list[Path], committed_at: float, manifest: dict | None = None) -> dict:
    matches, new_count = [], 0
    if manifest:
        current_sources = {_source_key(entry): (path, entry) for path, entry in manifest["entries"].items()}
        if set(current_sources) != {_source_key(old["lineage"]) for old in state["databases"]}:
            raise VerificationError("LINEAGE_SOURCE_SET_CHANGED")
        pairs = [(old, *current_sources[_source_key(old["lineage"])]) for old in state["databases"]]
    else:
        pairs = [(old, path, None) for old, path in zip(state["databases"], paths)]
    for old, path, entry in pairs:
        current = _read_snapshot(path, state["recipient_id"], state["self_wxid"])
        if entry:
            _attach_manifest(current, entry)
        elif current["identity"] != old["identity"]:
            raise VerificationError("DATABASE_REPLACED")
        for key in ("schema_version", "user_version", "schema_hash", "has_local_id", "has_server_id"):
            if current[key] != old[key]:
                raise VerificationError("SCHEMA_OR_DATABASE_REBUILT")
        if not set(old["own_ids"]) <= set(current["own_ids"]):
            raise VerificationError("SELF_IDENTITY_CHANGED")
        history = [row for row in current["rows"] if row["rowid"] <= old["max_rowid"]]
        if len(history) != old["row_count"] or _history_hash(history) != old["history_hash"]:
            raise VerificationError("BASELINE_HISTORY_CHANGED")
        added = [row for row in current["rows"] if row["rowid"] > old["max_rowid"]]
        new_count += len(added)
        old_local, old_server = set(old["local_ids"]), set(old["server_ids"])
        seen_local, seen_server = set(old_local), set(old_server)
        max_local = max((int(value) for value in old_local), default=0)
        for row in added:
            local, server = row["local_id"], row["server_id"]
            if (local or 0) > 0 and (str(local) in seen_local or local <= max_local):
                raise VerificationError("LOCAL_ID_REUSED_OR_RESET")
            if (server or 0) > 0 and str(server) in seen_server:
                raise VerificationError("SERVER_ID_REUSED")
            if (local or 0) > 0:
                seen_local.add(str(local))
            if (server or 0) > 0:
                seen_server.add(str(server))
            if row["sender"] not in current["own_ids"] or row["type"] is None or row["type"] & 0xffffffff != state["message_type"]:
                continue
            if _body_hash(row["body"]) != state["payload_hash"]:
                continue
            if row["timestamp"] is None or row["timestamp"] < math.floor(committed_at) or row["timestamp"] > math.ceil(time.time()) + 2:
                raise VerificationError("MATCH_OUTSIDE_COMMIT_WINDOW")
            matches.append({"database_id": current["database_id"], "rowid": row["rowid"],
                            "local_id": str(local) if local is not None else None,
                            "server_id": str(server) if server is not None else None,
                            "timestamp": row["timestamp"]})
    _assert_manifest_stable(manifest)
    cached = state.get("verified_result")
    if cached:
        if cached["records"][0] not in matches:
            raise VerificationError("VERIFIED_EVIDENCE_CHANGED")
        return {**cached, "cached": True}
    if len(matches) != 1:
        return _result("uncertain", state["operation_id"], reason="NO_UNIQUE_NEW_MATCH",
                       new_target_rows=new_count, matching_new_rows=len(matches))
    return _result("verified", state["operation_id"], cached=False, delivery_confirmed=False,
                   new_target_rows=new_count, matching_new_rows=1, records=matches,
                   committed_at=committed_at, verified_at=time.time())


def verify(databases, baseline, recipient_id: str, operation_id: str,
           committed_at: float, snapshot_id: str | None = None, manifest=None) -> dict:
    try:
        paths = _paths(databases)
        path = _state_path(baseline)
        with _locked(path):
            state = _load_state(path)
            if state.get("operation_id") != operation_id or state.get("recipient_id") != recipient_id:
                raise VerificationError("OPERATION_OR_RECIPIENT_MISMATCH")
            manifest_mode = state.get("lineage_mode") == "manifest"
            if manifest_mode != (manifest is not None):
                raise VerificationError("MANIFEST_MODE_CHANGED")
            lineage = _load_manifest(manifest, paths, snapshot_id, state["self_wxid"]) if manifest_mode else None
            if not manifest_mode and (state.get("snapshot_id") != snapshot_id or [item["path"] for item in state["databases"]] != [str(p) for p in paths]):
                raise VerificationError("SNAPSHOT_SET_CHANGED")
            if not isinstance(committed_at, (int, float)) or not math.isfinite(committed_at) or committed_at < state["created_at"] or committed_at > time.time() + 1:
                raise VerificationError("INVALID_COMMIT_TIME")
            if state["committed_at"] is not None and state["committed_at"] != committed_at:
                raise VerificationError("COMMIT_TIME_CHANGED")
            state["committed_at"] = committed_at
            state["verification_attempts"] += 1
            try:
                result = _verify_state(state, paths, committed_at, lineage)
            except VerificationError as error:
                result = _result("uncertain", operation_id, reason=str(error))
            state["last_result"] = result
            if result["status"] == "verified" and not state.get("verified_result"):
                state["verified_result"] = result
            _save_state(path, state)
            return result
    except VerificationError as error:
        return _result("uncertain", operation_id, reason=str(error))
    except Exception:
        return _result("uncertain", operation_id, reason="VERIFICATION_READ_FAILED")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise VerificationError("INVALID_ARGUMENTS")


def main(argv=None) -> int:
    operation_id = None
    try:
        parser = _Parser(description=__doc__, add_help=False)
        commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
        for command in ("baseline", "verify"):
            child = commands.add_parser(command, add_help=False)
            child.add_argument("--db", action="append", required=True)
            child.add_argument("--baseline", required=True)
            child.add_argument("--recipient-id", required=True)
            child.add_argument("--operation-id", required=True)
            child.add_argument("--snapshot-id")
            child.add_argument("--manifest")
            if command == "baseline":
                child.add_argument("--self-wxid", required=True)
                child.add_argument("--payload-hash", required=True)
                child.add_argument("--message-type", type=int, default=1)
            else:
                child.add_argument("--committed-at", type=float, required=True)
        args = parser.parse_args(argv)
        operation_id = args.operation_id
        common = dict(databases=args.db, baseline=args.baseline, recipient_id=args.recipient_id,
                      operation_id=operation_id, snapshot_id=args.snapshot_id, manifest=args.manifest)
        result = (create_baseline(**common, self_wxid=args.self_wxid,
                                  payload_hash=args.payload_hash, message_type=args.message_type)
                  if args.command == "baseline" else verify(**common, committed_at=args.committed_at))
    except VerificationError as error:
        result = _result("uncertain", operation_id, reason=str(error))
    except Exception:
        result = _result("uncertain", operation_id, reason="VERIFICATION_FAILED")
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    return 0 if result["status"] in {"baseline_created", "baseline_exists", "verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
