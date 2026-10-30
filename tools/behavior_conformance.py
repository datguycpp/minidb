#!/usr/bin/env python3
"""Behavioural conformance against real SQLite (milestone 2; directives added with t1b).

Syntax conformance asked "does it parse"; this asks "does it produce the same answer". Each
case in tests/corpus/ runs against the pinned `sqlite3` CLI and against MiniDB, and
the case passes only when both agree — on rows and column names when SQLite succeeds, or on
the error message when SQLite fails.

Normalisation is deliberately minimal and lives here, not in the engine: SQLite's JSON output
already gives typed values, so the only rewrite is stripping the CLI's error framing.

Directives (ratified spec: docs/conformance-spec.md, Directives roadmap):

- `--directive: reopen` is POSITIONAL: it closes the current phase. Statements before the
  marker run in one database; statements after it run after both engines have closed and
  reopened the same file (SQLite: one `sqlite3` invocation per phase on a temp file; MiniDB:
  `Database(path)`, close, `Database(path)` again). A case may reopen several times. The
  result compared is the final phase's last statement; an error anywhere is compared as an
  error, so rejection agreement still counts after a reopen.
- `--directive: sort` is case-level: both sides' rows are sorted by JSON repr before compare.
- The loader FAILS LOUDLY on: an unknown directive name after the marker, and colon-shaped
  lines whose key is within edit distance 1 of a known directive name (`--reopn:`) — the
  misspelled-directive trap that would otherwise no-op into a fake pass. Prose comments that
  merely start with a directive word (`-- reopen: rows written before close...`) stay
  comments: the marker is the only directive syntax, and corpus prose predates it.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402
from minidb.sql.parser import ParseError, parse  # noqa: E402

CORPUS_DIR = os.path.join(ROOT, "tests", "corpus")
# The tier-1 suite is everything that uses the blank-line case format. `syntax.txt` deliberately
# does NOT match this: it is one-statement-per-line with `#!` reject markers, consumed by
# tools/syntax_conformance.py, and feeding it to this runner would produce nonsense.
DEFAULT_CORPORA = ["behavior.txt"] + sorted(
    os.path.basename(p) for p in glob.glob(os.path.join(CORPUS_DIR, "t1*.txt"))
)
PINNED = "3.54.0"

# The CLI frames errors differently per class: "Error: near line 1: ...", "Parse error: ...",
# "Runtime error near line 2: ...". The framing is the tool's, not SQLite's, so it is stripped.
ERROR_FRAMING = re.compile(r"^(?:Parse |Runtime )?[Ee]rror(?:[:]? near line \d+)?[:]\s*")

KNOWN_DIRECTIVES = {"sort", "reopen"}
DIRECTIVE_RE = re.compile(r"^--\s*directive\s*:\s*([A-Za-z_][A-Za-z0-9_-]*)\s*$")
SHAPED_RE = re.compile(r"^--\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:")


def _levenshtein(a, b):
    """Edit distance, small inputs only — used to catch near-miss directive keys."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sqlite_version():
    out = subprocess.run(["sqlite3", "--version"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def corpus_revision():
    """Short git commit of tests/corpus/ (+ -dirty when there are uncommitted edits).

    Spec requirement (docs/conformance-spec.md, runner interface): every posted score
    names its corpus revision so the number is reproducible without trusting the poster.
    """
    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", "tests/corpus"],
            cwd=ROOT, capture_output=True, text=True, check=True)
        revision = log.stdout.strip() or "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "tests/corpus"],
            cwd=ROOT, capture_output=True, text=True, check=True)
        if status.stdout.strip():
            revision += "-dirty"
        return revision
    except Exception:
        return "unknown"


