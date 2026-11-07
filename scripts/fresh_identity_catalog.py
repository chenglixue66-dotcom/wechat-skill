"""Short-lived contact-only snapshots for local identity resolution.

The catalog does not prove GUI identity. Its lifetime additionally binds the
selected encrypted contact DB/WAL/journal and private account configuration.
No message tables, network, UI actions, or persistent plaintext output.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

from identity_catalog import CatalogError, IdentityCatalog
from snapshot_recipient import (DEFAULT_CONFIG, ROOT, checked_file, digest_file,
                                load_adapter, private_directory)

_LOCK = threading.RLock()
_CONTACT = "db_storage/contact/contact.db"
_OUTPUT = Path.home() / ".config/wechat-skill/identity-snapshots"


def _fingerprint(adapter, source):
    before = adapter._bundle_state(source)
    hashes = {suffix: digest_file(Path(str(source) + suffix))
              for suffix, state in before.items() if state is not None}
    if before != adapter._bundle_state(source):
        raise CatalogError("CONTACT_SOURCE_CHANGED")
    return before, hashes


@dataclass
class FreshIdentityCatalog:
    catalog: IdentityCatalog
    account_id: str
    source_token: str
    _adapter: object
    _source: Path
    _source_fingerprint: tuple
    _config: Path
    _config_fingerprint: tuple
    _expires: float
    _closed: bool = False

    def assert_current(self):
        if self._closed:
            raise CatalogError("FRESH_CATALOG_CLOSED")
        if time.monotonic() >= self._expires:
            raise CatalogError("FRESH_CATALOG_EXPIRED")
        try:
            checked_file(self._config)
            info = self._config.stat(follow_symlinks=False)
            current = (info.st_dev, info.st_ino, info.st_mtime_ns,
                       info.st_ctime_ns, info.st_size, digest_file(self._config))
            if (current != self._config_fingerprint
                    or _fingerprint(self._adapter, self._source) != self._source_fingerprint):
                raise CatalogError("CONTACT_SOURCE_CHANGED")
            self.catalog.assert_current()
            if time.monotonic() >= self._expires:
                raise CatalogError("FRESH_CATALOG_EXPIRED")
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("CONTACT_SOURCE_UNAVAILABLE") from None

    def resolve(self, query, *, kind="weixin_id"):
        self.assert_current()
        method = {"weixin_id": self.catalog.match_weixin_id,
                  "username": self.catalog.lookup_username,
                  "display_name": self.catalog.resolve_display_name,
                  "account_display": self.catalog.match_account_display}.get(kind)
        if method is None:
            raise CatalogError("INVALID_QUERY_KIND")
        result = method(query)
        self.assert_current()
        return result


@contextmanager
def open_fresh_catalog(config_file=DEFAULT_CONFIG, output_root=_OUTPUT, *,
                       adapter=None, ttl_seconds=15.0):
    """Yield a bounded contact catalog; remove private copies on every exit.

    Account ID comes from private config and remains unverified until a GUI
    account field independently matches. Nothing here may unlock UI effects.
    """
    if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
            or not 0 < ttl_seconds <= 30):
        raise CatalogError("INVALID_CATALOG_TTL")
    started = time.monotonic()
    generation, fresh, yielded = None, None, False
    with _LOCK:
        ex = load_adapter(ROOT) if adapter is None else adapter
        old_snapshot, old_decrypted = ex.SNAPSHOT_DIR, ex.DECRYPTED_DIR
        try:
            configuration = checked_file(Path(config_file))
            info = configuration.stat(follow_symlinks=False)
            config_fingerprint = (info.st_dev, info.st_ino, info.st_mtime_ns,
                                  info.st_ctime_ns, info.st_size, digest_file(configuration))
            config = json.loads(configuration.read_text())
            account = ex._account(config["account_dir"])
            account_id = config["self_wxid"]
            if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_@.\-]{1,512}", account_id):
                raise CatalogError("INVALID_ACCOUNT_ID")
            output_path = Path(output_root).expanduser().absolute()
            output_canonical, account_canonical = output_path.resolve(), account.resolve(strict=True)
            if (output_canonical == account_canonical
                    or output_canonical.is_relative_to(account_canonical)
                    or account_canonical.is_relative_to(output_canonical)):
                raise CatalogError("SOURCE_OUTPUT_OVERLAP")
            sources = [d for d in ex.discover_all_databases(account) if d.rel_path == _CONTACT]
            if len(sources) != 1:
                raise CatalogError("UNIQUE_CONTACT_SOURCE_REQUIRED")
            database = sources[0]
            source_fingerprint = _fingerprint(ex, database.source)
            output = private_directory(output_path)
            generation = Path(tempfile.mkdtemp(prefix="identity-", dir=output))
            generation.chmod(0o700)
            ex.SNAPSHOT_DIR, ex.DECRYPTED_DIR = generation / "encrypted", generation / "decrypted"
            key_path = checked_file(Path(config["key_file"]))
            key = key_path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9A-Fa-f]{64}", key) is None:
                raise CatalogError("INVALID_EXPLICIT_KEY_FILE")
            snapshot = ex.snapshot_database(database)
            copied_hashes = {suffix: digest_file(Path(str(snapshot.path) + suffix))
                             for suffix, state in snapshot.state.items() if state is not None}
            if (copied_hashes != source_fingerprint[1]
                    or _fingerprint(ex, database.source) != source_fingerprint):
                raise CatalogError("CONTACT_SOURCE_CHANGED")
            result = ex.decrypt_one_database(database, snapshot,
                ex.WeChatDatabaseDecryptor(key), bytes.fromhex(key))
            key = None
            if not result.success:
                raise CatalogError("CONTACT_DECRYPTION_FAILED")
            plaintext = checked_file(ex.DECRYPTED_DIR / database.rel_path)
            catalog = IdentityCatalog.from_db(plaintext)
            token_document = {"source": str(database.source),
                              "fingerprint": source_fingerprint,
                              "catalog": catalog.catalog_token}
            token = hashlib.sha256(json.dumps(token_document, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            fresh = FreshIdentityCatalog(catalog, account_id, token, ex,
                database.source, source_fingerprint, configuration,
                config_fingerprint, started + ttl_seconds)
            fresh.assert_current()
            yielded = True
            yield fresh
        except CatalogError:
            raise
        except Exception:
            if yielded:
                raise
            raise CatalogError("FRESH_CATALOG_UNAVAILABLE") from None
        finally:
            if fresh is not None:
                fresh._closed = True
            ex.SNAPSHOT_DIR, ex.DECRYPTED_DIR = old_snapshot, old_decrypted
            if generation is not None:
                shutil.rmtree(generation)
