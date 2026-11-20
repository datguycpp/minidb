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


def select_series(argv):
    """Which series to run; all by default.

    A gate-1 capture needs only `BLOCK`, whose input is the reproducible W9 file. The tiny input
    feeds `PARSEONLY` and `LOOP` and has no committed builder, so a machine without it can still
    take gate 1's number without measuring something else under the missing fixture's name.
    """
    wanted = {"PARSEONLY", "LOOP", "BLOCK"}
    for index, arg in enumerate(argv):
        if arg == "--series" and index + 1 < len(argv):
            chosen = {name.strip().upper() for name in argv[index + 1].split(",") if name.strip()}
            unknown = chosen - wanted
            if unknown or not chosen:
                raise SystemExit("baseline: --series takes a subset of PARSEONLY,LOOP,BLOCK")
            return chosen
        if arg.startswith("--series="):
            chosen = {name.strip().upper() for name in arg.split("=", 1)[1].split(",") if name.strip()}
            unknown = chosen - wanted
            if unknown or not chosen:
                raise SystemExit("baseline: --series takes a subset of PARSEONLY,LOOP,BLOCK")
            return chosen
    return wanted


WANTED = select_series(sys.argv[1:])


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


# Fixtures are resolved only for the series actually requested: a missing tiny input must block
# the series that measure it, not a BLOCK capture whose input is the reproducible W9 file. And
# PARSEONLY never opens a database, so it must not resolve one either.
TINY = fixture("G6_TINY_DB", "g6-tiny.db", LEGACY_TINY) if "LOOP" in WANTED else None
W9 = fixture("G6_W9_DB", "w9-fixture.db", LEGACY_W9) if "BLOCK" in WANTED else None
# The frozen inputs' identities. The W9 file is reproduced by the committed harness shape
# (benchmark.py's create + one transaction + build_rows(100000, seed=7)); the tiny file is built by
# tests/skeptic/make_tiny_fixture.py, whose bytes were produced by the pre-change revision and are
# reproduced by HEAD — both are now reproducible by construction, and both are committed so the
# input is present as well as derivable.
EXPECTED = {
    "tiny": (12288, "64a9d5417b7ec44389ffbde5b7e6da52ec53aff157156fbfd3e3caed3a4d4c30"),
    "w9": (8085504, "f65d581dbef12f634ded07c1e5933fa6558e641dab435718ad29dfbc65d0db82"),
}
print("loadavg:", loadavg(), flush=True)
if TINY is not None:
    check_identity("tiny", TINY, EXPECTED["tiny"])
if W9 is not None:
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


if "PARSEONLY" in WANTED:
    p = capture("PARSEONLY", parse_only)
    show("PARSEONLY", p)
if "LOOP" in WANTED:
    l = capture("LOOP", loop)
    show("LOOP", l)

if "BLOCK" in WANTED:
    _d = Database(W9); root = _d.tables["t"].entry["root"]; _d.close()


def block():
    pager = Pager(W9); tree = btree.BTree(pager); rows = 0
    for _rowid, payload in tree.scan(root):
        record.decode(payload); rows += 1
    pager.close(); return rows


if "BLOCK" in WANTED:
    b = capture("BLOCK", block)
    print(f"BLOCK rows={block()} n={len(b)} ms: {[round(x,1) for x in b]} "
          f"median={b[len(b)//2]:.1f} min={b[0]:.1f} max={b[-1]:.1f} "
          f"spread={(b[-1]-b[0])/b[len(b)//2]*100:.1f}%", flush=True)
