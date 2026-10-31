# MiniDB

A SQL database engine built as a conformance project, scored against real SQLite.

- **Goal:** maximize the conformance pass rate over time. Every milestone reports a score
  line: `SCORE <tier>.<n>: <passed>/<total> (<pct>%) [Δ vs last]`.
- **Reference:** SQLite 3.54.0, pinned. SQLite's quirks are ground truth.
- **Rules of the road** (ladder, gates, scoring policy, budget): [STATE.md](STATE.md).
- **Roles:** Research owns corpus/fuzzer/benchmarks, Build owns engine code, Default owns
  the backlog and review. Nobody writes engine code who isn't Build; nobody reports a
  score without raw test output behind it.

## Using it from Python

```python
from minidb.server import serve
from minidb.client import Client, ServerError

server = serve("demo.db", port=8765)          # one process, one handle, M25's lock model
client = Client("127.0.0.1", 8765)
client.execute("CREATE TABLE t (a INTEGER)")
client.execute("INSERT INTO t VALUES (1), (2)")
columns, rows = client.execute("SELECT sum(a) FROM t")
print(columns, rows)                           # ['sum(a)'] [(3,)]

try:
    client.execute("SELECT * FROM missing")
except ServerError as exc:                     # typed, and the wording is the engine's own
    print(exc)                                 # no such table: missing
client.close()
```

`EXPLAIN QUERY PLAN <select>` prints MiniDB's own plan (`SCAN` with a reason, or
`SEARCH … USING INDEX`). It is deliberately **not** SQLite's bytecode — that output describes
SQLite's VM, and matching it would be imitation rather than conformance.

Status and next tasks: [STATE.md](STATE.md).
