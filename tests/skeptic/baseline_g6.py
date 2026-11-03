import sys, time
sys.path.insert(0, "/home/grub/projects/minidb")
from minidb.engine import Database
from minidb.storage import btree, record
from minidb.storage.pager import Pager

print("loadavg:", open("/proc/loadavg").read().split()[0], flush=True)

# gate 3 (amended shape): 10,000 statements, ALL-DISTINCT texts -> every statement misses the
# 1024-entry parse LRU (engine.py:894), so the loop is parse-dominated by construction.
texts = [f"SELECT count(*) FROM t WHERE id > {i}" for i in range(10000)]
parse = []
for _ in range(7):
    db = Database("/tmp/g6-tiny.db")
    t0 = time.perf_counter()
    for sql in texts:
        db.run(sql)
    parse.append((time.perf_counter() - t0) * 1000.0)
    db.close()
parse.sort()
print(f"PARSE n=7 ms: {[round(v,1) for v in parse]} median={parse[3]:.1f} "
      f"min={parse[0]:.1f} max={parse[-1]:.1f}", flush=True)

DB = "/home/grub/.hermes/profiles/engineer/cache/scratch/m38/w9-fixture.db"
_db = Database(DB); root = _db.tables["t"].entry["root"]; _db.close()

def block():
    pager = Pager(DB)
    tree = btree.BTree(pager)
    rows = 0
    for _rowid, payload in tree.scan(root):
        record.decode(payload)
        rows += 1
    pager.close()
    return rows

blocks = []
for _ in range(7):
    t0 = time.perf_counter(); rows = block(); blocks.append((time.perf_counter() - t0) * 1000.0)
blocks.sort()
print(f"BLOCK rows={rows} n=7 ms: {[round(v,1) for v in blocks]} median={blocks[3]:.1f} "
      f"min={blocks[0]:.1f} max={blocks[-1]:.1f}", flush=True)
