#!/usr/bin/env python3
"""
minidb — a persistent database, standard library only, built along
the architecture of mkdb / "Writing My Own Database From Scratch"
(Antonio Sarosi, https://www.youtube.com/watch?v=5Pc18ge9ohI,
source: https://github.com/antoniosarosi/mkdb).

Layers, top to bottom (mkdb's pipeline, in Python):

    SQL text
      -> tokenizer            (_tokenize)
      -> parser               (_Parser -> AST: Select/Insert/...)
      -> analyzer             (table/column/type checks, literal coercion)
      -> planner              (SeqScan vs IndexScan, iterator plan tree)
      -> executor             (plan nodes pull one tuple at a time)
      -> storage              (DiskBTree over Pager: 4 KiB slotted
                               pages, overflow pages, clock cache,
                               journal transactions)

Storage model
-------------
A database is a directory holding ONE file, ``data.db``: a sequence
of 4096-byte pages (see pager.py). Page 0 is the header; page 1 is
the root of the schema B-tree (the ``minidb_meta`` catalog, like
mkdb's ``mkdb_meta`` table). Each table is a B-tree keyed by its
primary key — index-organized storage, like SQLite: a table row IS
a cell in the table B-tree, so a primary-key lookup is one O(log n)
search. Each secondary index is a B-tree keyed by (indexed value,
primary key); an index lookup is two O(log n) searches. Tables
without a PRIMARY KEY get a hidden auto-increment row id as the key.

Transactions
------------
BEGIN / COMMIT / ROLLBACK, plus one implicit transaction per
mutating statement (autocommit). Before a page is first modified
inside a transaction, its original bytes go to a journal file
(``data.db-journal``); COMMIT flushes dirty pages and deletes the
journal; ROLLBACK restores the originals. A journal found at open
time means the last transaction never committed, so it is rolled
back — that is the crash recovery. Single writer only, like mkdb.

What this is NOT: no JOINs / GROUP BY / subqueries, no MVCC, no
WAL (the journal plays that role, as in the video), no B*-tree
redistribution (plain CLRS splits), deletes are tombstones rather
than full B-tree removal. See README.md for the full list.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from diskbtree import DiskBTree, MINUS_INF, PLUS_INF, _cmp
from pager import Pager

VALID_TYPES = ("INT", "TEXT", "FLOAT", "BOOL")
_TYPE_ALIASES = {"INTEGER": "INT", "VARCHAR": "TEXT", "REAL": "FLOAT",
                 "BOOLEAN": "BOOL"}


def coerce(value: Any, col_type: str) -> Any:
    if value is None:
        return None
    if col_type == "INT":
        if isinstance(value, bool):
            raise ValueError("BOOL is not INT")
        if isinstance(value, str) and not re.fullmatch(r"-?\d+", value):
            raise ValueError(f"cannot coerce {value!r} to INT")
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


# ---------------------------------------------------------------------------
# SQL text -> tokens
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
            if sql[i:i + 2] in ("<=", ">=", "!=", "<>"):
                tokens.append("<=" if sql[i:i + 2] == "<=" else
                             ">=" if sql[i:i + 2] == ">=" else "!=")
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
        if tok.upper() == "TRUE":
            return True
        if tok.upper() == "FALSE":
            return False
        if tok.upper() == "NULL":
            return None
        return tok  # bare identifier used as a value (e.g. in SET)
    raise ValueError(f"cannot parse value {tok!r}")


# ---------------------------------------------------------------------------
# AST — the parser builds these; the analyzer checks them; the planner
# turns a SELECT into an iterator plan (mkdb: tokenizer -> TDOP parser
# -> AST -> analyzer -> optimizer -> plan).
# ---------------------------------------------------------------------------

@dataclass
class Compare:
    col: str
    op: str
    value: Any


@dataclass
class IsNull:
    col: str
    negated: bool = False


@dataclass
class And:
    left: Any
    right: Any


@dataclass
class Or:
    left: Any
    right: Any


@dataclass
class CallableExpr:
    fn: Any  # predicate(row) -> bool, for the legacy select() adapter


@dataclass
class ColumnDef:
    name: str
    type: str
    pk: bool = False
    not_null: bool = False
    unique: bool = False


@dataclass
class Select:
    columns: list[str]
    table: str
    where: Any = None
    order_by: list = field(default_factory=list)  # [(col, desc)]
    limit: Optional[int] = None
    offset: int = 0


@dataclass
class Insert:
    table: str
    columns: Optional[list[str]]
    rows: list[list[Any]]


@dataclass
class Update:
    table: str
    assignments: dict[str, Any]
    where: Any = None


@dataclass
class Delete:
    table: str
    where: Any = None


@dataclass
class CreateTable:
    name: str
    columns: list[ColumnDef]


@dataclass
class CreateIndex:
    table: str
    column: str
    name: str = ""
    unique: bool = False


@dataclass
class DropTable:
    name: str


class _Begin:
    pass


class _Commit:
    pass


class _Rollback:
    pass


@dataclass
class ShowTables:
    pass


@dataclass
class Describe:
    table: str


@dataclass
class Explain:
    stmt: Any


_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "ORDER", "BY", "LIMIT",
    "OFFSET", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
    "CREATE", "TABLE", "INDEX", "DROP", "SHOW", "DESCRIBE", "EXPLAIN",
    "BEGIN", "COMMIT", "ROLLBACK", "PRIMARY", "KEY", "NOT", "NULL",
    "UNIQUE", "TRUE", "FALSE", "ASC", "DESC", "ON", "IS",
}


class _Parser:
    def __init__(self, tokens: list[Any]):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def at(self, word: str) -> bool:
        tok = self.peek()
        return isinstance(tok, str) and tok.upper() == word

    def eat(self, word: str) -> bool:
        if self.at(word):
            self.i += 1
            return True
        return False

    def expect(self, word: str) -> None:
        if not self.eat(word):
            raise ValueError(f"expected {word!r}, got {self.peek()!r}")

    def expect_ident(self) -> str:
        tok = self.next()
        if not isinstance(tok, str) or tok.upper() in _KEYWORDS:
            raise ValueError(f"expected identifier, got {tok!r}")
        return tok

    # -- top level --------------------------------------------------------
    def parse_statement(self):
        tok = self.peek()
        kw = tok.upper() if isinstance(tok, str) else ""
        if kw == "SHOW":
            self.next()
            self.expect("TABLES")
            return ShowTables()
        if kw == "DESCRIBE":
            self.next()
            return Describe(self.expect_ident())
        if kw == "EXPLAIN":
            self.next()
            inner = self.parse_statement()
            if not isinstance(inner, Select):
                raise ValueError("EXPLAIN only supports SELECT here")
            return Explain(inner)
        if kw == "CREATE":
            return self._parse_create()
        if kw == "DROP":
            self.next()
            self.expect("TABLE")
            return DropTable(self.expect_ident())
        if kw == "INSERT":
            return self._parse_insert()
        if kw == "SELECT":
            return self._parse_select()
        if kw == "UPDATE":
            return self._parse_update()
        if kw == "DELETE":
            return self._parse_delete()
        if kw == "BEGIN":
            self.next()
            self.eat("TRANSACTION")
            return _Begin()
        if kw == "COMMIT":
            self.next()
            return _Commit()
        if kw == "ROLLBACK":
            self.next()
            return _Rollback()
        raise ValueError(f"unsupported statement: {kw!r} "
                         "(supported: CREATE TABLE/INDEX, DROP TABLE, "
                         "INSERT, SELECT, UPDATE, DELETE, BEGIN/COMMIT/"
                         "ROLLBACK, SHOW TABLES, DESCRIBE, EXPLAIN)")

    def _parse_create(self):
        self.next()  # CREATE
        unique = self.eat("UNIQUE")
        if self.at("TABLE"):
            self.next()
            name = self.expect_ident()
            self.expect("(")
            columns = []
            while True:
                col = self.expect_ident()
                raw_type = self.next()
                if not isinstance(raw_type, str):
                    raise ValueError(f"expected a type for {col!r}")
                col_type = _TYPE_ALIASES.get(raw_type.upper(),
                                             raw_type.upper())
                if col_type not in VALID_TYPES:
                    raise ValueError(f"unsupported type {raw_type!r} "
                                     f"(use {VALID_TYPES})")
                if self.at("("):  # VARCHAR(255) etc. — width ignored
                    self.next()
                    self.next()
                    self.expect(")")
                pk = not_null = uniq = False
                while True:
                    if self.at("PRIMARY"):
                        self.next()
                        self.expect("KEY")
                        pk = True
                    elif self.at("NOT"):
                        self.next()
                        self.expect("NULL")
                        not_null = True
                    elif self.at("UNIQUE"):
                        self.next()
                        uniq = True
                    else:
                        break
                columns.append(ColumnDef(col, col_type, pk, not_null, uniq))
                if not self.eat(","):
                    break
            self.expect(")")
            return CreateTable(name, columns)
        if self.at("INDEX"):
            self.next()
            name = ""
            if not self.at("ON"):
                name = self.expect_ident()
            self.expect("ON")
            table = self.expect_ident()
            self.expect("(")
            col = self.expect_ident()
            self.expect(")")
            return CreateIndex(table, col, name, unique)
        raise ValueError("expected CREATE TABLE or CREATE INDEX")

    def _parse_insert(self):
        self.next()  # INSERT
        self.expect("INTO")
        table = self.expect_ident()
        columns = None
        if self.peek() == "(":
            self.next()
            columns = []
            while True:
                columns.append(self.expect_ident())
                if not self.eat(","):
                    break
            self.expect(")")
        self.expect("VALUES")
        rows = []
        while True:
            self.expect("(")
            vals = []
            while True:
                vals.append(_parse_value(self.next()))
                if not self.eat(","):
                    break
            self.expect(")")
            rows.append(vals)
            if self.eat(","):
                continue
            break
        return Insert(table, columns, rows)

    def _parse_select(self):
        self.next()  # SELECT
        if self.peek() == "*":
            self.next()
            columns = ["*"]
        else:
            columns = []
            while True:
                columns.append(self.expect_ident())
                if not self.eat(","):
                    break
        self.expect("FROM")
        table = self.expect_ident()
        where = None
        if self.eat("WHERE"):
            where = self._parse_or()
        order_by = []
        if self.eat("ORDER"):
            self.expect("BY")
            while True:
                col = self.expect_ident()
                desc = False
                if self.eat("DESC"):
                    desc = True
                else:
                    self.eat("ASC")
                order_by.append((col, desc))
                if not self.eat(","):
                    break
        limit = None
        offset = 0
        if self.eat("LIMIT"):
            tok = self.next()
            if not isinstance(tok, str) or not tok.isdigit():
                raise ValueError("LIMIT needs a non-negative integer")
            limit = int(tok)
            if self.eat("OFFSET"):
                tok = self.next()
                if not isinstance(tok, str) or not tok.isdigit():
                    raise ValueError("OFFSET needs a non-negative integer")
                offset = int(tok)
        return Select(columns, table, where, order_by, limit, offset)

    def _parse_update(self):
        self.next()  # UPDATE
        table = self.expect_ident()
        self.expect("SET")
        assignments = {}
        while True:
            col = self.expect_ident()
            self.expect("=")
            assignments[col] = _parse_value(self.next())
            if not self.eat(","):
                break
        where = None
        if self.eat("WHERE"):
            where = self._parse_or()
        return Update(table, assignments, where)

    def _parse_delete(self):
        self.next()  # DELETE
        self.expect("FROM")
        table = self.expect_ident()
        where = None
        if self.eat("WHERE"):
            where = self._parse_or()
        return Delete(table, where)

    # -- WHERE expressions: OR of ANDs of comparisons ----------------------
    def _parse_or(self):
        node = self._parse_and()
        while self.eat("OR"):
            node = Or(node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_cmp()
        while self.eat("AND"):
            node = And(node, self._parse_cmp())
        return node

    def _parse_cmp(self):
        col = self.expect_ident()
        if self.eat("IS"):
            negated = self.eat("NOT")
            self.expect("NULL")
            return IsNull(col, negated)
        op = self.next()
        if op not in ("=", "!=", "<", ">", "<=", ">="):
            raise ValueError(f"expected a comparison operator, got {op!r}")
        return Compare(col, op, _parse_value(self.next()))


# ---------------------------------------------------------------------------
# Analyzer: resolve names, coerce literals to column types.
# ---------------------------------------------------------------------------

def _eval_expr(expr, row: dict) -> bool:
    if isinstance(expr, Compare):
        rv = row[expr.col]
        if rv is None or expr.value is None:
            return False  # SQL three-valued logic: NULL never matches
        return {"=": rv == expr.value, "!=": rv != expr.value,
                "<": rv < expr.value, ">": rv > expr.value,
                "<=": rv <= expr.value, ">=": rv >= expr.value}[expr.op]
    if isinstance(expr, IsNull):
        return (row[expr.col] is None) != expr.negated
    if isinstance(expr, And):
        return _eval_expr(expr.left, row) and _eval_expr(expr.right, row)
    if isinstance(expr, Or):
        return _eval_expr(expr.left, row) or _eval_expr(expr.right, row)
    if isinstance(expr, CallableExpr):
        return expr.fn(row)
    raise ValueError(f"unknown expression {expr!r}")


def _analyze_where(expr, meta):
    """Check columns exist; coerce each literal to its column type."""
    col_types = {c["name"]: c["type"] for c in meta["columns"]}
    if isinstance(expr, Compare):
        if expr.col not in col_types:
            raise ValueError(f"no column {expr.col!r} in WHERE")
        return Compare(expr.col, expr.op,
                       coerce(expr.value, col_types[expr.col]))
    if isinstance(expr, IsNull):
        if expr.col not in col_types:
            raise ValueError(f"no column {expr.col!r} in WHERE")
        return expr
    if isinstance(expr, And):
        return And(_analyze_where(expr.left, meta),
                   _analyze_where(expr.right, meta))
    if isinstance(expr, Or):
        return Or(_analyze_where(expr.left, meta),
                  _analyze_where(expr.right, meta))
    if isinstance(expr, CallableExpr):
        return expr
    if expr is None:
        return None
    raise ValueError(f"unknown expression {expr!r}")


def _extract_index_condition(expr, meta):
    """Pull one usable index condition out of a WHERE expression.

    Returns (condition, remaining_expr), where condition is
    (column, op, value) for an equality/range predicate on the
    primary key or an indexed column, or (None, expr) if there is
    nothing usable. Only the first usable condition is extracted;
    the rest stays as a Filter."""
    if expr is None:
        return None, None
    if isinstance(expr, Compare):
        if expr.col == meta["pk"] or expr.col in meta["indexes"]:
            if expr.op in ("=", "!=", "<", ">", "<=", ">="):
                return (expr.col, expr.op, expr.value), None
        return None, expr
    if isinstance(expr, And):
        cond, rem_left = _extract_index_condition(expr.left, meta)
        if cond is not None:
            rest = expr.right if rem_left is None else And(rem_left, expr.right)
            return cond, rest
        cond, rem_right = _extract_index_condition(expr.right, meta)
        if cond is not None:
            rest = expr.left if rem_right is None else And(expr.left, rem_right)
            return cond, rest
        return None, expr
    return None, expr


# ---------------------------------------------------------------------------
# Query plans — iterator model (mkdb's Plan::try_next). Each node yields
# one tuple (row dict) at a time; the tree is SeqScan/IndexScan ->
# Filter -> Sort -> Project -> Limit.
# ---------------------------------------------------------------------------

class _Desc:
    """Sort-key wrapper that inverts comparison (for ORDER BY ... DESC)."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return self.v > other.v

    def __eq__(self, other):
        return isinstance(other, _Desc) and self.v == other.v

    def __hash__(self):
        return hash(self.v)


