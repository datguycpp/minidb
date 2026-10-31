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
        self._ensure_catalog()
        self._load_catalog()

    def _ensure_catalog(self):
        """A rolled-back transaction can leave the header pointing at no catalog at all."""
        if self.pager.catalog_root == 0:
            self.pager.set_catalog_root(self.tree.create())

    # -- catalog ------------------------------------------------------------
    def _load_catalog(self):
        for key, payload in self.tree.scan(self.pager.catalog_root):
            entry = json.loads(payload.decode("utf-8"))
            entry["key"] = key
            self.catalog[entry["name"].lower()] = entry

    def save_catalog_entry(self, entry):
        """Public alias: the engine persists index definitions through this."""
        self._save_catalog_entry(entry)

    def _save_catalog_entry(self, entry):
        payload = json.dumps({
            "name": entry["name"],
            "sql": entry["sql"],
            "root": entry["root"],
            "next_rowid": entry["next_rowid"],
            "indexes": entry.get("indexes", []),
            "kind": entry.get("kind", "table"),
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
                 "next_rowid": 1, "key": self._next_catalog_key(), "indexes": []}
        self.catalog[name.lower()] = entry
        self._save_catalog_entry(entry)
        return entry

    def create_view(self, name, sql):
        if name.lower() in self.catalog:
            raise StorageError(f"view {name} already exists")
        entry = {"name": name, "sql": sql, "root": 0, "next_rowid": 0,
                 "key": self._next_catalog_key(), "indexes": [], "kind": "view"}
        self.catalog[name.lower()] = entry
        self._save_catalog_entry(entry)
        return entry

    def drop_entry(self, name):
        entry = self.catalog.pop(name.lower(), None)
        if entry is None:
            return False
        # Catalog rows are keyed, so removal means rewriting the catalog tree without that key.
        remaining = [(other["key"], other) for other in self.catalog.values()]
        root = self.tree.create()
        for key, other in remaining:
            root = self.tree.insert(root, key, json.dumps({
                "name": other["name"], "sql": other["sql"], "root": other["root"],
                "next_rowid": other["next_rowid"], "indexes": other.get("indexes", []),
                "kind": other.get("kind", "table"),
            }).encode("utf-8"))
        self.pager.set_catalog_root(root)
        return True

    def rewrite_rows(self, entry, rows):
        """Replace every row of a table. No B+tree delete exists yet (M19 owns it), so an
        UPDATE or DELETE rebuilds the tree; the old pages stay allocated until the freelist."""
        root = self.tree.create()
        for rowid, values in rows:
            root = self.tree.insert(root, rowid, record.encode(values))
        entry["root"] = root
        self._save_catalog_entry(entry)

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

    def update_row(self, entry, rowid, values):
        """Replace one row in place. The B+tree replaces a payload for an existing key, so this is
        O(log n) instead of the whole-table rewrite an UPDATE used to pay."""
        root = self.tree.insert(entry["root"], rowid, record.encode(values))
        entry["root"] = root
        self._save_catalog_entry(entry)
        return rowid

    def iter_rows(self, entry):
        for _, payload in self.tree.scan(entry["root"]):
            yield record.decode(payload)

    def iter_rows_with_ids(self, entry):
        for rowid, payload in self.tree.scan(entry["root"]):
            yield rowid, record.decode(payload)

    def search_row(self, entry, rowid):
        payload = self.tree.search(entry["root"], rowid)
        return None if payload is None else record.decode(payload)

    # -- transactions -------------------------------------------------------
    def commit(self):
        self.pager.commit()

    def rollback(self):
        """Uncommitted work lives in the pager's dirty buffer; dropping it is the rollback."""
        self.pager.rollback()
        self.catalog = {}
        self._ensure_catalog()
        self._load_catalog()

    # -- lifecycle ----------------------------------------------------------
    def close(self):
        self.pager.close()
