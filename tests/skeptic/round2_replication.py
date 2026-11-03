"""Skeptic round 2, surface 3 — replication under crash and divergence.

Pre-registered in LOG.md before this file existed (seed 20261103):
  R1  real SIGKILL during resolve — 10 stratified delays in [0, D1] + 1
      control; replica sha in {pre-resolve, primary} after the kill, resolve
      re-runs to convergence, no acknowledged row lost
  R2  torn replog tail — 12 stratified byte offsets in the last 160 bytes;
      parse yields a contiguous prefix 1..k or a typed error (typed = raised
      from a minidb module), never a gap or out-of-file page ids, and a
      subsequent sync (<=3) or resolve restores sha-identity with lag 0
  R3  ship racing primary writes — 5 rounds of a subprocess writer (20 inserts
      of constant 7) against 20 in-process sync attempts; every successful
      sync lands on a state that reopens with count in [5, 25] and
      sum == 7 * count, or the attempt raises typed/hangs <= bound; final
      sync converges (lag 0, sha equal)
  R4  divergence cycling — 3 refuse->resolve->refuse cycles; refusal names the
      base seq, both files byte-unchanged after each refusal, heal loses no
      acknowledged row
  R5  real SIGKILL during _apply — 15 stratified delays in [0, D5] + 1
      control; sha in {pre, post}, reopen works, row comparison
  R6  replay-idempotence control — 3 double-applies, byte-identical

Violation = any outcome outside those rules; every iteration prints raw.
Exit 1 on any violation.
"""
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.replication import (Primary, Replica, ReplicationConflict,  # noqa: E402
                                ReplicationError, sha256_file)
from minidb.storage.pager import read_repl_log  # noqa: E402

SEED = 20261103
HANG = 10.0
VIOLATIONS = []


def say(line):
    print(line, flush=True)


def violate(text):
    VIOLATIONS.append(text)
    say(f"  VIOLATION {text}")


def typed(exc):
    return exc.__class__.__module__.split(".")[0] == "minidb"


def remove_db(path):
    for suffix in ("", ".wal", ".replog", ".shipped", ".shipped.tmp"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def read_line(proc, timeout):
    box = []

    def target():
        try:
            box.append(proc.stdout.readline())
        except Exception as exc:  # noqa: BLE001
            box.append(f"<reader error: {exc!r}>")

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    return box[0] if box else ""


def spawn(args):
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)


def seed_payload(primary, rows=800):
    primary.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    primary.run("BEGIN")
    body = "y" * 900
    for i in range(1, rows + 1):
        primary.run(f"INSERT INTO t VALUES ({i}, '{body}')")
    primary.run("COMMIT")


def seed_small(primary):
    primary.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(1, 6):
        primary.run(f"INSERT INTO t VALUES ({i}, 'x{i}')")


def seed_seven(primary):
    primary.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    primary.run("INSERT INTO t VALUES (1, 7), (2, 7), (3, 7), (4, 7), (5, 7)")


def rows_of(path, sql="SELECT count(*), sum(v) FROM t"):
    db = Database(path)
    try:
        return db.run(sql)[1][0]
    finally:
        db.close()