def _sort_key(row: dict, order_by):
    """Sort key honouring ASC/DESC per column; NULLs sort last for
    ASC and first for DESC (like PostgreSQL's defaults)."""
    parts = []
    for col, desc in order_by:
        v = row[col]
        if desc:
            parts.append((0, 0) if v is None else (1, _Desc(v)))
        else:
            parts.append((1, 0) if v is None else (0, v))
    return tuple(parts)


class _Plan:
    def rows(self):
        raise NotImplementedError

    def explain(self, depth: int = 0) -> str:
        raise NotImplementedError


class _SeqScan(_Plan):
    def __init__(self, db, meta):
        self.db = db
        self.meta = meta
        self.examined = 0
        self.tree = DiskBTree(db.pager, meta["table_root"])

    def rows(self):
        for _key, raw in self.tree.scan():
            self.examined += 1
            yield json.loads(raw.decode("utf-8"))

    def describe(self, executed: bool = False) -> str:
        if executed:
            return (f"FULL TABLE SCAN ({self.examined} row(s) examined)")
        return (f"FULL TABLE SCAN ({self.meta['row_count']} row(s) "
                f"in table)")

    def explain(self, depth: int = 0) -> str:
        return "  " * depth + f"SeqScan({self.meta['name']}) — " \
            + self.describe()


