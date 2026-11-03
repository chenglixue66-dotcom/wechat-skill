import os, sqlite3, hashlib, zstandard, re, time
from pathlib import Path
SELF = os.environ["WECHAT_SELF_WXID"]
D = Path("decrypted/db_storage")
c = sqlite3.connect(D/"contact/contact.db")
names = {}
for u, rem, nick, alias in c.execute("select username, remark, nick_name, alias from contact"):
    names[hashlib.md5(u.encode()).hexdigest()] = (u, rem or "", nick or "", alias or "")
out = sqlite3.connect("idx.db"); out.executescript("drop table if exists m; create table m(ts int, u text, name text, nick text, self int, sender int, type int, text text); drop table if exists names; create table names(u text primary key, remark text, nick text, alias text);")
out.executemany("insert or ignore into names values(?,?,?,?)", [(v[0],v[1],v[2],v[3]) for v in names.values()])
z = zstandard.ZstdDecompressor()
for sh in (0,1,2):
    db = sqlite3.connect(D/f"message/message_{sh}.db")
    try: n2i = dict(db.execute("select rowid, user_name from Name2Id"))
    except Exception: n2i = {}
    selfid = [k for k,v in n2i.items() if v==SELF]
    selfid = selfid[0] if selfid else -1
    for (t,) in db.execute("select name from sqlite_master where type='table' and name like 'Msg_%'").fetchall():
        h = t[4:]
        info = names.get(h)
        if not info: continue
        rows = []
        cols = [r[1] for r in db.execute(f"pragma table_info([{t}])")]
        ct = "WCDB_CT_message_content" if "WCDB_CT_message_content" in cols else "0"
        for ts, s, ty, body, zf in db.execute(f"select create_time, real_sender_id, local_type, message_content, {ct} from [{t}]"):
            if isinstance(body,(bytes,bytearray)):
                try:
                    body = z.decompress(bytes(body), max_output_size=4_000_000).decode("utf8","ignore") if bytes(body)[:4]==b"\x28\xb5\x2f\xfd" else bytes(body).decode("utf8","ignore")
                except Exception: body = ""
            rows.append((ts, info[0], info[1], info[2], int(s==selfid), s, ty, body or ""))
        out.executemany("insert into m values(?,?,?,?,?,?,?,?)", rows)
    out.commit(); print("shard", sh, "ok", len(n2i))
out.execute("create index i1 on m(u,ts)"); out.execute("create index i2 on m(ts)"); out.commit()
print(out.execute("select count(*), datetime(min(ts),'unixepoch','+8 hours'), datetime(max(ts),'unixepoch','+8 hours') from m").fetchone())
