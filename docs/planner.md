# Planner: how an access path is chosen (milestone 5)

One rule, written down because "the planner picked it" is not an explanation. It is deliberately
cheap and deterministic: no statistics yet (ANALYZE is M21), so estimates come from the indexes
themselves.

## Eligible predicates

Only conjunctions of `column = literal` are index-eligible. `OR`, ranges and expressions stay
full scans. That is not laziness — an index that returns a superset and forgets the residual
filter would produce wrong rows, so partial support is worse than none until M21 can price it.

A composite index is eligible only when **every** one of its columns is constrained:

```sql
CREATE INDEX idx_ab ON t (a, b);
SELECT * FROM t WHERE a = 1;            -- full scan: prefix match is not used
SELECT * FROM t WHERE a = 1 AND b = 2;  -- index
```

## The rule

1. **No predicate, or no eligible index** → full scan.
2. **Estimate each eligible index** as `len(bucket)` for its equality key: the number of rows a
   lookup would hand to the filter. This is exact for the current data, not a guess.
3. **Pick the smallest estimate**, breaking ties by: more constrained columns → unique index →
   index name. The tie-breaks are ordered so the same query always takes the same path.
4. **If the best estimate is not smaller than the table's row count**, scan instead. An index
   that touches every row only adds a lookup.

Nothing here reads `ANALYZE` statistics, and nothing is cached between statements: the estimate
is recomputed per query, which is why the rule is stated in terms of the index contents rather
than a cost model that could go stale.

## What `last_plan` reports

```json
{
  "table": "sales",
  "access": "index",
  "index": "idx_amount",
  "reason": "smallest-estimate",
  "estimated_rows": 1,
  "rows_examined": 1,
  "rows_matched": 1,
  "candidates": [
    {"name": "idx_region", "columns": ["region"], "estimated_rows": 500},
    {"name": "idx_amount", "columns": ["amount"], "estimated_rows": 1}
  ]
}
```

`reason` is one of `no-predicate`, `no-eligible-index`, `index-not-selective`,
`smallest-estimate`. Every considered index appears in `candidates`, so a choice can be audited
without re-running the query.

`tools/plan_check.py` prints exactly this for five scenarios (no index, single candidate, two
candidates, unselective index, no eligible index) and is the evidence the conformance score
cannot provide: differential testing compares results, so an index that is created and ignored
scores identically to one that is used.

## Known gaps, owned by later milestones

| Gap | Owner |
| --- | --- |
| No cost model over statistics, no selectivity from ANALYZE | M21 |
| Ranges (`<`, `BETWEEN`) never use an index | M21 |
| `OR` never becomes an index union | M21 |
| No covering indexes (index-only scans still fetch the row) | M22 |
| Index choice is not cached; estimate recomputed per statement | M18 (page cache) |