class _IndexScan(_Plan):
    def __init__(self, db, meta, col: str, op: str, value):
        self.db = db
        self.meta = meta
        self.col, self.op, self.value = col, op, value
        self.examined = 0
        self.kind = "INDEX SCAN" if op == "=" else "INDEX RANGE SCAN"
        if col == meta["pk"]:
            self.tree = DiskBTree(db.pager, meta["table_root"])
            self.composite = False
        else:
            self.tree = DiskBTree(db.pager, meta["indexes"][col])
            self.composite = True
        self.table_tree = DiskBTree(db.pager, meta["table_root"])

    def _bounds(self):
        v = self.value
        if self.composite:
            lo_v, hi_v = [v, MINUS_INF], [v, PLUS_INF]
        else:
            lo_v, hi_v = v, v
        if self.op == "=":
            return lo_v, hi_v, True, True
        if self.op == ">":
            return ([v, PLUS_INF] if self.composite else v), PLUS_INF, \
                False, True
        if self.op == ">=":
            return lo_v, PLUS_INF, True, True
        if self.op == "<":
            return MINUS_INF, \
                ([v, MINUS_INF] if self.composite else v), True, False
        if self.op == "<=":
            return MINUS_INF, hi_v, True, True
        raise ValueError(f"unsupported operator {self.op!r}")

    def rows(self):
        if self.op == "!=":
            candidates = self.tree.scan()
        else:
            lo, hi, loi, hii = self._bounds()
            candidates = self.tree.scan_range(lo, hi, loi, hii)
        for k, _v in candidates:
            self.examined += 1
            if self.op == "!=":
                got = k[0] if self.composite else k
                if _cmp(got, self.value) == 0:
                    continue
            pk = k[1] if self.composite else k
            raw = self.table_tree.search(pk)
            if raw is None:
                continue  # index entry for a deleted row (tombstone lag)
            yield json.loads(raw.decode("utf-8"))

    def describe(self, executed: bool = False) -> str:
        what = (f"{self.kind} on {self.meta['name']}({self.col}) "
                f"using B-tree (height={self.tree.height()})")
        if not executed:
            return what
        if self.op == "=":
            return (f"{what} — {self.examined} row id(s) via index, "
                    f"no full scan")
        return (f"{what} — {self.examined} row id(s) via index, "
                f"in key order")

    def explain(self, depth: int = 0) -> str:
        return "  " * depth + f"IndexScan({self.meta['name']}.{self.col} " \
            + f"{self.op} {self.value!r}) — " + self.describe()


