"""Skeptic round 2, surface 2 — concurrent clients against the M30 server.

Pre-registered in LOG.md: 5 rounds (labels 20261103+round — no RNG: thread
interleaving is the declared nondeterminism) of four phases, socket timeout
10 s. Violation = any response outside its phase's allowlist, any timeout,
any count mismatch.

  P1  readers during an open transaction answer only from
      {pre-state rows, post-commit rows, typed `database is locked`}
  P2  racing autocommit writers: success or typed lock error only;
      final count == base + successes
  P3  disconnect-with-open-transaction storm: the lock releases within the
      bound and the uncommitted state is rolled back
  P4  readers over frozen data racing a writer inserting constant value 7:
      every answer satisfies sum == 7 * count with count in [50, 80]
      (`database is locked` allowed and counted; anything else is a violation)

Round-1 defect note (re-authored, disclosed): the v1 allowlist was a set of
tuples containing lists — unhashable — and crashed before any phase ran, so
the 5x4 matrix produced no evidence. It is a list now, and phase-level crashes
are recorded as PROBE-ERROR (exit 2) instead of killing the run, so one
instrument defect cannot zero the matrix again.

Raw per-op lines; summary block. Exit 1 on violations, 2 on probe errors only.
"""
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from minidb.server import serve  # noqa: E402

SEED_BASE = 20261103
ROUNDS = 5
TIMEOUT = 10.0
VIOLATIONS = []
PROBE_ERRORS = []
LOCKED = "database is locked"


def say(line):
    print(line, flush=True)


def violate(text):
    VIOLATIONS.append(text)
    say(f"  VIOLATION {text}")


def connect(host, port):
    sock = socket.create_connection((host, port), timeout=TIMEOUT)
    return sock, sock.makefile("rwb")


def send(stream, sql):
    stream.write((json.dumps({"sql": sql}) + "\n").encode())
    stream.flush()
    return json.loads(stream.readline().decode())


def one_shot(host, port, sql):
    sock, stream = connect(host, port)
    try:
        return send(stream, sql)
    finally:
        sock.close()


def is_locked(resp):
    return "error" in resp and resp["error"] == LOCKED


def run_phase1(host, port, rnd):
    """Readers during an open transaction; then after commit."""
    one_shot(host, port, "CREATE TABLE IF NOT EXISTS p1 (v INTEGER)")
    one_shot(host, port, "DELETE FROM p1")
    values = ",".join(f"({i})" for i in range(1, 101))
    one_shot(host, port, f"INSERT INTO p1 VALUES {values}")

    writer_sock, writer = connect(host, port)
    begin = send(writer, "BEGIN")
    insert = send(writer, "INSERT INTO p1 VALUES (101)")
    for tag, resp in (("BEGIN", begin), ("INSERT", insert)):
        if "error" in resp:
            violate(f"r{rnd} P1 setup {tag} errored: {resp['error']!r}")
            writer_sock.close()
            return

    during = []

    def reader():
        for _ in range(5):
            try:
                resp = one_shot(host, port, "SELECT count(*) FROM p1")
                during.append(("rows", resp["rows"]) if "error" not in resp
                              else ("err", resp["error"]))
            except Exception as exc:  # noqa: BLE001
                during.append(("exc", repr(exc)))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT + 5)
        if t.is_alive():
            violate(f"r{rnd} P1 reader thread hung")

    # list, not set: the row payloads are lists (unhashable) — round-1 defect
    allowed_during = [("rows", [[100]]), ("err", LOCKED)]
    for item in during:
        say(f"r{rnd} P1 during {item}")
        if item not in allowed_during:
            violate(f"r{rnd} P1 during-txn response {item!r} outside allowlist")

    commit = send(writer, "COMMIT")
    if "error" in commit:
        violate(f"r{rnd} P1 COMMIT errored: {commit['error']!r}")
    writer_sock.close()

    for _ in range(4):
        resp = one_shot(host, port, "SELECT count(*) FROM p1")
        say(f"r{rnd} P1 after {resp}")
        if "error" in resp or resp["rows"] != [[101]]:
            violate(f"r{rnd} P1 after-commit response {resp!r}")


def run_phase2(host, port, rnd):
    one_shot(host, port, "CREATE TABLE IF NOT EXISTS p2 (v INTEGER)")
    one_shot(host, port, "DELETE FROM p2")
    successes = [0]
    lock = threading.Lock()
    outcomes = []

    def writer(thread_id):
        for op in range(5):
            value = thread_id * 10 + op
            try:
                resp = one_shot(host, port, f"INSERT INTO p2 VALUES ({value})")
                if "error" in resp:
                    tag = "locked" if is_locked(resp) else f"err:{resp['error']}"
                else:
                    tag = "ok"
                    with lock:
                        successes[0] += 1
            except Exception as exc:  # noqa: BLE001
                tag = f"exc:{exc!r}"
            outcomes.append((thread_id, op, tag))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT + 5)
        if t.is_alive():
            violate(f"r{rnd} P2 writer thread hung")

    for thread_id, op, tag in outcomes:
        say(f"r{rnd} P2 t{thread_id} op{op} {tag}")
        if tag not in ("ok", "locked"):
            violate(f"r{rnd} P2 outcome {tag!r} outside ok|locked")

    count = one_shot(host, port, "SELECT count(*) FROM p2")
    say(f"r{rnd} P2 final count={count} successes={successes[0]}")
    if "error" in count or count["rows"] != [[successes[0]]]:
        violate(f"r{rnd} P2 final count {count!r} != base+successes "
                f"{successes[0]}")


