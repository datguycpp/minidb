"""Where the parse slice goes: profile for goal 6's gate 3, taken pre-edit at 52cb9e8.

Statements: `SELECT count(*) FROM t WHERE id > {i}` for i in range(2000), all distinct, so every
one misses `_parse_cached`'s 1024-entry LRU (engine.py:894) and the loop is parse-dominated.
The fixture is irrelevant to this measurement -- parsing never reads a table -- which is exactly
why an earlier version of this capture measured the wrong thing when it ran the statements
against a 100k-row database.

Exit 2 if the lexer is no longer the dominant share, so a future reader can tell a stale claim
from a current one.
"""
import cProfile
import io
import pstats
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import minidb.engine as engine  # noqa: E402

COUNT = 2000
texts = [f"SELECT count(*) FROM t WHERE id > {i}" for i in range(COUNT)]
engine._parse_cached.cache_clear()

profile = cProfile.Profile()
profile.enable()
for sql in texts:
    engine._parse_cached(sql)
profile.disable()

stream = io.StringIO()
stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
stats.print_stats(12)
table = "\n".join(stream.getvalue().splitlines()[4:18])
print(f"profile: {COUNT} distinct statements, cumulative top 12\n{table}")

def cumtime(line):
    return float(line.split()[3])

lexer = next((cumtime(l) for l in table.splitlines() if "lexer.py" in l and "tokenize" in l), None)
total = next((cumtime(l) for l in table.splitlines() if "parser.py" in l and "(parse)" in l), None)
share = lexer / total if lexer and total else None
print(f"\nlexer share of parse: {share:.0%} ({lexer:.3f}s of {total:.3f}s)" if share
      else "\nlexer or parse row not found")
sys.exit(0 if share and share > 0.40 else 2)