def clean_error(text):
    """Strip the CLI's line-number framing; keep SQLite's actual complaint."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        line = ERROR_FRAMING.sub("", line)
        if line:
            return line
    return (text or "").strip()


def run_sqlite(phases):
    """Run each phase as its own `sqlite3` invocation.

    Single phase: one invocation on :memory: (the pre-directive behaviour). Multiple phases:
    every phase runs against the SAME temp-file database, one invocation per phase — each
    invocation is a fresh open+close, which is exactly the reopen cycle. An error in any
    phase aborts; the result compared is the final phase's output.
    """
    path = ":memory:" if len(phases) == 1 else None
    result = None
    for index, statements in enumerate(phases):
        # Join with ';' but do NOT append a trailing one: the last statement must reach SQLite
        # terminated by EOF exactly as it reaches MiniDB, or an incomplete statement reports
        # `near ";": syntax error` on one side and `incomplete input` on the other.
        script = ";\n".join(statements) + "\n"
        db_path = path
        if db_path is None:
            assert REOPEN_TMPDIR is not None, "multi-phase case without a temp dir"
            db_path = os.path.join(REOPEN_TMPDIR, "sqlite.db")
        proc = subprocess.run(
            ["sqlite3", "-json", db_path],
            input=script, capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or proc.stderr.strip():
            return {"error": clean_error(proc.stderr)}
        result = proc.stdout.strip()
    rows = json.loads(result) if result else []
    columns = list(rows[0].keys()) if rows else []
    return {"columns": columns, "rows": rows}


def sqlite_float(value):
    """SQLite's -json output formats floats with 15 significant digits (`%!.15g`).

    Comparing Python's repr (17 digits) against that is comparing serialisers, not values, so
    our side is formatted the same way before comparison.
    """
    return float(f"{value:.15g}")


def normalise_values(rows):
    return [{key: (sqlite_float(value) if isinstance(value, float) else value)
             for key, value in row.items()} for row in rows]


def run_minidb(phases):
    """Mirror of run_sqlite's phase structure for MiniDB.

    Single phase: one in-memory Database (the pre-directive behaviour). Multiple phases:
    Database(path) → execute → close → Database(path) again, once per phase, same file.

    Statements go through `db.run`, the engine's documented statement path, NOT
    `db.execute`: run() carries the autocommit rule (every statement outside an explicit
    transaction is durable when it returns) that matches the sqlite3 CLI's default
    behaviour, which is what the differential design assumes. Found by the t1d landing:
    execute() left pre-transaction statements uncommitted, so a phase that ended in
    ROLLBACK discarded more than SQLite's side did — harness semantics, not engine
    semantics, diverged. The engine was right.
    """
    result = ([], [])
    path = None if len(phases) == 1 else os.path.join(REOPEN_TMPDIR, "minidb.db")  # type: ignore[arg-type]
    assert path is None or REOPEN_TMPDIR is not None, "multi-phase case without a temp dir"
    db = Database(path) if path else Database()
    try:
        for index, statements in enumerate(phases):
            last = index == len(phases) - 1
            try:
                for statement in statements:
                    result = db.run(statement)
            except (ExecError, ParseError) as exc:
                return {"error": str(exc)}
            if not last:
                db.close()
                db = Database(path)
        columns, rows = result
        keys = list(columns)
        return {"columns": keys,
                "rows": normalise_values([dict(zip(keys, row)) for row in rows])}
    finally:
        if path:
            db.close()


REOPEN_TMPDIR = None  # set per-case by main(); owned by the runner, deleted after the case


def load_cases(path):
    """Return [(case_directives, phases), ...] in file order.

    `phases` is a list of statement lists split by `--directive: reopen` markers: one phase
    = no reopen (all statements together); N phases = N-1 reopen cycles. `case_directives`
    holds non-positional directives (sort). The loader raises SystemExit on directive
    mistakes so a typo can never no-op into a fake pass (see module docstring).
    """
    cases = []
    phases, case_directives = [[]], set()
    saw_directive_marker = False

    def flush(lineno):
        nonlocal phases, case_directives
        if not any(phases):
            if case_directives:
                raise SystemExit(f"{path}:{lineno}: directive with no case block after it")
            return
        if not phases[-1]:
            raise SystemExit(
                f"{path}:{lineno}: reopen directive with no statements after it")
        cases.append((case_directives, phases))
        phases, case_directives = [[]], set()

    with open(path, encoding="utf-8") as fh:
        lineno = 0
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                flush(lineno)
                continue
            if stripped.startswith("--"):
                m = DIRECTIVE_RE.match(stripped)
                if m:
                    saw_directive_marker = True
                    name = m.group(1)
                    if name not in KNOWN_DIRECTIVES:
                        raise SystemExit(
                            f"{path}:{lineno}: unknown directive '{name}' "
                            f"(known: {', '.join(sorted(KNOWN_DIRECTIVES))})")
                    if name == "reopen":
                        if not phases[-1] and len(phases) > 1:
                            raise SystemExit(
                                f"{path}:{lineno}: redundant reopen — no statements since "
                                f"the previous reopen")
                        if phases[-1]:
                            phases.append([])
                    else:
                        case_directives.add(name)
                    continue
                m2 = SHAPED_RE.match(stripped)
                if m2:
                    key = m2.group(1)
                    if key not in KNOWN_DIRECTIVES:
                        near = [k for k in sorted(KNOWN_DIRECTIVES)
                                if _levenshtein(key.lower(), k) <= 1]
                        if near:
                            raise SystemExit(
                                f"{path}:{lineno}: '--{key}:' looks like a misspelled "
                                f"directive — use `--directive: {near[0]}`")
                continue  # plain comment (provenance headers, case descriptions)
            phases[-1].append(stripped.rstrip(";").strip())
    if not saw_directive_marker and not any(phases):
        raise SystemExit(f"{path}: no cases found (file is empty or comment-only)")
    flush(lineno + 1)
    return cases


def sort_rows(result):
    """--directive: sort — both sides' rows sorted by JSON repr before compare."""
    if "error" in result:
        return result
    return {**result, "rows": sorted(result.get("rows", []),
                                     key=lambda r: json.dumps(r, sort_keys=True))}