class _Filter(_Plan):
    def __init__(self, child: _Plan, expr):
        self.child = child
        self.expr = expr

    def rows(self):
        for row in self.child.rows():
            if _eval_expr(self.expr, row):
                yield row

    def explain(self, depth: int = 0) -> str:
        return ("  " * depth + "Filter\n"
                + self.child.explain(depth + 1))


class _Project(_Plan):
    def __init__(self, child: _Plan, columns: list[str]):
        self.child = child
        self.columns = columns

    def rows(self):
        for row in self.child.rows():
            yield {c: row[c] for c in self.columns}

    def explain(self, depth: int = 0) -> str:
        return ("  " * depth + f"Project({', '.join(self.columns)})\n"
                + self.child.explain(depth + 1))


class _Sort(_Plan):
    """ORDER BY. Small results sort in memory; bigger ones use a real
    K-way external merge sort (mkdb's Sort plan): sorted runs of
    ``mem_rows`` spill to temp files, then K runs merge at a time
    with a heap, so a result far larger than RAM still sorts."""

    def __init__(self, child: _Plan, order_by, mem_rows: int,
                 work_dir: str, k: int = 4):
        self.child = child
        self.order_by = order_by
        self.mem_rows = mem_rows
        self.work_dir = work_dir
        self.k = k
        self.used_external = False

    def rows(self):
        rows = list(self.child.rows())
        key = lambda r: _sort_key(r, self.order_by)  # noqa: E731
        if len(rows) <= self.mem_rows:
            rows.sort(key=key)
            yield from rows
            return
        self.used_external = True
        files = []
        try:
            for i in range(0, len(rows), self.mem_rows):
                run = sorted(rows[i:i + self.mem_rows], key=key)
                fd, path = tempfile.mkstemp(prefix=".minidb-sort-",
                                            dir=self.work_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for r in run:
                        f.write(json.dumps(r) + "\n")
                files.append(path)
            streams = [self._read_run(p) for p in files]
            yield from heapq.merge(*streams, key=key)
        finally:
            for p in files:
                try:
                    os.remove(p)
                except OSError:
                    pass

    @staticmethod
    def _read_run(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                yield json.loads(line)

    def explain(self, depth: int = 0) -> str:
        cols = ", ".join(c + (" DESC" if d else " ASC")
                         for c, d in self.order_by)
        return ("  " * depth + f"Sort({cols})\n"
                + self.child.explain(depth + 1))


class _Limit(_Plan):
    def __init__(self, child: _Plan, limit: Optional[int], offset: int):
        self.child = child
        self.limit = limit
        self.offset = offset

    def rows(self):
        n = 0
        for row in self.child.rows():
            if self.offset:
                self.offset -= 1
                continue
            if self.limit is not None and n >= self.limit:
                return
            n += 1
            yield row

    def explain(self, depth: int = 0) -> str:
        label = f"Limit({self.limit}" + \
            (f" OFFSET {self.offset}" if self.offset else "") + ")"
        return ("  " * depth + label + "\n"
                + self.child.explain(depth + 1))


# ---------------------------------------------------------------------------
# Database — schema catalog + tables over disk B-trees.
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, path: str, cache_size: int = 128,
                 sort_mem_rows: int = 1000):
        self.path = os.path.abspath(path)
        os.makedirs(self.path, exist_ok=True)
        if os.path.exists(os.path.join(self.path, "meta.json")) and \
                not os.path.exists(os.path.join(self.path, "data.db")):
            raise ValueError(
                "this directory holds a minidb database in the old JSON "
                "format (meta.json / <table>.json). The storage engine "
                "is now a single page file (data.db). Re-create the "
                "database (e.g. run demo.sql into a fresh directory); "
                "there is no automatic migration for the old format.")
        self.pager = Pager(os.path.join(self.path, "data.db"), cache_size)
        self.sort_mem_rows = sort_mem_rows
        self._txn_state = "none"  # none | active | aborted
        self.tables: dict[str, dict] = {}
        if self.pager.schema_root == 0:
            self.pager.begin()
            try:
                stree = DiskBTree.create(self.pager)
                self.pager.schema_root = stree.root
                self.pager._write_header()
                self.pager.commit()
            except Exception:
                self.pager.rollback()
                raise
        self._load_tables()

    # -- catalog ------------------------------------------------------------
    def _schema_tree(self) -> DiskBTree:
        return DiskBTree(self.pager, self.pager.schema_root)

    def _load_tables(self) -> None:
        self.tables = {}
        for name, raw in self._schema_tree().scan():
            meta = json.loads(raw.decode("utf-8"))
            meta["name"] = name
            self.tables[name] = meta

    def _save_meta(self, name: str) -> None:
        stree = self._schema_tree()
        stree.insert(name, json.dumps(self.tables[name]).encode("utf-8"))
        if stree.root != self.pager.schema_root:
            self.pager.schema_root = stree.root
            self.pager._write_header()

    def _refresh(self) -> None:
        """Reload schema + roots from disk (after a rollback)."""
        self._load_tables()

    def _get_meta(self, name: str) -> dict:
        if name not in self.tables:
            raise ValueError(f"no such table {name!r}")
        return self.tables[name]

    _get = _get_meta  # legacy alias

    @staticmethod
    def _col(meta: dict, name: str) -> dict:
        for c in meta["columns"]:
            if c["name"] == name:
                return c
        raise ValueError(f"no column {name!r}")

    # -- transactions ---------------------------------------------------------
    def begin(self) -> str:
        if self._txn_state != "none":
            raise ValueError("transaction already in progress")
        self.pager.begin()
        self._txn_state = "active"
        return "Transaction started."

    def commit(self) -> str:
        if self._txn_state == "none":
            raise ValueError("no transaction in progress")
        if self._txn_state == "aborted":
            raise ValueError("transaction is aborted; use ROLLBACK")
        self.pager.commit()
        self._txn_state = "none"
        return "Transaction committed."

    def rollback(self) -> str:
        if self._txn_state == "none":
            raise ValueError("no transaction in progress")
        self.pager.rollback()
        self._txn_state = "none"
        self._refresh()
        return "Transaction rolled back."

    def close(self) -> None:
        if self._txn_state == "active":
            # An open transaction at close is rolled back on next open
            # by crash recovery (the journal is still on disk).
            self.pager.rollback()
            self._txn_state = "none"
        self.pager.close()

    # -- DDL ------------------------------------------------------------------
    def create_table(self, name: str, columns) -> str:
        """columns: list of ColumnDef, or list of (name, TYPE) tuples
        (legacy form, no constraints)."""
        if name in self.tables:
            raise ValueError(f"table {name!r} already exists")
        norm = []
        for c in columns:
            if isinstance(c, ColumnDef):
                norm.append({"name": c.name, "type": c.type,
                             "pk": c.pk, "not_null": c.not_null,
                             "unique": c.unique})
            else:
                cname, ctype = c
                norm.append({"name": cname,
                             "type": _TYPE_ALIASES.get(ctype.upper(),
                                                       ctype.upper()),
                             "pk": False, "not_null": False,
                             "unique": False})
        if not norm:
            raise ValueError("CREATE TABLE needs at least one column")
        for c in norm:
            if c["type"] not in VALID_TYPES:
                raise ValueError(f"unsupported type {c['type']!r} "
                                 f"(use {VALID_TYPES})")
        if len({c["name"] for c in norm}) != len(norm):
            raise ValueError("duplicate column name")
        pks = [c["name"] for c in norm if c["pk"]]
        if len(pks) > 1:
            raise ValueError("only one PRIMARY KEY column is supported")
        tree = DiskBTree.create(self.pager)
        meta = {"name": name, "columns": norm, "pk": pks[0] if pks else None,
                "table_root": tree.root, "indexes": {},
                "next_rowid": 0, "row_count": 0}
        for c in norm:
            if c["unique"] and not c["pk"]:
                itree = DiskBTree.create(self.pager)
                meta["indexes"][c["name"]] = itree.root
        self.tables[name] = meta
        self._save_meta(name)
        return f"Table {name} created."

    def drop_table(self, name: str) -> str:
        meta = self._get_meta(name)
        DiskBTree(self.pager, meta["table_root"]).free_tree()
        for root in meta["indexes"].values():
            DiskBTree(self.pager, root).free_tree()
        stree = self._schema_tree()
        stree.delete(name)
        if stree.root != self.pager.schema_root:
            self.pager.schema_root = stree.root
            self.pager._write_header()
        del self.tables[name]
        return f"Table {name} dropped."

    def create_index(self, table_name: str, column: str,
                     index_name: str = "", unique: bool = False) -> str:
        meta = self._get_meta(table_name)
        self._col(meta, column)
        if column in meta["indexes"]:
            raise ValueError(f"index already exists on "
                             f"{table_name}({column})")
        if column == meta["pk"]:
            raise ValueError(f"{column!r} is the PRIMARY KEY; the table "
                             "B-tree already indexes it")
        tree = DiskBTree(self.pager, meta["table_root"])
        itree = DiskBTree.create(self.pager)
        seen: set = set()
        for key, raw in tree.scan():
            row = json.loads(raw.decode("utf-8"))
            v = row[column]
            if v is None:
                continue  # NULLs are not indexed
            if unique and v in seen:
                raise ValueError(f"duplicate value {v!r} for UNIQUE "
                                 f"index on {table_name}({column})")
            seen.add(v)
            itree.insert([v, key], b"")
        meta["indexes"][column] = itree.root
        self._save_meta(table_name)
        label = index_name or f"idx_{table_name}_{column}"
        return (f"Index {label} created on {table_name}({column}). "
                f"B-tree height={itree.height()}, "
                f"entries={itree.size()}.")

    # -- DML --------------------------------------------------------------------
    def _validate_row(self, meta: dict, row: dict) -> dict:
        unknown = set(row) - {c["name"] for c in meta["columns"]}
        if unknown:
            raise ValueError(f"unknown column(s): {sorted(unknown)}")
        full = {}
        for c in meta["columns"]:
            name = c["name"]
            if name in row:
                v = row[name]
                if v is None:
                    if c["pk"] or c["not_null"]:
                        raise ValueError(
                            f"column {name!r} may not be NULL")
                    full[name] = None
                else:
                    full[name] = coerce(v, c["type"])
            else:
                if c["pk"] or c["not_null"]:
                    raise ValueError(
                        f"missing value for column {name!r} "
                        "(NOT NULL / PRIMARY KEY)")
                full[name] = None
        return full

    def _check_unique(self, meta: dict, full: dict,
                      exclude_pk=None) -> None:
        for c in meta["columns"]:
            if not c["unique"] or c["pk"]:
                continue
            v = full[c["name"]]
            if v is None:
                continue
            itree = DiskBTree(self.pager, meta["indexes"][c["name"]])
            for k, _ in itree.scan_range([v, MINUS_INF], [v, PLUS_INF]):
                if exclude_pk is None or k[1] != exclude_pk:
                    raise ValueError(
                        f"duplicate value {v!r} for UNIQUE column "
                        f"{c['name']!r}")

    def _index_insert(self, meta: dict, key, full: dict) -> None:
        for col, root in list(meta["indexes"].items()):
            v = full[col]
            if v is None:
                continue
            itree = DiskBTree(self.pager, root)
            itree.insert([v, key], b"")
            meta["indexes"][col] = itree.root

    def _index_delete(self, meta: dict, key, row: dict) -> None:
        for col, root in list(meta["indexes"].items()):
            v = row.get(col)
            if v is None:
                continue
            itree = DiskBTree(self.pager, root)
            itree.delete([v, key])
            meta["indexes"][col] = itree.root

    def insert(self, table_name: str, row: dict[str, Any]) -> str:
        meta = self._get_meta(table_name)
        full = self._validate_row(meta, row)
        if meta["pk"]:
            key = full[meta["pk"]]
        else:
            key = meta["next_rowid"]
            meta["next_rowid"] += 1
        tree = DiskBTree(self.pager, meta["table_root"])
        if tree.search(key) is not None:
            raise ValueError(f"duplicate PRIMARY KEY value {key!r}")
        self._check_unique(meta, full)
        tree.insert(key, json.dumps(full).encode("utf-8"))
        meta["table_root"] = tree.root
        self._index_insert(meta, key, full)
        meta["row_count"] += 1
        self._save_meta(table_name)
        return f"1 row inserted into {table_name} (row id {key})."

    def delete(self, table_name: str, predicate) -> str:
        meta = self._get_meta(table_name)
        tree = DiskBTree(self.pager, meta["table_root"])
        doomed = []
        for key, raw in tree.scan():
            row = json.loads(raw.decode("utf-8"))
            if predicate(row):
                doomed.append((key, row))
        for key, row in doomed:
            tree.delete(key)
            self._index_delete(meta, key, row)
        if doomed:
            meta["table_root"] = tree.root
            meta["row_count"] -= len(doomed)
            self._save_meta(table_name)
        return f"{len(doomed)} row(s) deleted from {table_name}."

    def update(self, table_name: str, assignments: dict[str, Any],
               predicate) -> str:
        meta = self._get_meta(table_name)
        for col in assignments:
            self._col(meta, col)
        tree = DiskBTree(self.pager, meta["table_root"])
        matches = []
        for key, raw in tree.scan():
            row = json.loads(raw.decode("utf-8"))
            if predicate(row):
                matches.append((key, row))
        n = 0
        for key, row in matches:
            new = dict(row)
            for col, val in assignments.items():
                c = self._col(meta, col)
                if val is None:
                    if c["pk"] or c["not_null"]:
                        raise ValueError(
                            f"column {col!r} may not be NULL")
                    new[col] = None
                else:
                    new[col] = coerce(val, c["type"])
            new_pk = new[meta["pk"]] if meta["pk"] else key
            if meta["pk"] and new_pk != key \
                    and tree.search(new_pk) is not None:
                raise ValueError(
                    f"duplicate PRIMARY KEY value {new_pk!r}")
            self._check_unique(meta, new, exclude_pk=key)
            self._index_delete(meta, key, row)
            tree.delete(key)
            tree.insert(new_pk, json.dumps(new).encode("utf-8"))
            self._index_insert(meta, new_pk, new)
            n += 1
        if n:
            meta["table_root"] = tree.root
            self._save_meta(table_name)
        return f"{n} row(s) updated in {table_name}."

    # -- query -------------------------------------------------------------------
    def _plan_select(self, stmt: Select, predicate=None):
        """Build the iterator plan for a SELECT. Returns
        (plan_root, scan_node)."""
        meta = self._get_meta(stmt.table)
        where = _analyze_where(stmt.where, meta) \
            if stmt.where is not None else None
        cond, remaining = _extract_index_condition(where, meta)
        if cond is not None:
            scan = _IndexScan(self, meta, *cond)
        else:
            scan = _SeqScan(self, meta)
        node: _Plan = scan
        if remaining is not None:
            node = _Filter(node, remaining)
        if predicate is not None:
            node = _Filter(node, CallableExpr(predicate))
        if stmt.order_by:
            for col, _ in stmt.order_by:
                self._col(meta, col)
            node = _Sort(node, stmt.order_by, self.sort_mem_rows,
                         self.path)
        if stmt.columns == ["*"]:
            columns = [c["name"] for c in meta["columns"]]
        else:
            for c in stmt.columns:
                self._col(meta, c)
            columns = stmt.columns
        node = _Project(node, columns)
        if stmt.limit is not None or stmt.offset:
            node = _Limit(node, stmt.limit, stmt.offset)
        return node, scan

    def select(self, table_name: str, columns: list[str],
               predicate=None, where_clause: Optional[tuple] = None):
        """Legacy adapter (kept for the old test surface).
        Returns (rows, plan, rows_examined)."""
        meta = self._get_meta(table_name)
        where = None
        if where_clause is not None:
            col, op, val = where_clause
            self._col(meta, col)
            where = Compare(col, op,
                            coerce(val, self._col(meta, col)["type"]))
        stmt = Select(columns, table_name, where)
        plan, scan = self._plan_select(stmt, predicate)
        rows = list(plan.rows())
        return rows, scan.describe(executed=True), scan.examined


# ---------------------------------------------------------------------------
# execute() — one statement: parse, then dispatch. Mutating statements
# run inside one implicit transaction (autocommit); an explicit
# BEGIN/COMMIT/ROLLBACK transaction spans statements instead. On error
# inside an explicit transaction the state becomes "aborted" and only
# ROLLBACK can end it (mkdb's TransactionState::Aborted).
# ---------------------------------------------------------------------------

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
    stmt = _Parser(tokens).parse_statement()

    if isinstance(stmt, _Begin):
        return "info", None, db.begin()
    if isinstance(stmt, _Commit):
        return "info", None, db.commit()
    if isinstance(stmt, _Rollback):
        return "info", None, db.rollback()

    mutating = isinstance(stmt, (Insert, Update, Delete, CreateTable,
                                 CreateIndex, DropTable))
    implicit = mutating and db._txn_state == "none"
    if implicit:
        db.pager.begin()
    try:
        result = _dispatch(db, stmt)
    except Exception:
        if implicit:
            db.pager.rollback()
            db._refresh()
        elif db._txn_state == "active":
            db._txn_state = "aborted"
        raise
    if implicit:
        db.pager.commit()
    return result


def _dispatch(db: Database, stmt):
    if isinstance(stmt, ShowTables):
        rows = []
        for name in sorted(db.tables):
            meta = db.tables[name]
            rows.append({
                "table": name,
                "columns": ", ".join(
                    f"{c['name']} {c['type']}" +
                    (" PRIMARY KEY" if c["pk"] else "") +
                    (" NOT NULL" if c["not_null"] else "") +
                    (" UNIQUE" if c["unique"] else "")
                    for c in meta["columns"]),
                "indexes": ", ".join(sorted(meta["indexes"])) or "(none)",
                "rows": meta["row_count"]})
        return "rows", rows, f"{len(rows)} table(s)."

    if isinstance(stmt, Describe):
        meta = db._get_meta(stmt.table)
        rows = [{"column": c["name"], "type": c["type"],
                 "indexed": "yes" if c["name"] in meta["indexes"] or
                 c["name"] == meta["pk"] else "no",
                 "pk": "yes" if c["pk"] else "no",
                 "not_null": "yes" if c["not_null"] or c["pk"] else "no",
                 "unique": "yes" if c["unique"] or c["pk"] else "no"}
                for c in meta["columns"]]
        return "rows", rows, \
            f"Table {stmt.table}: {len(rows)} column(s)."

    if isinstance(stmt, Explain):
        plan, scan = db._plan_select(stmt.stmt)
        text = "EXPLAIN:\n" + plan.explain()
        if isinstance(scan, _IndexScan):
            text += (f"\nUses index on {scan.meta['name']}({scan.col}); "
                     f"no full table scan.")
        else:
            indexes = sorted(db.tables[stmt.stmt.table]["indexes"])
            text += (f"\nNo usable index for this predicate "
                     f"(indexes on this table: {indexes or 'none'}).")
        return "info", None, text

    if isinstance(stmt, CreateTable):
        return "info", None, db.create_table(stmt.name, stmt.columns)

    if isinstance(stmt, CreateIndex):
        return "info", None, db.create_index(
            stmt.table, stmt.column, stmt.name, stmt.unique)

    if isinstance(stmt, DropTable):
        return "info", None, db.drop_table(stmt.name)

    if isinstance(stmt, Insert):
        meta = db._get_meta(stmt.table)
        if stmt.columns is None:
            cols = [c["name"] for c in meta["columns"]]
        else:
            for c in stmt.columns:
                db._col(meta, c)
            cols = stmt.columns
        for vals in stmt.rows:
            if len(cols) != len(vals):
                raise ValueError(f"{len(cols)} column(s) but "
                                 f"{len(vals)} value(s)")
        messages = []
        for vals in stmt.rows:
            messages.append(db.insert(stmt.table, dict(zip(cols, vals))))
        if len(messages) == 1:
            return "info", None, messages[0]
        return "info", None, \
            f"{len(messages)} row(s) inserted into {stmt.table}."

    if isinstance(stmt, Select):
        plan, scan = db._plan_select(stmt)
        rows = list(plan.rows())
        return "rows", rows, \
            f"{len(rows)} row(s). Plan: {scan.describe(executed=True)}"

    if isinstance(stmt, Delete):
        meta = db._get_meta(stmt.table)
        where = _analyze_where(stmt.where, meta) \
            if stmt.where is not None else None
        pred = (lambda row, w=where: True) if where is None else \
            (lambda row, w=where: _eval_expr(w, row))
        return "info", None, db.delete(stmt.table, pred)

    if isinstance(stmt, Update):
        meta = db._get_meta(stmt.table)
        where = _analyze_where(stmt.where, meta) \
            if stmt.where is not None else None
        pred = (lambda row, w=where: True) if where is None else \
            (lambda row, w=where: _eval_expr(w, row))
        return "info", None, db.update(stmt.table, stmt.assignments, pred)

    raise ValueError(f"unsupported statement {stmt!r}")
