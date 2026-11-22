#!/usr/bin/env python3
"""minidb CLI / REPL.

Usage:
    python3 cli.py                      # REPL, database in ./demo.db
    python3 cli.py path/to/mydb         # REPL, database in that directory
    python3 cli.py path/to/mydb --file demo.sql
                                        # run statements from a file, then exit
    echo "SELECT * FROM users;" | python3 cli.py path/to/mydb
                                        # pipe statements in (non-interactive)

REPL commands (in addition to SQL):
    .tables   same as SHOW TABLES;
    .help     show help
    .exit / .quit / Ctrl-D   leave (data is already saved after each write)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minidb import Database, execute  # noqa: E402

HELP = """minidb — SQL supported here:
  CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT, email TEXT UNIQUE);
  CREATE INDEX idx_users_age ON users (age);   -- or: CREATE INDEX ON users (age);
  INSERT INTO users VALUES (1, 'Alice', 30);
  INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25), (3, 'Carol', 35);
  SELECT * FROM users;
  SELECT name, age FROM users WHERE age > 25 ORDER BY age DESC LIMIT 2;
  SELECT * FROM users WHERE name = 'Alice' OR age IS NULL;
  UPDATE users SET age = 31 WHERE id = 1;
  DELETE FROM users WHERE id = 2;
  BEGIN;  COMMIT;  ROLLBACK;   -- transactions (journal + crash recovery)
  SHOW TABLES;  DESCRIBE users;  EXPLAIN SELECT * FROM users WHERE age = 30;
  DROP TABLE users;
Types: INT (INTEGER), TEXT (VARCHAR), FLOAT (REAL), BOOL (BOOLEAN).
Strings use single quotes. NULL is supported; PRIMARY KEY / NOT NULL /
UNIQUE are enforced. The database is one page file (data.db): 4 KiB
slotted pages, B-tree tables + indexes, overflow pages, clock cache.
REPL: .tables  .help  .exit
TCP server: python3 server.py demo.db [port]  (client: --client)
"""


def _print_rows(rows):
    if not rows:
        print("(0 rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))


def run_statement(db, sql):
    sql = sql.strip()
    if not sql:
        return True
    low = sql.rstrip(";").strip().lower()
    if low in (".exit", ".quit", "exit", "quit"):
        return False
    if low in (".help", "help"):
        print(HELP)
        return True
    if low == ".tables":
        sql = "SHOW TABLES;"
    try:
        kind, payload, msg = execute(db, sql)
    except Exception as exc:  # show the error, keep the REPL alive
        print(f"Error: {exc}")
        return True
    if kind == "rows":
        _print_rows(payload)
    if msg:
        print(msg)
    return True


def run_file(db, path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # Split on ';' at line ends — statements in demo.sql are one per line,
    # and string literals in this project do not contain ';'.
    stmts = [s.strip() for s in text.split(";") if s.strip()]
    for stmt in stmts:
        print(f"minidb> {stmt};")
        if not run_statement(db, stmt + ";"):
            break


def main(argv):
    db_path = "demo.db"
    script = None
    args = list(argv)
    if args and not args[0].startswith("-"):
        db_path = args.pop(0)
    if "--file" in args:
        i = args.index("--file")
        script = args[i + 1]
    db = Database(db_path)
    print(f"minidb — database at {db.path}")
    print("Type .help for SQL help, .exit to quit. Data is saved after every write.")
    if script:
        run_file(db, script)
        return 0
    if not sys.stdin.isatty():
        for line in sys.stdin:
            if not run_statement(db, line):
                break
        return 0
    # Interactive REPL; statements may span lines until ';' appears.
    buf = ""
    try:
        while True:
            prompt = "minidb> " if not buf else "    ...> "
            try:
                line = input(prompt)
            except EOFError:
                print()
                break
            buf += line + "\n"
            if ";" in line or line.strip().lower() in (".exit", ".quit", ".help", ".tables"):
                if not run_statement(db, buf):
                    break
                buf = ""
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