def attempt(fn, bound=HANG):
    """Run fn in a thread; return ('ok', value) | ('typed'|'other', repr) | 'hang'."""
    box = {}

    def target():
        try:
            box["ok"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(bound)
    if thread.is_alive():
        return ("hang", None)
    if "err" in box:
        exc = box["err"]
        return ("typed" if typed(exc) else "other", repr(exc))
    return ("ok", box.get("ok"))


# ---------------------------------------------------------------- R1: resolve

def r1_setup(tag, workdir):
    d = os.path.join(workdir, f"r1-{tag}")
    os.makedirs(d)
    pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
    primary = Primary(pp)
    seed_payload(primary)
    replica = Replica(rp)
    replica.sync(primary)
    handle = replica.open()
    handle.run("INSERT INTO t VALUES (90001, 'local-divergent')")
    handle.close()
    primary.run("INSERT INTO t VALUES (90002, 'primary-new')")
    primary.close()
    pre_sha = sha256_file(rp)
    primary_sha = sha256_file(pp)
    acknowledged = rows_of(pp, "SELECT count(*) FROM t")
    return d, pp, rp, pre_sha, primary_sha, acknowledged


def child_resolve(rp, pp):
    print("RESOLVE-START", flush=True)
    primary = Primary(pp)
    Replica(rp).resolve(primary)
    print("RESOLVE-END", flush=True)
    primary.close()


def r1(workdir):
    # control: measure resolve duration D1 and confirm the path works
    d, pp, rp, pre_sha, primary_sha, ack = r1_setup("ctl", workdir)
    primary = Primary(pp)
    start = time.time()
    Replica(rp).resolve(primary)
    D1 = time.time() - start
    primary.close()
    post = sha256_file(rp)
    say(f"R1 control: D1={D1 * 1000:.1f}ms converged={post == primary_sha} "
        f"ack_rows={ack}")
    if post != primary_sha:
        violate(f"R1 control resolve did not converge ({post[:12]} != "
                f"{primary_sha[:12]})")
    shutil.rmtree(d, ignore_errors=True)

    rng = random.Random(SEED + 1)
    for i in range(10):
        d, pp, rp, pre_sha, primary_sha, ack = r1_setup(f"it{i}", workdir)
        delta = D1 * (i + rng.random()) / 10
        proc = spawn(["--child-resolve", rp, pp])
        line = read_line(proc, HANG)
        if line is None or line.strip() != "RESOLVE-START":
            try:
                proc.kill()
                proc.wait(timeout=HANG)
            except Exception:  # noqa: BLE001
                pass
            err = (proc.stderr.read() or "").strip()
            violate(f"R1 it{i}: marker not seen ({line!r}) stderr={err[:300]!r}")
            shutil.rmtree(d, ignore_errors=True)
            continue
        time.sleep(delta)
        os.kill(proc.pid, signal.SIGKILL)
        rc = proc.wait(timeout=HANG)
        if rc != -signal.SIGKILL:
            err = (proc.stderr.read() or "").strip()
            violate(f"R1 it{i}: victim exit {rc} expected -9; stderr={err[:300]!r}")
        state_sha = sha256_file(rp)
        state = ("pre" if state_sha == pre_sha
                 else "post" if state_sha == primary_sha else "OTHER")
        say(f"R1 it{i} delta={delta * 1000:.2f}ms rc={rc} state={state}")
        if state == "OTHER":
            violate(f"R1 it{i}: replica state neither pre nor post "
                    f"({state_sha[:12]})")
        # resolve must be re-runnable to convergence, no acknowledged row lost
        try:
            primary = Primary(pp)
            Replica(rp).resolve(primary)
            primary.close()
        except Exception as exc:  # noqa: BLE001
            violate(f"R1 it{i}: re-resolve raised {exc!r} "
                    f"(typed={typed(exc) if isinstance(exc, Exception) else '?'})")
            shutil.rmtree(d, ignore_errors=True)
            continue
        final = sha256_file(rp)
        if final != primary_sha:
            violate(f"R1 it{i}: re-resolve did not converge ({final[:12]})")
        count = rows_of(rp, "SELECT count(*) FROM t")[0]
        if count != ack:
            violate(f"R1 it{i}: rows {count} != acknowledged {ack}")
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------- R2: torn log

def r2(workdir):
    rng = random.Random(SEED + 2)
    for i in range(12):
        d = os.path.join(workdir, f"r2-{i}")
        os.makedirs(d)
        pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
        primary = Primary(pp)
        seed_small(primary)
        replica = Replica(rp)
        replica.sync(primary)
        primary.close()
        log = pp + ".replog"
        size = os.path.getsize(log)
        lo = max(1, size - 160)
        offset = lo + int(rng.random() * (size - lo))
        with open(log, "r+b") as handle:
            handle.truncate(offset)
        primary_sha = sha256_file(pp)

        # (a) the log must parse as a contiguous prefix or raise typed
        try:
            records = read_repl_log(log)
            seqs = [seq for seq, _ in records]
            if seqs != list(range(1, len(seqs) + 1)):
                violate(f"R2 it{i}: torn parse has a gap: {seqs}")
            page_limit = os.path.getsize(pp)
            bad = [pid for _, pages in records for pid in pages
                   if pid < 1 or pid * 4096 > page_limit + 4096]
            if bad:
                violate(f"R2 it{i}: garbage page ids {bad[:5]} "
                        f"(file size {page_limit})")
            outcome = f"prefix-1..{len(seqs)} (of size {size} -> {offset})"
        except Exception as exc:  # noqa: BLE001
            if not typed(exc):
                violate(f"R2 it{i}: torn parse raised non-typed {exc!r}")
            outcome = f"typed-error {exc.__class__.__name__}: {exc}"
        say(f"R2 it{i} truncate {size}->{offset}: {outcome}")

        # (b) shipping or resolving must restore identity; silent under-ship fails
        converged = False
        for attempt_no in range(3):
            primary = Primary(pp)
            kind, val = attempt(lambda p=primary, r=replica: r.sync(p))
            primary.close()
            say(f"R2 it{i} sync#{attempt_no}: {kind} {val!r}"[:200])
            if kind == "other":
                violate(f"R2 it{i}: sync raised non-typed {val}")
                break
            if kind == "ok" and sha256_file(rp) == primary_sha:
                converged = True
                break
        if not converged:
            try:
                primary = Primary(pp)
                Replica(rp).resolve(primary)
                primary.close()
                converged = sha256_file(rp) == primary_sha
            except Exception as exc:  # noqa: BLE001
                violate(f"R2 it{i}: resolve after tear raised {exc!r} "
                        f"(typed={typed(exc)})")
        if not converged:
            violate(f"R2 it{i}: no convergence after sync<=3 + resolve")
        else:
            lag = Replica(rp).lag(Primary(pp))
            say(f"R2 it{i} converged lag={lag}")
            if lag != 0:
                violate(f"R2 it{i}: converged but lag={lag}")
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- R3: ship vs writer

def child_writer(pp):
    print("WRITER-READY", flush=True)
    primary = Primary(pp)
    for i in range(100, 120):
        primary.run(f"INSERT INTO t VALUES ({i}, 7)")
        time.sleep(0.01)
    primary.close()
    print("WRITER-DONE", flush=True)


def r3(workdir):
    for rnd in range(5):
        d = os.path.join(workdir, f"r3-{rnd}")
        os.makedirs(d)
        pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
        primary = Primary(pp)
        seed_seven(primary)
        replica = Replica(rp)
        replica.sync(primary)
        primary.close()
        primary_sha = sha256_file(pp)

        proc = spawn(["--child-writer", pp])
        line = read_line(proc, HANG)
        if line is None or line.strip() != "WRITER-READY":
            try:
                proc.kill()
                proc.wait(timeout=HANG)
            except Exception:  # noqa: BLE001
                pass
            violate(f"R3 r{rnd}: writer marker not seen ({line!r}) "
                    f"stderr={(proc.stderr.read() or '')[:300]!r}")
            shutil.rmtree(d, ignore_errors=True)
            continue

        hangs = others = typed_n = ok_n = 0

        def do_sync(rep=replica, path=pp):
            p = Primary(path)          # construction inside the bound: the
            try:                       # writer's LOCK_EX must not wedge the
                return rep.sync(p)     # main thread outside the watchdog
            finally:
                p.close()

        for i in range(20):
            kind, val = attempt(do_sync)
            if kind == "ok":
                ok_n += 1
                state = rows_of(rp)
                count, total = state
                if not (5 <= count <= 25) or total != 7 * count:
                    violate(f"R3 r{rnd} sync{i}: torn state count={count} "
                            f"sum={total}")
            elif kind == "hang":
                hangs += 1
                violate(f"R3 r{rnd} sync{i}: sync hung > {HANG}s")
            elif kind == "typed":
                typed_n += 1
            else:
                others += 1
                violate(f"R3 r{rnd} sync{i}: non-typed {val}")
        rc = proc.wait(timeout=HANG + 5)
        say(f"R3 r{rnd} writer rc={rc} syncs ok={ok_n} typed={typed_n} "
            f"hang={hangs} other={others}")
        if rc != 0:
            violate(f"R3 r{rnd}: writer exit {rc}")

        for _ in range(3):
            primary = Primary(pp)
            kind, val = attempt(lambda p=primary, r=replica: r.sync(p), bound=HANG)
            primary.close()
            if kind == "ok" and sha256_file(rp) == primary_sha:
                break
        final_sha = sha256_file(rp)
        count, total = rows_of(rp)
        lag = Replica(rp).lag(Primary(pp))
        say(f"R3 r{rnd} final converged={final_sha == primary_sha} lag={lag} "
            f"count={count} sum={total}")
        if final_sha != primary_sha or lag != 0 or count != 25 or total != 175:
            violate(f"R3 r{rnd}: final not converged "
                    f"(sha={final_sha == primary_sha}, lag={lag}, "
                    f"count={count}, sum={total})")
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- R4: divergence cycle

def r4(workdir):
    d = os.path.join(workdir, "r4")
    os.makedirs(d)
    pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
    primary = Primary(pp)
    seed_small(primary)
    replica = Replica(rp)
    replica.sync(primary)
    base_ack = rows_of(pp, "SELECT count(*) FROM t")[0]

    for cycle in range(3):
        handle = replica.open()
        handle.run(f"INSERT INTO t VALUES (9{cycle}01, 'local-{cycle}')")
        handle.close()
        primary.run(f"INSERT INTO t VALUES (9{cycle}02, 'primary-{cycle}')")
        before = (sha256_file(rp), sha256_file(pp))
        try:
            replica.sync(primary)
            violate(f"R4 cycle{cycle}: divergent sync was NOT refused")
            continue
        except ReplicationConflict as exc:
            named = f"base seq {replica.marker()[0]}" in str(exc) or \
                exc.base_seq is not None
            say(f"R4 cycle{cycle} refused: base_seq={exc.base_seq} "
                f"local={exc.local_commits} named={named}")
            if not named:
                violate(f"R4 cycle{cycle}: refusal does not name the base seq")
        except Exception as exc:  # noqa: BLE001
            violate(f"R4 cycle{cycle}: wrong error type {exc!r}")
            continue
        after = (sha256_file(rp), sha256_file(pp))
        if after != before:
            violate(f"R4 cycle{cycle}: a refusal mutated a file "
                    f"({before} -> {after})")
        replica.resolve(primary)
        if sha256_file(rp) != sha256_file(pp):
            violate(f"R4 cycle{cycle}: heal did not converge")
        count = rows_of(rp, "SELECT count(*) FROM t")[0]
        # acknowledged rows: base + one primary insert per cycle, no locals
        if count != base_ack + cycle + 1:
            violate(f"R4 cycle{cycle}: rows {count} != acknowledged "
                    f"{base_ack + cycle + 1}")
    primary.close()
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- R5: kill during apply

def child_apply(work, pp, seq):
    print("APPLY-START", flush=True)
    primary = Primary(pp)
    batch = primary.batches_since(seq)[0]
    Replica(work)._apply(batch)
    print("APPLY-END", flush=True)
    primary.close()


def r5(workdir):
    d = os.path.join(workdir, "r5")
    os.makedirs(d)
    pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
    primary = Primary(pp)
    seed_payload(primary, rows=40)
    replica = Replica(rp)
    replica.sync(primary)
    primary.run("INSERT INTO t VALUES (91001, 'in-flight-a')")
    primary.run("INSERT INTO t VALUES (91002, 'in-flight-b')")
    base_seq = replica.marker()[0]
    batch = primary.batches_since(base_seq)[0]
    primary.close()
    pre_sha = sha256_file(rp)
    primary_sha = sha256_file(pp)

    # control: time the apply on a shadow copy; also yields post state
    shadow = rp + ".shadow"
    shutil.copyfile(rp, shadow)
    start = time.time()
    Replica(shadow)._apply(batch)
    D5 = time.time() - start
    post_sha = sha256_file(shadow)
    say(f"R5 control: D5={D5 * 1000:.1f}ms post!=pre={post_sha != pre_sha}")
    os.remove(shadow)

    rng = random.Random(SEED + 5)
    for i in range(15):
        work = rp + f".it{i}"
        shutil.copyfile(rp, work)
        delta = D5 * (i + rng.random()) / 15
        proc = spawn(["--child-apply", work, pp, str(base_seq)])
        line = read_line(proc, HANG)
        if line is None or line.strip() != "APPLY-START":
            try:
                proc.kill()
                proc.wait(timeout=HANG)
            except Exception:  # noqa: BLE001
                pass
            violate(f"R5 it{i}: marker not seen ({line!r}) "
                    f"stderr={(proc.stderr.read() or '')[:300]!r}")
            remove_db(work)
            continue
        time.sleep(delta)
        os.kill(proc.pid, signal.SIGKILL)
        rc = proc.wait(timeout=HANG)
        if rc != -signal.SIGKILL:
            violate(f"R5 it{i}: victim exit {rc} expected -9")
        state_sha = sha256_file(work)
        state = ("pre" if state_sha == pre_sha
                 else "post" if state_sha == post_sha else "OTHER")
        say(f"R5 it{i} delta={delta * 1000:.2f}ms rc={rc} state={state}")
        if state == "OTHER":
            violate(f"R5 it{i}: state neither pre nor post ({state_sha[:12]})")
        try:
            db = Database(work)
            count = db.run("SELECT count(*) FROM t")[1][0][0]
            db.close()
        except Exception as exc:  # noqa: BLE001
            violate(f"R5 it{i}: reopen raised {exc!r}")
            remove_db(work)
            continue
        # one batch is applied (batches_since(...)[0], the landed test's
        # pattern), so the states are 40 (pre) or 41 (post that batch)
        if count not in (40, 41):
            violate(f"R5 it{i}: row count {count} not in {{40, 41}}")
        remove_db(work)
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- R6: idempotence

def r6(workdir):
    for i in range(3):
        d = os.path.join(workdir, f"r6-{i}")
        os.makedirs(d)
        pp, rp = os.path.join(d, "primary.db"), os.path.join(d, "replica.db")
        primary = Primary(pp)
        seed_small(primary)
        replica = Replica(rp)
        replica.sync(primary)
        primary.run("INSERT INTO t VALUES (77, 'replay')")
        batch = primary.batches_since(replica.marker()[0])[-1]
        primary.close()
        work = rp + ".w"
        shutil.copyfile(rp, work)
        Replica(work)._apply(batch)
        once = sha256_file(work)
        Replica(work)._apply(batch)
        twice = sha256_file(work)
        say(f"R6 it{i} idempotent={once == twice}")
        if once != twice:
            violate(f"R6 it{i}: double apply changed the file")
        remove_db(work)
        shutil.rmtree(d, ignore_errors=True)


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--child-resolve":
        child_resolve(sys.argv[2], sys.argv[3])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--child-writer":
        child_writer(sys.argv[2])
        return 0
    if len(sys.argv) >= 5 and sys.argv[1] == "--child-apply":
        child_apply(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        return 0

    say(f"command: {' '.join(sys.argv)}")
    say(f"seed: {SEED}  hang bound: {HANG}s  "
        f"counts: R1 10+1, R2 12, R3 5x20, R4 3, R5 15+1, R6 3")
    workdir = tempfile.mkdtemp(prefix="sk2-repl-")
    try:
        r1(workdir)
        r2(workdir)
        r3(workdir)
        r4(workdir)
        r5(workdir)
        r6(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"violations: {len(VIOLATIONS)}")
    if VIOLATIONS:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
