#!/usr/bin/env python3
"""Advanced tests for minidb: the video's later chapters.

Covers the pager + slotted pages + overflow pages, journal
transactions (commit / rollback / crash recovery / aborted state),
ORDER BY / LIMIT / OFFSET including the K-way external merge sort,
PRIMARY KEY / NOT NULL / UNIQUE / NULLs, multi-row INSERT, OR and
IS NULL, index-organized PK lookups, DROP TABLE page reuse, and the
TCP server. Run:  python3 test_advanced.py
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diskbtree import DiskBTree  # noqa: E402
from minidb import Database, execute  # noqa: E402
from pager import PAGE_SIZE, Pager  # noqa: E402
from server import MiniDBClient, MiniDBServer  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  ok — {name}" + (f" ({detail})" if detail else ""))


def run(db, sql):
    return execute(db, sql)


def expect_error(db, sql, why):
    try:
        run(db, sql)
    except ValueError:
        check(f"error raised: {why}", True)
        return
    raise AssertionError(f"expected error for: {sql}")


def test_pager():
    print("Pager (4 KiB pages, clock cache, free list):")
    tmp = tempfile.mkdtemp(prefix="minidb-pager-test-")
    try:
        path = os.path.join(tmp, "p.db")
        pager = Pager(path, cache_size=4)
        check("fresh file is one header page", pager.page_count == 1,
              f"pages={pager.page_count}")
        pager.begin()
        pages = [pager.alloc_page() for _ in range(5)]
        for i, p in enumerate(pages):
            pager.write_page(p, bytes([i + 1]) * PAGE_SIZE)
        pager.commit()
        check("alloc grows the file", pager.page_count == 6)
        # Free a page, allocate again: the free list reuses it.
        pager.begin()
        pager.free_page(pages[2])
        reused = pager.alloc_page()
        pager.commit()
        check("free list reuses pages", reused == pages[2],
              f"reused={reused}")
        # Tiny cache forces evictions, but reads stay correct.
        for i, p in enumerate(pages):
            if p != pages[2]:
                check(f"page {p} survives eviction",
                      pager.read_page(p)[:1] == bytes([i + 1]))
        stats = pager.stats()
        check("clock cache records hits/misses",
              stats["cache_hits"] > 0 and stats["cache_misses"] > 0,
              str(stats))
        pager.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_btree_depth_and_overflow():
    print("Disk B-tree (deep tree, overflow payloads):")
    tmp = tempfile.mkdtemp(prefix="minidb-deep-test-")
    try:
        pager = Pager(os.path.join(tmp, "t.db"), cache_size=8)
        pager.begin()
        tree = DiskBTree.create(pager)
        for k in range(2000):
            tree.insert(k, f"v{k}".encode())
        pager.commit()
        check("2000 inserts searchable",
              all(tree.search(k) == f"v{k}".encode() for k in range(0, 2000, 37)))
        check("deep tree stays logarithmic", tree.height() <= 4,
              f"height={tree.height()}")
        check("range scan in order",
              [k for k, _ in tree.scan_range(1995, 1999)] ==
              [1995, 1996, 1997, 1998, 1999])
        # Overflow: a value far bigger than one page, over several pages.
        big = ("hello-world-" * 800).encode()  # ~9600 bytes
        pager.begin()
        tree.insert(5000, big)
        pager.commit()
        check("overflow value round-trips", tree.search(5000) == big,
              f"len={len(big)}")
        pager.begin()
        tree.insert(5000, b"small now")
        pager.commit()
        check("overflow value replaced by small one",
              tree.search(5000) == b"small now")
        pager.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_transactions():
    print("Transactions (journal, commit, rollback, crash, abort):")
    tmp = tempfile.mkdtemp(prefix="minidb-txn-test-")
    try:
        db = Database(os.path.join(tmp, "db"))
        run(db, "CREATE TABLE t (id INT PRIMARY KEY, v TEXT);")
        run(db, "INSERT INTO t VALUES (1, 'one');")
        # Explicit transaction, committed: visible after reopen.
        run(db, "BEGIN;")
        run(db, "INSERT INTO t VALUES (2, 'two');")
        _, _, msg = run(db, "COMMIT;")
        check("commit message", "committed" in msg, msg)
        db2 = Database(os.path.join(tmp, "db"))
        _, rows, _ = run(db2, "SELECT * FROM t;")
        check("committed rows survive reopen", len(rows) == 2)
        # Explicit transaction, rolled back: invisible.
        run(db2, "BEGIN;")
        run(db2, "INSERT INTO t VALUES (3, 'three');")
        run(db2, "DELETE FROM t WHERE id = 1;")
        run(db2, "ROLLBACK;")
        _, rows, _ = run(db2, "SELECT * FROM t;")
        check("rolled-back rows are gone",
              sorted(r["id"] for r in rows) == [1, 2], str(rows))
        # Crash recovery: journal left behind by an uncommitted txn.
        run(db2, "BEGIN;")
        run(db2, "INSERT INTO t VALUES (4, 'four');")
        db2.pager.close()  # process "dies" mid-transaction
        check("journal left behind after crash",
              os.path.exists(os.path.join(tmp, "db", "data.db-journal")))
        db3 = Database(os.path.join(tmp, "db"))
        check("journal removed by recovery on open",
              not os.path.exists(
                  os.path.join(tmp, "db", "data.db-journal")))
        _, rows, _ = run(db3, "SELECT * FROM t;")
        check("crashed transaction rolled back",
              sorted(r["id"] for r in rows) == [1, 2], str(rows))
        # Aborted transaction: an error mid-txn blocks COMMIT.
        run(db3, "BEGIN;")
        run(db3, "INSERT INTO t VALUES (5, 'five');")
        expect_error(db3, "INSERT INTO t VALUES (5, 'dup');",
                     "duplicate PK inside txn")
        expect_error(db3, "COMMIT;", "commit of aborted txn")
        _, _, msg = run(db3, "ROLLBACK;")
        check("rollback after abort works", "rolled back" in msg, msg)
        _, rows, _ = run(db3, "SELECT * FROM t;")
        check("aborted txn left nothing behind",
              sorted(r["id"] for r in rows) == [1, 2], str(rows))
        expect_error(db3, "COMMIT;", "commit with no txn")
        expect_error(db3, "ROLLBACK;", "rollback with no txn")
        db3.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_order_limit_sort():
    print("ORDER BY / LIMIT / OFFSET (incl. external merge sort):")
    tmp = tempfile.mkdtemp(prefix="minidb-sort-test-")
    try:
        # Tiny in-memory sort threshold forces the external path.
        db = Database(os.path.join(tmp, "db"), sort_mem_rows=5)
        run(db, "CREATE TABLE s (id INT PRIMARY KEY, v INT, w TEXT);")
        for i in range(30):
            run(db, f"INSERT INTO s VALUES ({i}, {30 - i}, 'n{i}');")
        _, rows, _ = run(db, "SELECT id, v FROM s ORDER BY v ASC;")
        check("ORDER BY ASC", [r["v"] for r in rows] == list(range(1, 31)))
        _, rows, _ = run(db, "SELECT id FROM s ORDER BY v DESC LIMIT 3;")
        check("ORDER BY DESC + LIMIT", [r["id"] for r in rows] == [0, 1, 2])
        _, rows, _ = run(db, "SELECT id FROM s ORDER BY id LIMIT 3 OFFSET 27;")
        check("LIMIT + OFFSET", [r["id"] for r in rows] == [27, 28, 29])
        _, rows, _ = run(db, "SELECT id FROM s ORDER BY v LIMIT 100;")
        check("LIMIT larger than table", len(rows) == 30)
        # No sort temp files left behind.
        leftovers = [f for f in os.listdir(os.path.join(tmp, "db"))
                     if f.startswith(".minidb-sort-")]
        check("external sort cleans up temp files", leftovers == [],
              str(leftovers))
        # NULLs sort last ASC, first DESC.
        run(db, "INSERT INTO s (id, v) VALUES (100, NULL);")
        _, rows, _ = run(db, "SELECT id FROM s ORDER BY v ASC LIMIT 1 OFFSET 30;")
        check("NULL sorts last ASC", rows[0]["id"] == 100)
        _, rows, _ = run(db, "SELECT id FROM s ORDER BY v DESC LIMIT 1;")
        check("NULL sorts first DESC", rows[0]["id"] == 100)
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_constraints():
    print("Constraints (PRIMARY KEY, NOT NULL, UNIQUE, NULLs):")
    tmp = tempfile.mkdtemp(prefix="minidb-constraints-test-")
    try:
        db = Database(os.path.join(tmp, "db"))
        run(db, "CREATE TABLE u (id INTEGER PRIMARY KEY, "
                "name VARCHAR(100) NOT NULL, email TEXT UNIQUE, age INT);")
        _, rows, msg = run(db, "INSERT INTO u VALUES (1, 'A', 'a@x', 30);")
        check("insert with aliases + constraints", "inserted" in msg, msg)
        expect_error(db, "INSERT INTO u VALUES (1, 'B', 'b@x', 20);",
                     "duplicate PRIMARY KEY")
        expect_error(db, "INSERT INTO u VALUES (2, 'B', 'a@x', 20);",
                     "duplicate UNIQUE")
        expect_error(db, "INSERT INTO u (id, email) VALUES (3, 'c@x');",
                     "missing NOT NULL")
        expect_error(db, "INSERT INTO u VALUES (NULL, 'D', 'd@x', 20);",
                     "NULL PRIMARY KEY")
        # NULLs are allowed where the column is nullable.
        run(db, "INSERT INTO u (id, name) VALUES (4, 'D');")
        _, rows, _ = run(db, "SELECT * FROM u WHERE age IS NULL;")
        check("IS NULL finds nullable rows",
              sorted(r["id"] for r in rows) == [4], str(rows))
        _, rows, _ = run(db, "SELECT * FROM u WHERE email IS NOT NULL;")
        check("IS NOT NULL", sorted(r["id"] for r in rows) == [1], str(rows))
        # PK lookup goes through the table B-tree (index-organized).
        _, rows, msg = run(db, "SELECT * FROM u WHERE id = 1;")
        check("PK lookup uses INDEX SCAN",
              "INDEX SCAN" in msg and len(rows) == 1, msg)
        # UPDATE the PK: old key goes, new key works, UNIQUE still held.
        run(db, "UPDATE u SET id = 10 WHERE id = 1;")
        _, rows, _ = run(db, "SELECT * FROM u WHERE id = 10;")
        check("UPDATE of PRIMARY KEY", len(rows) == 1 and rows[0]["name"] == "A")
        _, rows, _ = run(db, "SELECT * FROM u WHERE id = 1;")
        check("old PK gone", rows == [])
        expect_error(db, "UPDATE u SET email = 'a@x' WHERE id = 4;",
                     "UNIQUE on UPDATE")
        # Multi-row INSERT + OR in WHERE.
        _, _, msg = run(db, "INSERT INTO u (id, name) VALUES (5, 'E'), (6, 'F');")
        check("multi-row INSERT", "2 row(s) inserted" in msg, msg)
        _, rows, _ = run(db, "SELECT id FROM u WHERE id = 5 OR id = 6;")
        check("OR in WHERE", sorted(r["id"] for r in rows) == [5, 6])
        _, _, msg = run(db, "EXPLAIN SELECT id FROM u WHERE id = 5 "
                            "ORDER BY id LIMIT 1;")
        check("EXPLAIN shows plan tree",
              "Sort" in msg and "Limit" in msg and "IndexScan" in msg, msg)
        # Wrong-type literal in WHERE is an analyzer error, not 0 rows.
        expect_error(db, "SELECT * FROM u WHERE age = 'abc';",
                     "bad literal type in WHERE")
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_pk_and_drop():
    print("Tables without PK, DROP TABLE:")
    tmp = tempfile.mkdtemp(prefix="minidb-nopk-test-")
    try:
        db = Database(os.path.join(tmp, "db"))
        run(db, "CREATE TABLE log (msg TEXT, n INT);")
        run(db, "INSERT INTO log VALUES ('a', 1);")
        run(db, "INSERT INTO log VALUES ('a', 1);")  # duplicates allowed
        _, rows, _ = run(db, "SELECT * FROM log;")
        check("no-PK table allows duplicate rows", len(rows) == 2)
        run(db, "DELETE FROM log WHERE msg = 'a';")
        _, rows, _ = run(db, "SELECT * FROM log;")
        check("DELETE removes all matches", rows == [])
        pages_before = db.pager.page_count
        run(db, "DROP TABLE log;")
        expect_error(db, "SELECT * FROM log;", "dropped table is gone")
        run(db, "CREATE TABLE log2 (x INT);")
        run(db, "INSERT INTO log2 VALUES (7);")
        _, rows, _ = run(db, "SELECT * FROM log2;")
        check("table works after DROP", rows == [{"x": 7}])
        check("DROP frees pages for reuse",
              db.pager.page_count <= pages_before + 2,
              f"pages={db.pager.page_count} before={pages_before}")
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_server():
    print("TCP server (length-prefixed protocol, txn over the wire):")
    tmp = tempfile.mkdtemp(prefix="minidb-server-test-")
    server = None
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = MiniDBServer(os.path.join(tmp, "db"), port)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        for _ in range(50):
            try:
                client = MiniDBClient("127.0.0.1", port)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start")
        out = client.send("CREATE TABLE kv (id INT PRIMARY KEY, v TEXT);")
        check("server CREATE TABLE", "created" in out, out)
        out = client.send("INSERT INTO kv VALUES (1, 'hello');")
        check("server INSERT", "inserted" in out, out)
        out = client.send("SELECT * FROM kv;")
        check("server SELECT round-trip", "hello" in out and "1 row(s)" in out, out)
        out = client.send("BEGIN;")
        check("server BEGIN", "started" in out, out)
        client.send("INSERT INTO kv VALUES (2, 'gone');")
        out = client.send("ROLLBACK;")
        check("server ROLLBACK", "rolled back" in out, out)
        out = client.send("SELECT * FROM kv;")
        check("rolled-back row invisible over TCP",
              "gone" not in out and "hello" in out, out)
        out = client.send("SELECT * FROM nope;")
        check("server reports errors", out.startswith("Error:"), out)
        client.sock.close()
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("minidb advanced tests")
    test_pager()
    test_btree_depth_and_overflow()
    test_transactions()
    test_order_limit_sort()
    test_constraints()
    test_no_pk_and_drop()
    test_server()
    print(f"\nAll {PASS} advanced checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
