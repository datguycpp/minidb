"""Disk storage: catalog + one B+tree per table (milestone 3).

The catalog is itself a B+tree (keys are catalog rowids) holding one row per table:
`{name, sql, root, next_rowid}`. Storing the original CREATE statement rather than a
serialised AST is deliberate — it is what makes reopening a database cheap and keeps schema
parsing in exactly one place, the parser.
"""

import json

from . import record
from .btree import BTree
from .pager import Pager, StorageError


class DiskStorage:
    def __init__(self, path):
        self.pager = Pager(path)
        self.tree = BTree(self.pager)
        self.catalog = {}          # lower(name) -> {"name", "sql", "root", "next_rowid", "key"}
        if self.pager.catalog_root == 0:
            self.pager.set_catalog_root(self.tree.create())
        self._load_catalog()

    # -- catalog ------------------------------------------------------------
    def _load_catalog(self):
        for key, payload in self.tree.scan(self.pager.catalog_root):
            entry = json.loads(payload.decode("utf-8"))
            entry["key"] = key
            self.catalog[entry["name"].lower()] = entry

    def _save_catalog_entry(self, entry):
        payload = json.dumps({
            "name": entry["name"],
            "sql": entry["sql"],
            "root": entry["root"],
            "next_rowid": entry["next_rowid"],
        }).encode("utf-8")
        new_root = self.tree.insert(self.pager.catalog_root, entry["key"], payload)
        if new_root != self.pager.catalog_root:
            self.pager.set_catalog_root(new_root)

    def _next_catalog_key(self):
        keys = [entry["key"] for entry in self.catalog.values()]
        return max(keys) + 1 if keys else 1

    # -- tables -------------------------------------------------------------
    def create_table(self, name, sql):
        if name.lower() in self.catalog:
            raise StorageError(f"table {name} already exists")
        root = self.tree.create()
        entry = {"name": name, "sql": sql, "root": root,
                 "next_rowid": 1, "key": self._next_catalog_key()}
        self.catalog[name.lower()] = entry
        self._save_catalog_entry(entry)
        return entry

    def table(self, name):
        try:
            return self.catalog[name.lower()]
        except KeyError:
            raise StorageError(f"no such table: {name}") from None

    def insert_row(self, entry, values):
        rowid = entry["next_rowid"]
        payload = record.encode(values)
        root = self.tree.insert(entry["root"], rowid, payload)
        entry["root"] = root
        entry["next_rowid"] = rowid + 1
        self._save_catalog_entry(entry)
        return rowid

    def iter_rows(self, entry):
        for _, payload in self.tree.scan(entry["root"]):
            yield record.decode(payload)

    # -- lifecycle ----------------------------------------------------------
    def close(self):
        self.pager.close()
