"""Engineer's accounting probe for round 2's P3 finding -- evidence, not part of the pre-registered run.

P3 asserts that a dropper's INSERT is uncommitted and must vanish. But a dropper whose BEGIN was
refused ("database is locked") has no open transaction, so its INSERT is an ordinary autocommit
write. This probe counts both classes per round and checks whether the observed row count is
exactly 10 + (inserts that succeeded with a refused BEGIN). Result: 8/8 rounds consistent, and no
row from an acknowledged in-transaction INSERT survived.
"""
import json, socket, sys, threading, time
sys.path.insert(0, "/home/grub/projects/minidb")
from minidb.server import serve

server = serve(port=0)
host, port = server.server_address
threading.Thread(target=server.serve_forever, daemon=True).start()

def connect():
    sock = socket.create_connection((host, port), timeout=10)
    return sock, sock.makefile("rwb")

def send(stream, sql):
    stream.write(json.dumps({"sql": sql}).encode() + b"\n")
    stream.flush()
    return json.loads(stream.readline().decode())

def one(sql):
    sock, stream = connect()
    try: return send(stream, sql)
    finally: stream.close(); sock.close()

one("CREATE TABLE p3 (v INTEGER)")
ok_matches = 0
for rnd in range(1, 9):
    one("DELETE FROM p3")
    one("INSERT INTO p3 VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10)")
    began_ok, auto_ok, insert_failed = [], [], []
    def dropper(tid):
        for op in range(3):
            sock, stream = connect()
            b = send(stream, "BEGIN")
            i = send(stream, f"INSERT INTO p3 VALUES ({100 + tid * 10 + op})")
            if i.get("error") is not None:
                insert_failed.append(1)
            elif b.get("error") is None:
                began_ok.append(1)          # a real open transaction -> must roll back
            else:
                auto_ok.append(1)           # BEGIN was refused -> autocommit write
            stream.close(); sock.close()
    ts = [threading.Thread(target=dropper, args=(i,)) for i in range(6)]
    for t in ts: t.start()
    for t in ts: t.join(15)
    time.sleep(0.2)
    count = one("SELECT count(*) FROM p3")["rows"][0][0]
    autocommitted = len(auto_ok)
    consistent = (count == 10 + autocommitted)
    print(f"   detail: inserts ok-in-tx={len(began_ok)} ok-autocommit={len(auto_ok)} failed={len(insert_failed)}")
    ok_matches += consistent
    print(f"round {rnd}: count={count}  committed-inside-a-refused-BEGIN={autocommitted}  "
          f"expected-if-autocommit-semantics={10 + autocommitted}  {'consistent' if consistent else 'MISMATCH'}")
print(f"rounds consistent with autocommit semantics: {ok_matches}/8")
