"""Per-character tokenizer cost by token class -- the instrument that named `advance()` as target.

Why it exists: the whole-function `tokenize` timings could not say *which* class costs, and two
changes were made blind before this ran (one neutral, one a regression). Isolating classes showed
cost rising 1,315 ns/char (strings) to 3,253 ns/char (punctuation) -- far above a bare Python loop
-- which points at `advance()`'s per-character line/col bookkeeping, not at any token class.

Run it at both ends of the scanner rewrite: the honest before/after comparison needs the same
instrument, same load window, same N. Usage: python3 tests/skeptic/profile_g6_lexer_classes.py
Exit 2 if cost has fallen below 300 ns/char in every class, i.e. the diagnosis is spent.
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from minidb.sql.lexer import tokenize  # noqa: E402

N = 4000
CASES = {
    "ident-heavy": "SELECT alpha_beta, gamma_delta FROM table_one WHERE col_xyz = other_col",
    "number-heavy": "SELECT 1, 22.5, 3e10 FROM t WHERE x > 12345.678",
    "string-heavy": "SELECT 'aaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbb' FROM t",
    "punct-heavy": "SELECT (((1))) FROM t WHERE a=1 AND b=2 AND c=3 OR d=4",
}

print("loadavg:", open("/proc/loadavg").read().split()[0])
worst = 0.0
for name, sql in CASES.items():
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(N):
            tokenize(sql)
        samples.append((time.perf_counter() - start) / N * 1e6)
    samples.sort()
    per_char = samples[2] / len(sql) * 1000
    worst = max(worst, per_char)
    print(f"{name:14s} {samples[2]:7.1f} us/statement  {len(sql):3d} chars  {per_char:7.2f} ns/char")

sys.exit(2 if worst < 300 else 0)
