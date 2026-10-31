# Plan vocabulary

`EXPLAIN QUERY PLAN <select>` returns four columns — `id`, `parent`, `notused`, `detail` — the
shape tools already read. The `detail` vocabulary is **ours**, and it is deliberately small:

| Detail | Meaning |
| --- | --- |
| `SCAN <table>` | every row of the table is visited |
| `  reason: no-predicate` | there was no `WHERE` to narrow anything |
| `  reason: no-eligible-index` | no index covers the predicate |
| `  reason: index-not-selective` | an index matched, but a scan was estimated cheaper |
| `SEARCH <table> USING INDEX <name>` | rows are found through an index |
| `  estimated rows: N` | the planner's estimate for that lookup |
| `NESTED LOOP` | a multi-source query, sources listed beneath |
| `  TABLE <name>` | one source of the loop (or scan), by alias if aliased |
| `  GROUP BY` | grouping happens after the sources are read |
| `COMPOUND <op>` | a set operation, each branch planned in turn |
| `SCAN` | a complex-path scan (join/group source listed in `TABLE` lines) |
| `MiniDB: only SELECT plans are explained` | `EXPLAIN` of a write is refused after parsing |

This is not SQLite's vocabulary and does not try to be: SQLite's `EXPLAIN` prints its virtual
machine's bytecode, which describes SQLite's implementation rather than any standard. MiniDB's
plan describes MiniDB's own choices, and the two are not comparable line by line.

## Scans and searches

A lookup with no usable index is a scan, and the plan says why.

```sql
CREATE TABLE t (a INTEGER, b TEXT);
INSERT INTO t VALUES (1, 'x'), (2, 'y');
EXPLAIN QUERY PLAN SELECT * FROM t WHERE b = 'x';
```

With an index on the filtered column, and enough rows for the planner to prefer it, the same query
becomes a search. The fixtures are fixed so this example is deterministic.

```sql
CREATE TABLE big (a INTEGER, b TEXT);
INSERT INTO big VALUES (1, 'v1'), (2, 'v2'), (3, 'v3'), (4, 'v4'), (5, 'v5'), (6, 'v6'), (7, 'v7'), (8, 'v8'), (9, 'v9'), (10, 'v10'), (11, 'v11'), (12, 'v12'), (13, 'v13'), (14, 'v14'), (15, 'v15'), (16, 'v16'), (17, 'v17'), (18, 'v18'), (19, 'v19'), (20, 'v20'), (21, 'v21'), (22, 'v22'), (23, 'v23'), (24, 'v24'), (25, 'v25'), (26, 'v26'), (27, 'v27'), (28, 'v28'), (29, 'v29'), (30, 'v30'), (31, 'v31'), (32, 'v32'), (33, 'v33'), (34, 'v34'), (35, 'v35'), (36, 'v36'), (37, 'v37'), (38, 'v38'), (39, 'v39'), (40, 'v40'), (41, 'v41'), (42, 'v42'), (43, 'v43'), (44, 'v44'), (45, 'v45'), (46, 'v46'), (47, 'v47'), (48, 'v48'), (49, 'v49'), (50, 'v50'), (51, 'v51'), (52, 'v52'), (53, 'v53'), (54, 'v54'), (55, 'v55'), (56, 'v56'), (57, 'v57'), (58, 'v58'), (59, 'v59'), (60, 'v60');
CREATE INDEX idx_a ON big (a);
EXPLAIN QUERY PLAN SELECT * FROM big WHERE a = 42;
```

## Joins and compounds

```sql
CREATE TABLE t (a INTEGER);
CREATE TABLE u (a INTEGER);
EXPLAIN QUERY PLAN SELECT * FROM t, u WHERE t.a = u.a;
```

```sql
CREATE TABLE t (a INTEGER);
EXPLAIN QUERY PLAN SELECT a FROM t UNION SELECT a FROM t;
```

## What it refuses

`EXPLAIN` of a write is parsed, then refused: there is no bytecode here to show, and inventing one
would be worse than saying so.

```text
CREATE TABLE t (a INTEGER);
EXPLAIN INSERT INTO t VALUES (1);
```

That block is expected to raise `only SELECT plans are explained`; the docs runner treats a
raised engine error as a failed block, so it is kept out of the runnable set on purpose and shown
here as prose instead.

```sql
CREATE TABLE t (a INTEGER);
EXPLAIN QUERY PLAN SELECT * FROM t;
```
