#!/usr/bin/env python3
"""
minidb — a minimal but REAL persistent database, standard library only.

Storage model
-------------
A database is a directory. Inside it:
    meta.json           {"tables": {"users": {"columns": [...], "indexes": ["age"]}}}
    <table>.json        {"columns": [...], "rows": [{...}, {...}]}

Every mutating statement rewrites the affected table file atomically
(write to <file>.tmp, then os.replace). That is the durability mechanism:
no in-memory-only state — close the process, reopen the directory, and
the rows are still there. Indexes are NOT stored on disk; they are rebuilt
from the table file on open (see BTree below), exactly like a real DB
rebuilding an index after a crash / restart.

Index model
-----------
Each indexed column gets a B-tree (see BTree class). Keys are column
values, values are lists of row ids (positions in the table's row list).
Equality lookups go through BTree.search(); range lookups walk the
tree in key order. Non-indexed predicates fall back to a full scan.
EXPLAIN <SELECT ...> prints which plan was chosen, and every SELECT
reports how many rows it examined, so index use is observable, not
claimed on faith.

What this is NOT: no transactions / WAL, no concurrency control
(single process, single writer), no JOINs, no NULLs, no secondary
storage formats. See README.md for the full list.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Optional

# ---------------------------------------------------------------------------
# B-tree (CLRS-style, minimum degree t)
# ---------------------------------------------------------------------------
# A B-tree node holds up to 2t-1 keys. With t=3 a node holds at most 5 keys
# and 6 children, so the tree stays shallow: ~5^depth rows are addressable
# with only `depth` disk/logical steps. Keys are kept sorted inside each
# node, children partition the key space, and every leaf is at the same
# depth — that balance is what makes search/insert/delete O(log n) instead
# of O(n) like a linear scan, and O(log n) with far fewer steps than a
# binary tree for on-disk data (each node = one page read).


class BTreeNode:
    def __init__(self, leaf: bool = True):
        self.leaf: bool = leaf
        self.keys: list[Any] = []        # sorted column values
        self.values: list[list[int]] = []  # row ids per key (duplicates allowed)
        self.children: list["BTreeNode"] = []

    def to_dict(self) -> dict:
        return {
            "leaf": self.leaf,
            "keys": self.keys,
            "values": self.values,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BTreeNode":
        node = cls(leaf=d["leaf"])
        node.keys = list(d["keys"])
        node.values = [list(v) for v in d["values"]]
        node.children = [cls.from_dict(c) for c in d["children"]]
        return node


class BTree:
    """B-tree with minimum degree t. Keys must be mutually comparable
    (all INT, all TEXT, … — the table enforces one type per column)."""

    def __init__(self, t: int = 3):
        if t < 2:
            raise ValueError("minimum degree t must be >= 2")
        self.t = t
        self.root = BTreeNode(leaf=True)

    # -- search -------------------------------------------------------------
    def search(self, key: Any) -> list[int]:
        """Exact-match lookup. Returns row ids, [] if absent."""
        return self._search(self.root, key)

    def _search(self, node: BTreeNode, key: Any) -> list[int]:
        i = 0
        while i < len(node.keys) and key > node.keys[i]:
            i += 1
        if i < len(node.keys) and key == node.keys[i]:
            return list(node.values[i])
        if node.leaf:
            return []
        return self._search(node.children[i], key)

    def range_search(self, op: str, key: Any) -> list[int]:
        """Ordered walk: collect row ids whose key satisfies `op key`."""
        out: list[int] = []
        for k, v in self.items():
            if op == ">" and k > key:
                out.extend(v)
            elif op == ">=" and k >= key:
                out.extend(v)
            elif op == "<" and k < key:
                out.extend(v)
            elif op == "<=" and k <= key:
                out.extend(v)
            elif op == "!=" and k != key:
                out.extend(v)
        return out

    def items(self):
        """All (key, row_ids) pairs in sorted key order (in-order walk)."""
        yield from self._items(self.root)

    def _items(self, node: BTreeNode):
        for i, k in enumerate(node.keys):
            if not node.leaf:
                yield from self._items(node.children[i])
            yield k, list(node.values[i])
        if not node.leaf:
            yield from self._items(node.children[len(node.keys)])

    def height(self) -> int:
        h, node = 1, self.root
        while not node.leaf:
            h += 1
            node = node.children[0]
        return h

    def size(self) -> int:
        return sum(len(v) for _, v in self.items())

    # -- insert -------------------------------------------------------------
    def insert(self, key: Any, row_id: int) -> None:
        root = self.root
        if len(root.keys) == 2 * self.t - 1:  # root full -> grow tree by one level
            new_root = BTreeNode(leaf=False)
            new_root.children.append(root)
            self._split_child(new_root, 0)
            self.root = new_root
        self._insert_nonfull(self.root, key, row_id)

    def _insert_nonfull(self, node: BTreeNode, key: Any, row_id: int) -> None:
        # Duplicate key anywhere in this node: share its bucket instead of
        # descending and creating a second copy of the key in a leaf
        # (search() returns the first bucket it finds, so a second copy
        # would be invisible). This is what makes non-unique indexes work.
        for j, k in enumerate(node.keys):
            if key == k:
                if row_id not in node.values[j]:
                    node.values[j].append(row_id)
                return
        i = len(node.keys) - 1
        if node.leaf:
            # Find slot; duplicate key -> append row id to existing bucket.
            while i >= 0 and key < node.keys[i]:
                i -= 1
            if i >= 0 and key == node.keys[i]:
                if row_id not in node.values[i]:
                    node.values[i].append(row_id)
                return
            node.keys.insert(i + 1, key)
            node.values.insert(i + 1, [row_id])
            return
        while i >= 0 and key < node.keys[i]:
            i -= 1
        i += 1
        if len(node.children[i].keys) == 2 * self.t - 1:
            self._split_child(node, i)
            if key > node.keys[i]:
                i += 1
            elif key == node.keys[i]:
                if row_id not in node.values[i]:
                    node.values[i].append(row_id)
                return
        self._insert_nonfull(node.children[i], key, row_id)

    def _split_child(self, parent: BTreeNode, i: int) -> None:
        t = self.t
        full = parent.children[i]
        new = BTreeNode(leaf=full.leaf)
        mid_key, mid_val = full.keys[t - 1], full.values[t - 1]
        new.keys = full.keys[t:]
        new.values = full.values[t:]
        if not full.leaf:
            new.children = full.children[t:]
            full.children = full.children[:t]
        full.keys = full.keys[: t - 1]
        full.values = full.values[: t - 1]
        parent.children.insert(i + 1, new)
        parent.keys.insert(i, mid_key)
        parent.values.insert(i, mid_val)

    # -- (de)serialisation, for inspection / debugging ----------------------
    def to_dict(self) -> dict:
        return {"t": self.t, "root": self.root.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "BTree":
        tree = cls(t=d["t"])
        tree.root = BTreeNode.from_dict(d["root"])
        return tree


# ---------------------------------------------------------------------------
# Storage / table / database
# ---------------------------------------------------------------------------

VALID_TYPES = ("INT", "TEXT", "FLOAT", "BOOL")


def coerce(value: Any, col_type: str) -> Any:
    if col_type == "INT":
        if isinstance(value, bool):
            raise ValueError("BOOL is not INT")
        return int(value)
    if col_type == "FLOAT":
        return float(value)
    if col_type == "TEXT":
        return str(value)
    if col_type == "BOOL":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.upper() in ("TRUE", "1"):
                return True
            if value.upper() in ("FALSE", "0"):
                return False
        raise ValueError(f"cannot coerce {value!r} to BOOL")
    raise ValueError(f"unknown type {col_type}")


class Table:
    def __init__(self, name: str, columns: list[tuple[str, str]]):
        self.name = name
        self.columns = columns  # [(name, TYPE)]
        self.col_types = dict(columns)
        self.rows: list[dict[str, Any]] = []
        self.indexes: dict[str, BTree] = {}  # column -> B-tree

    def create_index(self, column: str) -> BTree:
        if column not in self.col_types:
            raise ValueError(f"no column {column!r} in table {self.name!r}")
        tree = BTree(t=3)
        for rid, row in enumerate(self.rows):
            tree.insert(row[column], rid)
        self.indexes[column] = tree
        return tree

    def rebuild_indexes(self) -> None:
        for col in list(self.indexes):
            self.create_index(col)

    def insert(self, row: dict[str, Any]) -> int:
        unknown = set(row) - set(self.col_types)
        if unknown:
            raise ValueError(f"unknown column(s): {sorted(unknown)}")
        full = {c: coerce(row[c], t) if c in row else None
                for c, t in self.columns}
        # Require every column present (no NULLs in this minimal DB).
        missing = [c for c, v in full.items() if v is None]
        if missing:
            raise ValueError(f"missing value(s) for column(s): {missing} (no NULLs yet)")
        rid = len(self.rows)
        self.rows.append(full)
        for col, tree in self.indexes.items():
            tree.insert(full[col], rid)
        return rid

    def delete_where(self, predicate) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if not predicate(r)]
        deleted = before - len(self.rows)
        if deleted:
            self.rebuild_indexes()  # row ids shifted; rebuild is simplest + correct
        return deleted


class Database:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(self.path, exist_ok=True)
        self.tables: dict[str, Table] = {}
        self._load()

    # -- persistence ---------------------------------------------------------
    def _meta_path(self) -> str:
        return os.path.join(self.path, "meta.json")

    def _table_path(self, name: str) -> str:
        return os.path.join(self.path, f"{name}.json")

    def _atomic_write(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _load(self) -> None:
        if not os.path.exists(self._meta_path()):
            return
        with open(self._meta_path(), encoding="utf-8") as f:
            meta = json.load(f)
        for name, spec in meta.get("tables", {}).items():
            cols = [(c["name"], c["type"]) for c in spec["columns"]]
            table = Table(name, cols)
            tpath = self._table_path(name)
            if os.path.exists(tpath):
                with open(tpath, encoding="utf-8") as f:
                    data = json.load(f)
                table.rows = data.get("rows", [])
            self.tables[name] = table
            for col in spec.get("indexes", []):
                table.create_index(col)

    def _save_meta(self) -> None:
        meta = {"tables": {
            name: {
                "columns": [{"name": c, "type": t} for c, t in tbl.columns],
                "indexes": sorted(tbl.indexes),
            } for name, tbl in self.tables.items()}}
        self._atomic_write(self._meta_path(), meta)

    def _save_table(self, name: str) -> None:
        tbl = self.tables[name]
        self._atomic_write(self._table_path(name),
                           {"columns": [{"name": c, "type": t} for c, t in tbl.columns],
                            "rows": tbl.rows})

    def save(self) -> None:
        self._save_meta()
        for name in self.tables:
            self._save_table(name)

    # -- DDL / DML ------------------------------------------------------------
    def create_table(self, name: str, columns: list[tuple[str, str]]) -> str:
        if name in self.tables:
            raise ValueError(f"table {name!r} already exists")
        if not columns:
            raise ValueError("CREATE TABLE needs at least one column")
        for _, t in columns:
            if t not in VALID_TYPES:
                raise ValueError(f"unsupported type {t!r} (use {VALID_TYPES})")
        if len({c for c, _ in columns}) != len(columns):
            raise ValueError("duplicate column name")
        self.tables[name] = Table(name, columns)
        self._save_meta()
        self._save_table(name)
        return f"Table {name} created."

    def drop_table(self, name: str) -> str:
        if name not in self.tables:
            raise ValueError(f"no such table {name!r}")
        del self.tables[name]
        if os.path.exists(self._table_path(name)):
            os.remove(self._table_path(name))
        self._save_meta()
        return f"Table {name} dropped."

    def create_index(self, table_name: str, column: str, index_name: str = "") -> str:
        tbl = self._get(table_name)
        tree = tbl.create_index(column)
        self._save_meta()
        label = index_name or f"idx_{table_name}_{column}"
        return (f"Index {label} created on {table_name}({column}). "
                f"B-tree height={tree.height()}, entries={tree.size()}.")

    def _get(self, name: str) -> Table:
        if name not in self.tables:
            raise ValueError(f"no such table {name!r}")
        return self.tables[name]

    def insert(self, table_name: str, row: dict[str, Any]) -> str:
        tbl = self._get(table_name)
        rid = tbl.insert(row)
        self._save_table(table_name)
        return f"1 row inserted into {table_name} (row id {rid})."

    def delete(self, table_name: str, predicate) -> str:
        tbl = self._get(table_name)
        n = tbl.delete_where(predicate)
        if n:
            self._save_table(table_name)
        return f"{n} row(s) deleted from {table_name}."

    def update(self, table_name: str, assignments: dict[str, Any], predicate) -> str:
        tbl = self._get(table_name)
        n = 0
        for row in tbl.rows:
            if predicate(row):
                for col, val in assignments.items():
                    if col not in tbl.col_types:
                        raise ValueError(f"no column {col!r}")
                    row[col] = coerce(val, tbl.col_types[col])
                n += 1
        if n:
            tbl.rebuild_indexes()
            self._save_table(table_name)
        return f"{n} row(s) updated in {table_name}."

    # -- query ----------------------------------------------------------------
    def select(self, table_name: str, columns: list[str],
               predicate=None, where_clause: Optional[tuple] = None):
        """where_clause: (column, op, value) for a single simple predicate,
        used for index selection. predicate: callable(row)->bool (any WHERE).

        Returns (rows, plan, rows_examined)."""
        tbl = self._get(table_name)
        if columns == ["*"]:
            columns = [c for c, _ in tbl.columns]
        for c in columns:
            if c not in tbl.col_types:
                raise ValueError(f"no column {c!r} in table {table_name!r}")

        # Index selection: equality (or range) on an indexed column.
        plan = f"FULL TABLE SCAN ({len(tbl.rows)} row(s) examined)"
        candidate_ids: Optional[list[int]] = None
        rows_examined = len(tbl.rows)
        if where_clause is not None:
            col, op, val = where_clause
            if col in tbl.indexes:
                tree = tbl.indexes[col]
                if op == "=":
                    candidate_ids = tree.search(val)
                    plan = (f"INDEX SCAN on {table_name}({col}) using B-tree "
                            f"(height={tree.height()}) — {len(candidate_ids)} row id(s) "
                            f"via index, no full scan")
                elif op in (">", ">=", "<", "<=", "!="):
                    candidate_ids = tree.range_search(op, val)
                    plan = (f"INDEX RANGE SCAN on {table_name}({col}) using B-tree "
                            f"(height={tree.height()}) — {len(candidate_ids)} row id(s) "
                            f"via index, in key order")
                rows_examined = len(candidate_ids) if candidate_ids is not None else len(tbl.rows)

        if candidate_ids is not None:
            rows = [tbl.rows[i] for i in candidate_ids if i < len(tbl.rows)]
        else:
            rows = list(tbl.rows)
        if predicate is not None:
            rows = [r for r in rows if predicate(r)]
        return [{c: r[c] for c in columns} for r in rows], plan, rows_examined


# ---------------------------------------------------------------------------
# Tiny SQL parser (enough for the REPL / tests — not a full SQL grammar)
# ---------------------------------------------------------------------------

def _tokenize(sql: str) -> list[Any]:
    """Split SQL into tokens; quoted strings become ('STR', value)."""
    # Strip -- line comments first (demo.sql uses them). Without this a
    # comment containing ':' or '/' used to hang the tokenizer: those
    # characters matched no branch and produced an empty token with no
    # forward progress. The guard below (j == i) is a second defence
    # against the same class of bug for any other stray character.
    sql = re.sub(r"--[^\n]*", " ", sql)
    tokens: list[Any] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif ch == "'":
            j = sql.find("'", i + 1)
            if j == -1:
                raise ValueError("unterminated string literal")
            tokens.append(("STR", sql[i + 1:j].replace("''", "'")))
            i = j + 1
        elif ch in "(),;*=":
            tokens.append(ch)
            i += 1
        elif ch in "<>!":
            if i + 1 < n and sql[i + 1] == "=":
                tokens.append(ch + "=")
                i += 2
            else:
                tokens.append(ch)
                i += 1
        else:
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_.-"):
                j += 1
            if j == i:  # stray punctuation (e.g. ':') — consume it, never stall
                tokens.append(ch)
                i += 1
            else:
                tokens.append(sql[i:j])
                i = j
    return tokens


def _parse_value(tok: Any) -> Any:
    if isinstance(tok, tuple):  # ('STR', ...)
        return tok[1]
    if isinstance(tok, str):
        if re.fullmatch(r"-?\d+", tok):
            return int(tok)
        if re.fullmatch(r"-?\d+\.\d+", tok):
            return float(tok)
        if tok.upper() in ("TRUE", "FALSE"):
            return tok.upper() == "TRUE"
        return tok  # bare identifier used as a value (e.g. in SET)
    raise ValueError(f"cannot parse value {tok!r}")


def _parse_where(tokens: list[Any], pos: int):
    """Parse `col op value [AND col op value ...]` from pos.
    Returns (predicate, simple_clause_or_None, new_pos)."""
    conditions = []
    while pos < len(tokens):
        col = tokens[pos]
        op = tokens[pos + 1]
        val = _parse_value(tokens[pos + 2])
        conditions.append((col, op, val))
        pos += 3
        if pos < len(tokens) and isinstance(tokens[pos], str) and tokens[pos].upper() == "AND":
            pos += 1
            continue
        break

    def predicate(row: dict) -> bool:
        for col, op, val in conditions:
            if col not in row:
                raise ValueError(f"no column {col!r} in WHERE")
            rv = row[col]
            ok = {"=": rv == val, "!=": rv != val, "<": rv < val,
                  ">": rv > val, "<=": rv <= val, ">=": rv >= val}.get(op)
            if ok is None:
                raise ValueError(f"unsupported operator {op!r}")
            if not ok:
                return False
        return True

    simple = conditions[0] if len(conditions) == 1 else None
    return predicate, simple, pos


def execute(db: Database, sql: str):
    """Execute one statement. Returns (kind, payload, message).
    kind: 'rows' (payload = list of dicts), 'info' (payload = None)."""
    sql = sql.strip()
    if not sql:
        return "info", None, ""
    if sql.endswith(";"):
        sql = sql[:-1].strip()
    tokens = _tokenize(sql)
    if not tokens:
        return "info", None, ""
    kw = tokens[0].upper() if isinstance(tokens[0], str) else ""

    # SHOW TABLES / DESCRIBE / EXPLAIN
    if kw == "SHOW" and len(tokens) >= 2 and tokens[1].upper() == "TABLES":
        rows = [{"table": name,
                 "columns": ", ".join(f"{c} {t}" for c, t in db.tables[name].columns),
                 "indexes": ", ".join(sorted(db.tables[name].indexes)) or "(none)",
                 "rows": len(db.tables[name].rows)}
                for name in sorted(db.tables)]
        return "rows", rows, f"{len(rows)} table(s)."
    if kw == "DESCRIBE" and len(tokens) >= 2:
        tbl = db._get(tokens[1])
        rows = [{"column": c, "type": t,
                 "indexed": "yes" if c in tbl.indexes else "no"} for c, t in tbl.columns]
        return "rows", rows, f"Table {tbl.name}: {len(rows)} column(s)."
    if kw == "EXPLAIN":
        inner = sql[len("EXPLAIN"):].strip()
        kind, payload, msg = execute(db, inner)
        # Re-run the SELECT path to get its plan without printing rows.
        return "info", None, getattr(execute, "_last_plan", msg) if False else _explain(db, inner)

    # CREATE TABLE name (col TYPE, ...)
    if kw == "CREATE" and len(tokens) >= 2 and tokens[1].upper() == "TABLE":
        name = tokens[2]
        if tokens[3] != "(" or tokens[-1] != ")":
            raise ValueError("expected CREATE TABLE name (col TYPE, ...)")
        inner = tokens[4:-1]
        cols, i = [], 0
        while i < len(inner):
            cols.append((inner[i], inner[i + 1].upper()))
            i += 2
            if i < len(inner) and inner[i] == ",":
                i += 1
        return "info", None, db.create_table(name, cols)

    # CREATE INDEX [name] ON table (col)
    if kw == "CREATE" and len(tokens) >= 2 and tokens[1].upper() == "INDEX":
        rest = tokens[2:]
        if rest[0].upper() == "ON":
            idx_name, on = "", rest[1:]
        else:
            idx_name, on = rest[0], rest[1:]
        if on[0].upper() != "ON":
            raise ValueError("expected CREATE INDEX [name] ON table (col)")
        table_name, col = on[1], on[3]
        return "info", None, db.create_index(table_name, col, idx_name)

    if kw == "DROP" and len(tokens) >= 3 and tokens[1].upper() == "TABLE":
        return "info", None, db.drop_table(tokens[2])

    # INSERT INTO t [(cols)] VALUES (...)
    if kw == "INSERT" and len(tokens) >= 2 and tokens[1].upper() == "INTO":
        table_name = tokens[2]
        pos = 3
        cols = None
        if tokens[pos] == "(":
            end = tokens.index(")", pos)
            cols = [t for t in tokens[pos + 1:end] if t != ","]
            pos = end + 1
        if tokens[pos].upper() != "VALUES" or tokens[pos + 1] != "(" or tokens[-1] != ")":
            raise ValueError("expected INSERT INTO t [(cols)] VALUES (...)")
        vals = [_parse_value(t) for t in tokens[pos + 2:-1] if t != ","]
        tbl = db._get(table_name)
        if cols is None:
            cols = [c for c, _ in tbl.columns]
        if len(cols) != len(vals):
            raise ValueError(f"{len(cols)} column(s) but {len(vals)} value(s)")
        return "info", None, db.insert(table_name, dict(zip(cols, vals)))

    # SELECT cols FROM t [WHERE ...]
    if kw == "SELECT":
        try:
            frm = next(i for i, t in enumerate(tokens) if isinstance(t, str) and t.upper() == "FROM")
        except StopIteration:
            raise ValueError("SELECT needs FROM")
        cols = [t for t in tokens[1:frm] if t != ","]
        if cols == ["*"]:
            cols = ["*"]
        table_name = tokens[frm + 1]
        predicate, simple, _ = (None, None, frm + 2)
        if frm + 2 < len(tokens) and tokens[frm + 2].upper() == "WHERE":
            predicate, simple, _ = _parse_where(tokens, frm + 3)
        rows, plan, examined = db.select(table_name, cols, predicate, simple)
        execute._last_plan = plan  # type: ignore[attr-defined]
        msg = f"{len(rows)} row(s). Plan: {plan}"
        return "rows", rows, msg

    # DELETE FROM t [WHERE ...]
    if kw == "DELETE":
        if tokens[1].upper() != "FROM":
            raise ValueError("expected DELETE FROM t [WHERE ...]")
        table_name = tokens[2]
        predicate = None
        if len(tokens) > 3 and tokens[3].upper() == "WHERE":
            predicate, _, _ = _parse_where(tokens, 4)
        return "info", None, db.delete(table_name, predicate or (lambda r: True))

    # UPDATE t SET col = val [, ...] [WHERE ...]
    if kw == "UPDATE":
        table_name = tokens[1]
        if tokens[2].upper() != "SET":
            raise ValueError("expected UPDATE t SET col = val ...")
        pos, assignments = 3, {}
        while pos < len(tokens):
            col = tokens[pos]
            if tokens[pos + 1] != "=":
                raise ValueError("expected col = value in SET")
            assignments[col] = _parse_value(tokens[pos + 2])
            pos += 3
            if pos < len(tokens) and tokens[pos] == ",":
                pos += 1
                continue
            break
        predicate = None
        if pos < len(tokens) and tokens[pos].upper() == "WHERE":
            predicate, _, _ = _parse_where(tokens, pos + 1)
        return "info", None, db.update(table_name, assignments,
                                       predicate or (lambda r: True))

    raise ValueError(f"unsupported statement: {sql[:60]!r} "
                     "(supported: CREATE TABLE/INDEX, DROP TABLE, INSERT, "
                     "SELECT, UPDATE, DELETE, SHOW TABLES, DESCRIBE, EXPLAIN)")


def _explain(db: Database, inner_sql: str) -> str:
    tokens = _tokenize(inner_sql.rstrip(";"))
    if not tokens or tokens[0].upper() != "SELECT":
        raise ValueError("EXPLAIN only supports SELECT here")
    try:
        frm = next(i for i, t in enumerate(tokens) if isinstance(t, str) and t.upper() == "FROM")
    except StopIteration:
        raise ValueError("EXPLAIN needs a SELECT ... FROM ...")
    table_name = tokens[frm + 1]
    tbl = db._get(table_name)
    predicate, simple, _ = (None, None, frm + 2)
    if frm + 2 < len(tokens) and tokens[frm + 2].upper() == "WHERE":
        predicate, simple, _ = _parse_where(tokens, frm + 3)
    _, plan, examined = db.select(table_name, ["*"], predicate, simple)
    if simple is not None and simple[0] in tbl.indexes:
        return f"EXPLAIN: {plan}"
    return (f"EXPLAIN: {plan} — no usable index for this predicate "
            f"(indexes on this table: {sorted(tbl.indexes) or 'none'}).")
