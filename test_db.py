#!/usr/bin/env python3
"""Automated tests for minidb. Run:  python3 test_db.py
Uses a throwaway directory under /tmp; exits non-zero on any failure.
Covers: create table, insert, select all, WHERE filter, persistence
across restarts, index use (equality + range), B-tree correctness,
UPDATE/DELETE, and error cases.

The B-tree checks now target the on-disk B-tree (DiskBTree over the
pager file) instead of the old in-memory B-tree: same behaviours,
except duplicate keys REPLACE the old value — an on-disk B-tree has
unique keys (duplicates live in composite (value, pk) index keys).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diskbtree import DiskBTree  # noqa: E402
from minidb import Database, _tokenize, execute  # noqa: E402
from pager import Pager  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  ok — {name}" + (f" ({detail})" if detail else ""))


def run(db, sql):
    kind, payload, msg = execute(db, sql)
    return kind, payload, msg


def test_tokenizer():
    print("Tokenizer (regression: comment chars must not hang it):")
    # Previously '-- comment: with / punctuation' looped forever:
    # ':' and '/' matched no branch and made zero progress.
    toks = _tokenize("-- demo: run with python3 cli.py demo.db --file demo.sql\nSELECT * FROM users;")
    check("comment stripped, SELECT survives", toks[0] == "SELECT", str(toks[:4]))
    check("stray punctuation consumed", _tokenize("a:b/c") == ["a", ":", "b", "/", "c"])


def test_btree():
    print("B-tree (on-disk, over the pager file):")
    tmp = tempfile.mkdtemp(prefix="minidb-btree-test-")
    try:
        pager = Pager(os.path.join(tmp, "t.db"), cache_size=4)
        pager.begin()
        t = DiskBTree.create(pager)
        for k in [10, 20, 5, 6, 12, 30, 7, 17]:
            t.insert(k, str(k * 100).encode())
        pager.commit()
        check("search hit", t.search(6) == b"600")
        check("search miss", t.search(99) is None)
        check("in-order keys sorted",
              [k for k, _ in t.items()] == [5, 6, 7, 10, 12, 17, 20, 30])
        check("range search", sorted(t.range_search(">", 15)) == [b"1700", b"2000", b"3000"])
        # 200 inserts force splits; height must stay logarithmic.
        pager.begin()
        big = DiskBTree.create(pager)
        for k in range(200):
            big.insert(k, str(k).encode())
        pager.commit()
        check("200 inserts, all searchable",
              all(big.search(k) == str(k).encode() for k in range(200)))
        # A 4 KiB page holds ~100 small cells, so 200 rows sit in a
        # 2-level tree; the old bound (height <= 5) still holds.
        check("200-row tree stays shallow", big.height() <= 5, f"height={big.height()}")
        # Duplicate keys replace the old value (unique keys on disk).
        pager.begin()
        big.insert(5, b"9999")
        pager.commit()
        check("duplicate key replaces value", big.search(5) == b"9999")
        pager.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_database():
    print("Database:")
    tmp = tempfile.mkdtemp(prefix="minidb-test-")
    try:
        db = Database(os.path.join(tmp, "db"))
        _, _, msg = run(db, "CREATE TABLE users (id INT, name TEXT, age INT);")
        check("create table", "created" in msg, msg)
        for sql in [
            "INSERT INTO users VALUES (1, 'Alice', 30);",
            "INSERT INTO users VALUES (2, 'Bob', 25);",
            "INSERT INTO users VALUES (3, 'Carol', 35);",
            "INSERT INTO users (id, name, age) VALUES (4, 'Dave', 28);",
        ]:
            run(db, sql)
        kind, rows, msg = run(db, "SELECT * FROM users;")
        check("select all returns 4 rows", len(rows) == 4, msg)
        check("select all content",
              rows[0] == {"id": 1, "name": "Alice", "age": 30})
        _, rows, _ = run(db, "SELECT name, age FROM users WHERE age > 28;")
        check("WHERE > filter",
              sorted(r["name"] for r in rows) == ["Alice", "Carol"], str(rows))
        _, rows, _ = run(db, "SELECT * FROM users WHERE name = 'Bob';")
        check("WHERE = on TEXT", len(rows) == 1 and rows[0]["id"] == 2)
        _, rows, _ = run(db, "SELECT * FROM users WHERE age > 100;")
        check("WHERE with no matches", rows == [])

        # Index: create, then prove the planner uses it.
        _, _, msg = run(db, "CREATE INDEX idx_users_age ON users (age);")
        check("create index", "created" in msg, msg)
        _, rows, msg = run(db, "SELECT * FROM users WHERE age = 30;")
        check("indexed equality still correct",
              len(rows) == 1 and rows[0]["name"] == "Alice")
        check("indexed equality uses INDEX SCAN", "INDEX SCAN" in msg, msg)
        _, rows, msg = run(db, "SELECT * FROM users WHERE age >= 30;")
        check("indexed range uses INDEX RANGE SCAN",
              "INDEX RANGE SCAN" in msg and len(rows) == 2, msg)
        _, _, msg = run(db, "SELECT * FROM users WHERE name = 'Bob';")
        check("non-indexed column falls back to full scan",
              "FULL TABLE SCAN" in msg, msg)
        _, _, msg = run(db, "EXPLAIN SELECT * FROM users WHERE age = 30;")
        check("EXPLAIN reports index plan", "INDEX SCAN" in msg, msg)

        # UPDATE / DELETE
        run(db, "UPDATE users SET age = 31 WHERE id = 1;")
        _, rows, _ = run(db, "SELECT * FROM users WHERE id = 1;")
        check("update", rows[0]["age"] == 31)
        _, rows, msg = run(db, "SELECT * FROM users WHERE age = 31;")
        check("index reflects update", len(rows) == 1, msg)
        run(db, "DELETE FROM users WHERE id = 4;")
        _, rows, _ = run(db, "SELECT * FROM users;")
        check("delete", len(rows) == 3)

        # Persistence across restarts: new Database object, same directory.
        db2 = Database(os.path.join(tmp, "db"))
        _, rows, _ = run(db2, "SELECT * FROM users;")
        check("persistence: rows survive reopen",
              len(rows) == 3 and sorted(r["name"] for r in rows) == ["Alice", "Bob", "Carol"],
              str(rows))
        _, rows, msg = run(db2, "SELECT * FROM users WHERE age = 31;")
        check("persistence: index on disk, still used",
              "INDEX SCAN" in msg and len(rows) == 1, msg)
        # One page file on disk now (no more meta.json / users.json).
        files = sorted(os.listdir(os.path.join(tmp, "db")))
        check("files on disk", "data.db" in files and "meta.json" not in files,
              str(files))

        # Errors are raised, not silently swallowed.
        for bad, why in [
            ("CREATE TABLE users (id INT);", "duplicate table"),
            ("INSERT INTO users VALUES (9, 'Eve');", "wrong arity"),
            ("SELECT * FROM nope;", "unknown table"),
            ("SELECT nope FROM users;", "unknown column"),
        ]:
            try:
                run(db2, bad)
                raise AssertionError(f"expected error for: {bad}")
            except (ValueError, AssertionError) as e:
                if isinstance(e, AssertionError) and "expected error" in str(e):
                    raise
                check(f"error raised: {why}", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("minidb tests")
    test_tokenizer()
    test_btree()
    test_database()
    print(f"\nAll {PASS} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
