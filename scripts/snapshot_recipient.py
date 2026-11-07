#!/usr/bin/env python3
"""Private, local message-shard snapshots for target-only receipt verification.

No UI operations, sending, indexing, key discovery, or external network calls.
The manifest is produced by this trusted local sampler, not a signed attestation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid

ROOT = Path.home() / "WeChatDataAnalysis"
DEFAULT_CONFIG = Path.home() / ".config/wechat-skill/config.json"
DEFAULT_OUTPUT = Path.home() / ".config/wechat-skill/verification-snapshots"
SHARD = re.compile(r"db_storage/message/message_\d+\.db\Z")


def checked_file(value: Path, mode=0o600) -> Path:
    path = value.expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("PARENT_PATH_REJECTED")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("SYMLINK_REJECTED")
    info = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("PRIVATE_FILE_REQUIRED")
    return path


def private_directory(value: Path) -> Path:
    path = value.expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("PARENT_PATH_REJECTED")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("SYMLINK_REJECTED")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    info = path.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("PRIVATE_DIRECTORY_REQUIRED")
    return path


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(path: Path) -> dict:
    before = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("INVALID_SOURCE")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        salt = stream.read(16)
    after = path.stat(follow_symlinks=False)
    if (len(salt) != 16 or salt == b"SQLite format 3\x00"
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
        raise ValueError("SOURCE_CHANGED_OR_NOT_ENCRYPTED")
    return {"source_path": str(path), "source_dev": before.st_dev,
            "source_ino": before.st_ino,
            "source_salt_sha256": hashlib.sha256(salt).hexdigest()}


def load_adapter(root: Path):
    path = root / "tools/export_wechat_archive.py"
    spec = importlib.util.spec_from_file_location("wechat_receipt_snapshot_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError("ADAPTER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_snapshot(config_file=DEFAULT_CONFIG, output_root=DEFAULT_OUTPUT, *, adapter=None) -> dict:
    started = time.monotonic()
    os.umask(0o077)
    configuration = checked_file(Path(config_file))
    config = json.loads(configuration.read_text())
    account = Path(config["account_dir"]).expanduser().absolute()
    account_id = config["self_wxid"]
    key_file = checked_file(Path(config["key_file"]))
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("ACCOUNT_ID_REQUIRED")
    ex = load_adapter(ROOT) if adapter is None else adapter
    databases = [d for d in ex.discover_all_databases(account) if SHARD.fullmatch(d.rel_path)]
    if not databases:
        raise ValueError("NO_MESSAGE_SHARDS")
    account_canonical = account.resolve(strict=True)
    output_canonical = Path(output_root).expanduser().resolve()
    if (output_canonical == account_canonical or output_canonical.is_relative_to(account_canonical)
            or account_canonical.is_relative_to(output_canonical)):
        raise ValueError("SOURCE_OUTPUT_OVERLAP")
    output = private_directory(Path(output_root))
    generation = Path(tempfile.mkdtemp(prefix="receipt-", dir=output))
    generation.chmod(0o700)
    snapshot_id = uuid.uuid4().hex
    ex.SNAPSHOT_DIR = generation / ".snapshot"
    ex.DECRYPTED_DIR = generation / "decrypted"
    previous = os.environ.get("WECHAT_KEY_FILE")
    os.environ["WECHAT_KEY_FILE"] = str(key_file)
    try:
        # Explicit local key only; no extraction and no additional account scan.
        key = ex._read_explicit_key()
        databases.sort(key=lambda item: int(Path(item.rel_path).stem.rsplit("_", 1)[1]))
        decryptor = ex.WeChatDatabaseDecryptor(key)
        entries = []
        for database in databases:
            identity = source_identity(database.source)
            encrypted_copy = ex.snapshot_database(database)
            with encrypted_copy.path.open("rb") as stream:
                copied_salt = hashlib.sha256(stream.read(16)).hexdigest()
            if copied_salt != identity["source_salt_sha256"] or source_identity(database.source) != identity:
                raise ValueError("SOURCE_LINEAGE_CHANGED")
            result = ex.decrypt_one_database(database, encrypted_copy, decryptor, bytes.fromhex(key))
            if not result.success:
                raise ValueError("DECRYPTION_OR_INTEGRITY_FAILED")
            path = ex.DECRYPTED_DIR / database.rel_path
            checked_file(path)
            if source_identity(database.source) != identity:
                raise ValueError("SOURCE_LINEAGE_CHANGED")
            entries.append({"snapshot_path": str(path), "snapshot_sha256": digest_file(path), **identity})
        after = [d.rel_path for d in ex.discover_all_databases(account) if SHARD.fullmatch(d.rel_path)]
        if sorted(after) != sorted(d.rel_path for d in databases):
            raise ValueError("MESSAGE_SHARD_SET_CHANGED")
        document = {"schema": 1, "snapshot_id": snapshot_id,
                    "account_id": account_id, "databases": entries}
        manifest = generation / "manifest.json"
        with manifest.open("x") as stream:
            json.dump(document, stream, separators=(",", ":"))
        manifest.chmod(0o600)
        return {"status": "snapshot_created", "snapshot_id": snapshot_id,
                "manifest": str(manifest), "databases": [entry["snapshot_path"] for entry in entries],
                "database_count": len(entries), "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "network_calls": 0, "ui_writes": 0, "send_attempts": 0}
    finally:
        if previous is None:
            os.environ.pop("WECHAT_KEY_FILE", None)
        else:
            os.environ["WECHAT_KEY_FILE"] = previous


def main():
    parser = argparse.ArgumentParser(description="Sample only ordinary message shards for local receipt checks.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = create_snapshot(args.config, args.output_root)
    except Exception:
        print('{"status":"blocked","code":"SNAPSHOT_FAILED","send_attempts":0}')
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
