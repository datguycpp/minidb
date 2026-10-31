"""Documentation runner (milestone 33).

Every fenced block in README.md and docs/*.md is executed:

  ```python   -> run as a script (cwd = repo root, timeout, must exit 0)
  ```sql      -> run through MiniDB, statement by statement, in a fresh in-memory database

A `sql` block must be self-contained — it creates what it uses — which is the same house rule the
test suite follows: a block that depends on a neighbour's state breaks the moment someone reorders
the page. Blocks tagged with anything else (text, bash, …) are skipped and counted, so the header
says how much was skipped rather than implying everything ran.

    python3 tools/docs_runner.py            # all documents
    python3 tools/docs_runner.py README.md  # one document
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402
from minidb.sql.parser import ParseError  # noqa: E402

FENCE = re.compile(r"^```(\w*)\s*$")
SKIP_LANGUAGES = {"", "text", "bash", "sh", "console", "json", "sqlite"}


def blocks(path):
    """Yield (language, start_line, code) for every fenced block in a markdown file."""
    language, start, lines = None, 0, []
    with open(path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            match = FENCE.match(raw.rstrip("\n"))
            if match and language is None:
                language, start, lines = match.group(1).lower(), number, []
                continue
            if raw.rstrip("\n").startswith("```") and language is not None:
                yield language, start, "".join(lines)
                language = None
                continue
            if language is not None:
                lines.append(raw)
    if language is not None:
        raise SystemExit(f"{path}:{start}: unterminated code fence")


def run_python(code, label):
    with tempfile.TemporaryDirectory() as workdir:
        script = os.path.join(workdir, "block.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(code)
        environment = dict(os.environ, PYTHONPATH=ROOT)
        result = subprocess.run([sys.executable, script], cwd=workdir, env=environment,
                                capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "failed"
    return True, "ran"


def strip_comments(code):
    """Drop `--` comments so a trailing annotation is not parsed as a statement."""
    lines = []
    for line in code.splitlines():
        cut = line.find("--")
        lines.append(line if cut < 0 else line[:cut])
    return "\n".join(lines)


def run_sql(code, label):
    db = Database()
    statements = [part.strip() for part in strip_comments(code).split(";") if part.strip()]
    try:
        for statement in statements:
            db.run(statement)
    except (ExecError, ParseError) as exc:
        return False, f"{statement.splitlines()[0][:60]}: {exc}"
    finally:
        db.close()
    return True, f"{len(statements)} statements"


def run_document(path):
    failures, ran, skipped = [], 0, 0
    for language, line, code in blocks(path):
        label = f"{os.path.relpath(path, ROOT)}:{line}"
        if language in SKIP_LANGUAGES:
            skipped += 1
            continue
        if language == "python":
            ok, detail = run_python(code, label)
        elif language == "sql":
            ok, detail = run_sql(code, label)
        else:
            skipped += 1
            continue
        ran += 1
        print(f"{'ok  ' if ok else 'FAIL'} {label} [{language}] {detail}")
        if not ok:
            failures.append(label)
    return failures, ran, skipped


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    paths = args.paths or [os.path.join(ROOT, "README.md")] + sorted(
        glob.glob(os.path.join(ROOT, "docs", "*.md")))
    failures, ran, skipped = [], 0, 0
    for path in paths:
        doc_failures, doc_ran, doc_skipped = run_document(path)
        failures.extend(doc_failures)
        ran += doc_ran
        skipped += doc_skipped
    print(f"DOCS RUNNER: {ran - len(failures)}/{ran} blocks ran, {skipped} skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
