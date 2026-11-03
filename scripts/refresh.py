# 只解密现在要查的库（新消息分片、联系人、会话），不动 .local-export，不拷媒体。不打印密钥。
import os, sys, importlib.util
from pathlib import Path
R = Path.home() / "WeChatDataAnalysis"
spec = importlib.util.spec_from_file_location("ex", R / "tools/export_wechat_archive.py")
ex = importlib.util.module_from_spec(spec); sys.modules["ex"] = ex; spec.loader.exec_module(ex)
root = R / ".local-export-fresh"
ex.SNAPSHOT_DIR = root / ".snapshot"; ex.DECRYPTED_DIR = root / "decrypted"
for d in (ex.SNAPSHOT_DIR, ex.DECRYPTED_DIR): d.mkdir(parents=True, exist_ok=True)
acc = Path(os.environ["WECHAT_ACCOUNT_DIR"])
key = ex.load_validated_key(acc); dec = ex.WeChatDatabaseDecryptor(key)
want = {"message/message_0.db","message/message_1.db","message/message_2.db","contact/contact.db","session/session.db"}
for d in ex.discover_all_databases(acc):
    if not any(d.rel_path.replace("\\","/").endswith(w) for w in want): continue
    o = ex.decrypt_one_database(d, ex.snapshot_database(d), dec, bytes.fromhex(key))
    print(d.rel_path, o.success, o.quick_check, o.table_count, o.wal.applied, o.error[:100])
