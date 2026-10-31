"""MiniDB's shell (milestone 29).

The gate set is declared, not inherited from sqlite3's dot-command zoo: `.tables`, `.schema [name]`,
`.quit`, `.help`, plus CSV import/export (the ladder asks for those, and the acceptance is a
round-trip). Conformance lives in the SQL; the dot-commands are ours.
"""

import csv
import os
import sys

from .engine import Database, ExecError
from .sql.parser import ParseError

PROMPT = "minidb> "
CONTINUATION = "   ...> "


def format_rows(rows):
    lines = []
    for row in rows:
        cells = []
        for value in row:
            if value is None:
                cells.append("")
            elif isinstance(value, bytes):
                cells.append(value.decode("utf-8", "surrogateescape"))
            else:
                cells.append(str(value))
        lines.append("|".join(cells))
    return lines


def schema_text(db, name=None):
    """CREATE statements, read from the same catalog sqlite_master exposes."""
    statements = []
    for key in sorted(db.tables):
        table = db.tables[key]
        if name and table.name.lower() != name.lower():
            continue
        statements.append(db.run(f"SELECT sql FROM sqlite_master WHERE name = '{table.name}'")[1][0][0])
    for view in db.views.values():
        if name and view.name.lower() != name.lower():
            continue
        statements.append(view.sql or f"CREATE VIEW {view.name}")
    return statements


class Shell:
    def __init__(self, db, out=None):
        self.db = db
        self.out = out or sys.stdout
        self.buffer = ""

    def say(self, text=""):
        print(text, file=self.out)

    def run_sql(self, statement):
        try:
            result = self.db.run(statement)
        except (ExecError, ParseError) as exc:
            self.say(f"Error: {exc}")
            return
        if not isinstance(result, tuple):     # DDL/DML return counts, not rows
            return
        columns, rows = result
        for line in format_rows(rows):
            self.say(line)

    def dot_command(self, line):
        parts = line[1:].split()
        if not parts:
            return True
        command, args = parts[0].lower(), parts[1:]
        if command in ("quit", "exit", "q"):
            return False
        if command == "help":
            self.say(".tables            list tables")
            self.say(".schema [name]     show CREATE statements")
            self.say(".import FILE TABLE  load a CSV file into a table")
            self.say(".export TABLE FILE  write a table to a CSV file")
            self.say(".quit              leave the shell")
            return True
        if command == "tables":
            self.say(" ".join(sorted(table.name for table in self.db.tables.values())))
            return True
        if command == "schema":
            for statement in schema_text(self.db, args[0] if args else None):
                self.say(statement + ";")
            return True
        if command in ("import", "export"):
            if len(args) != 2:
                self.say(f"Error: .{command} needs two arguments")
                return True
            self.csv_command(command, args[0], args[1])
            return True
        self.say(f"Error: unknown command .{command}")
        return True

    def csv_command(self, command, first, second):
        if command == "import":
            path, table = first, second
            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            if not rows:
                self.say("Error: empty CSV file")
                return
            columns = ", ".join(rows[0])
            for row in rows[1:]:
                values = ", ".join(_literal(value) for value in row)
                try:
                    self.db.run(f"INSERT INTO {table} ({columns}) VALUES ({values})")
                except (ExecError, ParseError) as exc:
                    self.say(f"Error: {exc}")
                    return
            self.say(f"imported {len(rows) - 1} rows into {table}")
            return
        table, path = first, second
        try:
            result = self.db.run(f"SELECT * FROM {table}")
            if result is None:
                self.say(f"Error: no such table: {table}")
                return
            columns, rows = result
        except (ExecError, ParseError) as exc:
            self.say(f"Error: {exc}")
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows([["" if value is None else value for value in row] for row in rows])
        self.say(f"exported {len(rows)} rows from {table}")


def _literal(value):
    if value == "":
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else None
    db = Database(path)
    shell = Shell(db)
    interactive = sys.stdin.isatty()
    try:
        while True:
            if interactive:
                print(PROMPT if not shell.buffer else CONTINUATION, end="", flush=True)
            line = sys.stdin.readline()
            if not line:
                break
            stripped = line.strip()
            if not shell.buffer and stripped.startswith("."):
                if not shell.dot_command(stripped):
                    break
                continue
            if not stripped and not shell.buffer:
                continue
            shell.buffer = (shell.buffer + " " + stripped).strip()
            if not shell.buffer.endswith(";"):
                continue
            statement, shell.buffer = shell.buffer[:-1], ""
            if statement.strip():
                shell.run_sql(statement)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
