"""Gate 1/3 baseline capture: parse-only, loop, and record-decode block series.

The frozen ceilings at `d89f689` came from this script's output, so what it *measures* must not
move: `series()`'s inner loop is byte-identical (perf_counter around a bare `fn()`, sorted, median
at the middle index for n=7). What grew is the output side — fixture identity, one load reading per
series, labels that report the count they actually printed — so a later capture can prove it
measured the same inputs on the same box state.

Fixture provenance: the frozen inputs' identities are pinned in EXPECTED and **asserted** — a file
that no longer matches stops the capture (loudly, and only past `G6_ALLOW_DRIFT=1` deliberately).
The W9 identity is reproducible, not merely recorded: two fresh builds through the committed
harness shape (`benchmark.py`'s create + one transaction + `build_rows(100000, seed=7)`) hash to it
byte-for-byte, so a cold clone can verify provenance by rebuilding. The tiny fixture has no
committed builder; its identity is recorded and its history remains the assumption the record
states. Override the paths with G6_TINY_DB / G6_W9_DB.

The BLOCK fixture is a 100k-row file and its `rows=` count is printed beside the timing, so a
rebuilt input with different content is visible in the artifact instead of arriving as a quietly
different baseline.
"""

import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import minidb.engine as engine                     # noqa: E402
from minidb.engine import Database                 # noqa: E402
from minidb.storage import btree, record           # noqa: E402
from minidb.storage.pager import Pager             # noqa: E402

REPO_FIXTURES = os.path.join(ROOT, "tests", "skeptic", "fixtures")
LEGACY_TINY = "/tmp/g6-tiny.db"
LEGACY_W9 = "/home/grub/.hermes/profiles/engineer/cache/scratch/m38/w9-fixture.db"


def loadavg():
    with open("/proc/loadavg", encoding="ascii") as handle:
        return handle.read().split()[0]


def fixture(env, name, legacy):
    """Resolve one fixture; refuse to measure a file that is not there.

    An absent input must stop the capture rather than silently measure a different one — that is
    the mixed-baseline class arriving through the fixture instead of the number.
    """
    candidate = os.environ.get(env)
    paths = [candidate] if candidate else [os.path.join(REPO_FIXTURES, name), legacy]
    for path in paths:
        if path and os.path.exists(path):
            return path
    raise SystemExit("baseline: no %s fixture — set %s to a copy of the capture input"
                     % (name, env))


def identity(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return os.path.getsize(path), digest.hexdigest()


def check_identity(role, path, expected):
    """Print the input's identity and refuse a different one.

    Identity is *asserted*, not merely recorded: an accidental modification (a probe opening the
    file read-write, a checkpoint, a VACUUM, a rebuild in place) otherwise produces a valid-looking
    file that is no longer the one the baseline measured, and the difference would show up only if
    a human compared two hashes across two records. `G6_ALLOW_DRIFT=1` is the deliberate override,
    and it prints the drift loudly first so the artifact carries it.
    """
    size, digest = identity(path)
    print("fixture %s: %s bytes=%d sha256=%s" % (role, os.path.abspath(path), size, digest),
          flush=True)
    if (size, digest) != expected:
        print("FIXTURE DRIFT %s: expected bytes=%d sha256=%s" % (role, expected[0], expected[1]),
              flush=True)
        if os.environ.get("G6_ALLOW_DRIFT") != "1":
            raise SystemExit("baseline: %s input is not the frozen one — set G6_ALLOW_DRIFT=1 only "
                             "to capture a deliberately different input" % role)
    return path


texts = [f"SELECT count(*) FROM t WHERE id > {i}" for i in range(10000)]


def series(fn, n=7):
    out = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); out.append((time.perf_counter() - t0) * 1000.0)
    out.sort()
    return out


def capture(tag, fn):
    """One series, bracketed by its own load readings: this box drifts within a run."""
    print(f"loadavg before {tag}: {loadavg()}", flush=True)
    values = series(fn)
    print(f"loadavg after {tag}: {loadavg()}", flush=True)
    return values


def show(tag, v):
    middle = len(v) // 2
    print(f"{tag} n={len(v)} ms: {[round(x,1) for x in v]} median={v[middle]:.1f} "
          f"min={v[0]:.1f} max={v[-1]:.1f} spread={(v[-1]-v[0])/v[middle]*100:.1f}%",
          flush=True)


TINY = fixture("G6_TINY_DB", "g6-tiny.db", LEGACY_TINY)
W9 = fixture("G6_W9_DB", "w9-fixture.db", LEGACY_W9)
# The frozen inputs' identities. Both were confirmed by independent hashing, and the W9 one is
# reproducible: two fresh builds by the committed harness shape (benchmark.py's create + one
# transaction + build_rows(100000, seed=7)) hash to it byte-for-byte, so a cold clone can verify
# provenance by rebuilding rather than trusting a copy. The tiny fixture is a three-page capture
# with no committed builder; its identity is recorded, not reproducible.
EXPECTED = {
    "tiny": (12288, "75848fa83f696ac65f683bc22c90139cbe3bae36e0bad13d90dd2c20e45afb7a"),
    "w9": (8085504, "f65d581dbef12f634ded07c1e5933fa6558e641dab435718ad29dfbc65d0db82"),
}
print("loadavg:", loadavg(), flush=True)
check_identity("tiny", TINY, EXPECTED["tiny"])
check_identity("w9", W9, EXPECTED["w9"])


def parse_only():
    engine._parse_cached.cache_clear()
    for sql in texts:
        engine._parse_cached(sql)


def loop():
    db = Database(TINY)
    for sql in texts:
        db.run(sql)
    db.close()


p = capture("PARSEONLY", parse_only)
l = capture("LOOP", loop)
show("PARSEONLY", p)
show("LOOP", l)

_d = Database(W9); root = _d.tables["t"].entry["root"]; _d.close()


def block():
    pager = Pager(W9); tree = btree.BTree(pager); rows = 0
    for _rowid, payload in tree.scan(root):
        record.decode(payload); rows += 1
    pager.close(); return rows


b = capture("BLOCK", block)
print(f"BLOCK rows={block()} n={len(b)} ms: {[round(x,1) for x in b]} "
      f"median={b[len(b)//2]:.1f} min={b[0]:.1f} max={b[-1]:.1f} "
      f"spread={(b[-1]-b[0])/b[len(b)//2]*100:.1f}%", flush=True)