def run_phase3(host, port, rnd):
    one_shot(host, port, "CREATE TABLE IF NOT EXISTS p3 (v INTEGER)")
    one_shot(host, port, "DELETE FROM p3")
    one_shot(host, port, "INSERT INTO p3 VALUES (1),(2),(3),(4),(5),"
                         "(6),(7),(8),(9),(10)")

    def dropper(thread_id):
        for op in range(3):
            try:
                sock, stream = connect(host, port)
                began = send(stream, "BEGIN")
                if "error" in began:
                    # BEGIN refused: this dropper has no open transaction, so
                    # its INSERT would be an ordinary autocommit write and
                    # would legitimately commit (v2's accounting probe, 8/8
                    # rounds). Skip it — this phase tests disconnects with an
                    # OPEN transaction only, and the invariant stays count==10.
                    stream.close()
                    sock.close()
                    continue
                insert = send(stream, f"INSERT INTO p3 VALUES ({100 + thread_id * 10 + op})")
                if "error" in insert:
                    violate(f"r{rnd} P3 dropper t{thread_id} op{op} INSERT "
                            f"errored after accepted BEGIN: {insert['error']!r}")
                stream.close()
                sock.close()                      # vanish without COMMIT
            except Exception as exc:  # noqa: BLE001
                violate(f"r{rnd} P3 dropper t{thread_id} op{op} raised {exc!r}")

    threads = [threading.Thread(target=dropper, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT + 5)
        if t.is_alive():
            violate(f"r{rnd} P3 dropper thread hung")

    deadline = time.time() + TIMEOUT
    resp = None
    while time.time() < deadline:
        resp = one_shot(host, port, "SELECT count(*) FROM p3")
        if "error" not in resp:
            break
        time.sleep(0.05)
    say(f"r{rnd} P3 post-storm {resp}")
    if resp is None or "error" in resp:
        violate(f"r{rnd} P3 lock not released within {TIMEOUT}s: {resp!r}")
    elif resp["rows"] != [[10]]:
        violate(f"r{rnd} P3 state {resp!r} != rolled-back 10 rows")


def run_phase4(host, port, rnd):
    one_shot(host, port, "CREATE TABLE IF NOT EXISTS p4 (v INTEGER)")
    one_shot(host, port, "DELETE FROM p4")
    one_shot(host, port, "INSERT INTO p4 VALUES " + ",".join("(7)" for _ in range(50)))

    locked_seen = [0]

    def writer():
        for _ in range(30):
            try:
                resp = one_shot(host, port, "INSERT INTO p4 VALUES (7)")
                if "error" in resp and not is_locked(resp):
                    violate(f"r{rnd} P4 writer error {resp['error']!r}")
            except Exception as exc:  # noqa: BLE001
                violate(f"r{rnd} P4 writer raised {exc!r}")

    def reader(thread_id):
        for op in range(20):
            try:
                resp = one_shot(host, port, "SELECT count(*), sum(v) FROM p4")
            except Exception as exc:  # noqa: BLE001
                violate(f"r{rnd} P4 reader t{thread_id} op{op} raised {exc!r}")
                continue
            if "error" in resp:
                if is_locked(resp):
                    locked_seen[0] += 1
                    say(f"r{rnd} P4 reader t{thread_id} op{op} locked")
                else:
                    violate(f"r{rnd} P4 reader error {resp['error']!r}")
                continue
            count, total = resp["rows"][0]
            if total != 7 * count or not (50 <= count <= 80):
                violate(f"r{rnd} P4 reader t{thread_id} op{op} torn/inconsistent "
                        f"count={count} sum={total}")

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    for t in threads:
        t.start()
    writer_thread.join(TIMEOUT + 5)
    if writer_thread.is_alive():
        violate(f"r{rnd} P4 writer hung")
    for t in threads:
        t.join(TIMEOUT + 5)
        if t.is_alive():
            violate(f"r{rnd} P4 reader thread hung")

    final = one_shot(host, port, "SELECT count(*), sum(v) FROM p4")
    say(f"r{rnd} P4 final={final} locked_seen={locked_seen[0]}")
    if "error" in final or final["rows"] != [[80, 560]]:
        violate(f"r{rnd} P4 final state {final!r} != [[80, 560]]")


PHASES = (("P1", run_phase1), ("P2", run_phase2),
          ("P3", run_phase3), ("P4", run_phase4))


def main():
    server = serve(port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    say(f"command: {' '.join(sys.argv)}")
    say(f"server: {host}:{port}  rounds: {ROUNDS}  "
        f"labels: {SEED_BASE}..{SEED_BASE + ROUNDS - 1}  timeout: {TIMEOUT}s")
    say("note: thread interleaving is the nondeterminism; there is no RNG")

    try:
        for rnd in range(ROUNDS):
            say(f"--- round {rnd} (label {SEED_BASE + rnd}) ---")
            for name, fn in PHASES:
                try:
                    fn(host, port, rnd)
                except Exception as exc:  # noqa: BLE001 - instrument defect
                    PROBE_ERRORS.append(f"r{rnd} {name}: {exc!r}")
                    say(f"  PROBE-ERROR r{rnd} {name}: {exc!r}")
    finally:
        server.shutdown()
        server.server_close()

    for item in PROBE_ERRORS:
        print(f"  PROBE-ERROR {item}")
    print(f"rounds: {ROUNDS}  phases per round: 4  "
          f"violations: {len(VIOLATIONS)}  probe errors: {len(PROBE_ERRORS)}")
    if VIOLATIONS:
        print("RESULT: FAIL")
        return 1
    if PROBE_ERRORS:
        print("RESULT: ERROR")
        return 2
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
