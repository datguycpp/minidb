import sys, time
sys.path.insert(0, "/home/grub/projects/minidb")
import minidb.engine as engine
from minidb.engine import Database
from minidb.storage import btree, record
from minidb.storage.pager import Pager

print("loadavg:", open("/proc/loadavg").read().split()[0], flush=True)
texts = [f"SELECT count(*) FROM t WHERE id > {i}" for i in range(10000)]

def series(fn, n=7):
    out = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); out.append((time.perf_counter() - t0) * 1000.0)
    out.sort()
    return out

def parse_only():
    engine._parse_cached.cache_clear()
    for sql in texts:
        engine._parse_cached(sql)

def loop():
    db = Database("/tmp/g6-tiny.db")
    for sql in texts:
        db.run(sql)
    db.close()

p = series(parse_only); l = series(loop)
def show(tag, v):
    print(f"{tag} n=7 ms: {[round(x,1) for x in v]} median={v[3]:.1f} min={v[0]:.1f} max={v[-1]:.1f} "
          f"spread={(v[-1]-v[0])/v[3]*100:.1f}%", flush=True)
show("PARSEONLY", p); show("LOOP", l)

DB = "/home/grub/.hermes/profiles/engineer/cache/scratch/m38/w9-fixture.db"
_d = Database(DB); root = _d.tables["t"].entry["root"]; _d.close()
def block():
    pager = Pager(DB); tree = btree.BTree(pager); rows = 0
    for _rowid, payload in tree.scan(root):
        record.decode(payload); rows += 1
    pager.close(); return rows
b = series(block)
print(f"BLOCK rows={block()} n=7 ms: {[round(x,1) for x in b]} median={b[3]:.1f} min={b[0]:.1f} "
      f"max={b[-1]:.1f} spread={(b[-1]-b[0])/b[3]*100:.1f}%", flush=True)