def normalise_numbers(rows):
    """Format floats identically on both sides before comparing.

    SQLite's JSON writer emits 15 significant digits (`%!.15g`), which does not always
    round-trip to the same binary value Python computes for AVG(). Comparing raw floats would
    measure the two serialisers against each other, so both sides go through the same
    formatting. This is a normalizer rule, owned by Research (docs/conformance-spec.md).
    """
    return [{key: (float(f"{value:.15g}") if isinstance(value, float) else value)
             for key, value in row.items()} for row in rows]


def compare(expected, actual):
    if "error" in expected:
        if "error" not in actual:
            return False, f"SQLite errored ({expected['error']!r}), MiniDB returned rows"
        return expected["error"] == actual["error"], f"{expected['error']!r} vs {actual['error']!r}"
    if "error" in actual:
        return False, f"SQLite returned rows, MiniDB errored: {actual['error']!r}"
    if expected["columns"] and actual["columns"] and expected["columns"] != actual["columns"]:
        return False, f"columns {expected['columns']} vs {actual['columns']}"
    left = normalise_numbers(expected["rows"])
    right = normalise_numbers(actual["rows"])
    return left == right, f"{left} vs {right}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--corpus", action="append", metavar="PATH",
                    help="corpus file or glob; repeatable. Default: behavior.txt + t1*.txt")
    args = ap.parse_args()

    if shutil.which("sqlite3") is None:
        print("reference binary 'sqlite3' not found on PATH", file=sys.stderr)
        return 2

    version = sqlite_version()

    paths = []
    for pattern in (args.corpus or DEFAULT_CORPORA):
        pattern = pattern if os.path.isabs(pattern) or os.sep in pattern else os.path.join(CORPUS_DIR, pattern)
        matched = sorted(glob.glob(pattern))
        if not matched:
            print(f"no corpus files matched {pattern!r}", file=sys.stderr)
            return 2
        paths.extend(matched)

    results = []
    per_corpus = {}
    for path in paths:
        name = os.path.relpath(path, ROOT)
        cases = load_cases(path)
        corpus_passed = 0
        for index, (case_directives, phases) in enumerate(cases, 1):
            global REOPEN_TMPDIR
            REOPEN_TMPDIR = tempfile.mkdtemp(prefix="minidb-reopen-") if len(phases) > 1 else None
            try:
                expected = run_sqlite(phases)
                actual = run_minidb(phases)
            finally:
                if REOPEN_TMPDIR:
                    shutil.rmtree(REOPEN_TMPDIR, ignore_errors=True)
                    REOPEN_TMPDIR = None
            if "sort" in case_directives:
                expected, actual = sort_rows(expected), sort_rows(actual)
            ok, detail = compare(expected, actual)
            corpus_passed += 1 if ok else 0
            results.append({"corpus": name, "case": index, "statements": phases[-1],
                            "directives": sorted(case_directives),
                            "reopens": len(phases) - 1, "pass": ok, "detail": detail})
        per_corpus[name] = (corpus_passed, len(cases))

    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    pct = 100.0 * passed / total if total else 0.0

    if args.json:
        print(json.dumps({"passed": passed, "total": total, "reference": version,
                          "corpus_revision": corpus_revision(), "results": results}, indent=2))
        return 0 if passed == total else 1

    for r in results:
        if args.verbose or not r["pass"]:
            tag = "ok  " if r["pass"] else "FAIL"
            suffix = f"  [reopens={r['reopens']}]" if r["reopens"] else ""
            print(f"{tag} {r['corpus']} case {r['case']}: {r['statements'][-1]}{suffix}")
            if not r["pass"]:
                print(f"     {r['detail']}")

    print()
    for name, (ok, count) in per_corpus.items():
        print(f"  {name}: {ok}/{count}")
    print(f"BEHAVIOUR CONFORMANCE: {passed}/{total} ({pct:.1f}%)")
    print(f"corpus: {' + '.join(sorted(per_corpus))}")
    print(f"corpus revision: {corpus_revision()}")
    print(f"reference: SQLite {version}" + ("" if version == PINNED else f"  *** PINNED IS {PINNED} ***"))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
