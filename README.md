# database-from-scratch — minidb

A minimal but **real** persistent database, built from scratch in Python
(standard library only). Tables, `INSERT` / `SELECT`, a B-tree index,
persistence to disk, and a small CLI / REPL to test it with.

> Suggested repo name: **`database-from-scratch`**. This folder is a
> standalone repo in its own right — its root holds this README, the MIT
> `LICENSE`, and a `.gitignore`. It is **not** a subfolder of a monorepo
> (that was an earlier idea; Boss decided on 5 separate repos). Nothing has
> been uploaded to GitHub yet: no remote is configured, no credentials were
> used. See [Taking it live](#taking-it-live) for what is still needed.

## What it does

| Feature | Status |
|---|---|
| `CREATE TABLE` / `DROP TABLE`, `SHOW TABLES`, `DESCRIBE` | ✅ works |
| `INSERT INTO … VALUES …` (with or without a column list) | ✅ works |
| `SELECT` columns or `*`, `WHERE` with `= != < > <= >=` and `AND` | ✅ works |
| `UPDATE … SET … WHERE …`, `DELETE FROM … WHERE …` | ✅ works |
| `CREATE INDEX` — a real B-tree per indexed column | ✅ works |
| `EXPLAIN SELECT …` — shows INDEX SCAN vs FULL TABLE SCAN | ✅ works |
| Persistence to disk across restarts (atomic file writes) | ✅ works |
| CLI / REPL (`cli.py`), script mode, piped input | ✅ works |
| JOINs, `ORDER BY` / `LIMIT` / `GROUP BY`, `NULL`s, primary keys | ❌ not implemented |
| Transactions / WAL, concurrency, multi-user access | ❌ not implemented |

Types: `INT`, `TEXT`, `FLOAT`, `BOOL`. Strings use single quotes.
Every column value is required on insert (there are no `NULL`s yet).

## Folder structure

```
database-from-scratch/            <- this folder = one standalone repo
├── README.md                     <- this file
├── LICENSE                       <- MIT
├── .gitignore                    <- excludes database data, caches, local output
├── minidb.py                     <- the database: B-tree + storage + SQL engine
├── cli.py                        <- CLI / REPL
├── demo.sql                      <- demo script (create, insert, index, select)
├── test_db.py                    <- automated tests (31 checks)
└── demo/
    └── index.html                <- static web demo page (GitHub Pages-ready)
```

Files **not** in the repo (created when you run it, excluded by `.gitignore`):
`demo.db/` (the demo database directory: `meta.json` + `users.json`),
`__pycache__/`, `test-output.txt`.

## How to run it

Requirements: **Python 3.10+ only**. No packages to install, no environment
variables, no network, no Docker, no Java.

```bash
# 1. Get the code (after it is uploaded to GitHub; for now, this folder)
git clone <your-repo-url> database-from-scratch
cd database-from-scratch

# 2. Run the tests
python3 test_db.py

# 3. Run the demo script (creates demo.db/ with a users table)
python3 cli.py demo.db --file demo.sql

# 4. Interactive REPL (the demo data is still there — persistence check)
python3 cli.py demo.db
```

In the REPL, try:

```sql
SELECT * FROM users;
SELECT name, age FROM users WHERE age > 28;
SELECT * FROM users WHERE age = 30;
EXPLAIN SELECT * FROM users WHERE age = 30;
EXPLAIN SELECT * FROM users WHERE name = 'Bob';
SHOW TABLES;
DESCRIBE users;
```

REPL commands: `.help` (SQL help), `.tables` (same as `SHOW TABLES;`),
`.exit` / `.quit` / Ctrl-D to leave. Data is saved after every write, so
leaving is safe. You can also pipe statements in:

```bash
printf "SELECT * FROM users;\n" | python3 cli.py demo.db
```

## How storage works

A database is a **directory**. Inside it:

```
demo.db/
├── meta.json     # {"tables": {"users": {"columns": [...], "indexes": ["age"]}}}
└── users.json    # {"columns": [...], "rows": [{"id": 1, "name": "Alice", "age": 30}, ...]}
```

Every mutating statement rewrites the affected table file **atomically**:
write to `<file>.json.tmp`, `fsync`, then `os.replace()` over the old file.
A crash mid-write therefore leaves either the old or the new file, never a
half-written one. Indexes are **not** stored on disk; they are rebuilt from
the table files when the database is opened — the same trade-off real
databases make after a restart, and it is verified by the persistence test
(the reopened database still uses the index).

## B-tree / index concepts

An **index** is a separate data structure that lets the database find rows
by a column value without scanning every row. Without one, `WHERE age = 30`
must read all N rows — O(N). With one, it follows the index straight to the
matching row ids — O(log N) plus the rows returned.

This project implements the classic index structure, a **B-tree**
(see `BTree` in `minidb.py`, minimum degree `t = 3`):

- **Sorted keys, many per node.** A node holds up to `2t − 1 = 5` keys
  (and 6 children). Keys are the column values, kept sorted inside each
  node; each key maps to a *bucket* of row ids, so duplicate values
  (two users aged 30) share one key instead of needing a unique key.
- **Balanced by construction.** Every leaf is at the same depth, so no
  path is longer than any other. Search, insert and lookup cost O(log n).
- **Splits keep it balanced.** When a node fills up, it *splits*: the
  median key moves up to the parent and the node becomes two half-full
  nodes. If the root splits, the tree grows one level taller. There is no
  separate rebalancing pass — splits are the mechanism.
- **Height bound.** Height ≤ `log_t((n+1)/2) + 1`. For 200 rows at `t = 3`
  that is ≈ 5, and the tests verify a 200-row tree has height 5 with every
  key still findable. Sequential inserts leave nodes at minimum occupancy
  (2 keys / 3 children), so the height sits at the bound rather than below
  it; raising `t` (bigger nodes, like disk pages) is the obvious tuning step.
- **Range queries are free.** Because keys are stored in order, an
  in-order walk yields rows sorted by the indexed column, so
  `WHERE age >= 30` is an *index range scan*, not a scan-and-filter.
- **The planner is observable, not a claim.** Every `SELECT` prints its
  plan and how many rows it examined; `EXPLAIN` prints it without running
  the query. Equality or range on an indexed column →
  `INDEX SCAN` / `INDEX RANGE SCAN`; anything else → `FULL TABLE SCAN`.
  If a predicate uses an indexed column, the B-tree is used; if it uses
  `name` when only `age` is indexed, you can watch it fall back.

## What was tested

Run: `python3 test_db.py` — **all 31 checks passed** (2026-11-21,
Python 3.12.3). The tests use a throwaway directory under `/tmp` and cover:

- **B-tree:** search hit/miss, in-order keys sorted, range search, 200
  inserts all searchable, height ≤ 5 for 200 rows, duplicate-key buckets.
- **Database:** create table, insert 4 rows (both `VALUES` forms), select
  all (4 rows, correct content), `WHERE age > 28` → Alice + Carol,
  `WHERE name = 'Bob'`, `WHERE` with no matches → 0 rows.
- **Index use:** `CREATE INDEX` on `age`; `WHERE age = 30` returns Alice
  via `INDEX SCAN`; `WHERE age >= 30` via `INDEX RANGE SCAN`;
  `WHERE name = 'Bob'` (not indexed) falls back to `FULL TABLE SCAN`;
  `EXPLAIN` reports the index plan; index still correct after `UPDATE`.
- **UPDATE / DELETE:** `UPDATE … SET age = 31 WHERE id = 1` (index
  reflects it), `DELETE … WHERE id = 4` (3 rows left).
- **Persistence across restarts:** a *new* `Database` object on the same
  directory returns the same 3 rows, the index is rebuilt on open and still
  used (`INDEX SCAN`), and `meta.json` + `users.json` exist on disk.
- **Errors:** duplicate table, wrong number of values, unknown table,
  unknown column all raise errors instead of silently succeeding.
- **Tokenizer regression:** a `-- comment: with / punctuation` line in
  `demo.sql` used to hang the tokenizer forever (those characters matched
  no branch and made zero progress); comments are now stripped and stray
  punctuation is consumed. Two checks pin this down.

The CLI demo was also run end-to-end and its output captured:

```bash
python3 cli.py demo.db --file demo.sql
# Table users created.
# 1 row inserted into users (row id 0). … (4 rows)
# Index idx_users_age created on users(age). B-tree height=1, entries=4.
# SELECT * FROM users;            -> 4 rows, FULL TABLE SCAN
# SELECT name, age … WHERE age > 28; -> Alice, Carol, INDEX RANGE SCAN
# SELECT * FROM users WHERE age = 30; -> Alice, INDEX SCAN
# EXPLAIN … WHERE name = 'Bob';   -> FULL TABLE SCAN, no usable index
```

Then, in a **separate process** (a real restart, not the same object):

```bash
printf "SELECT * FROM users WHERE age = 30;\nSELECT * FROM users;\n" \
  | python3 cli.py demo.db
# -> Alice via INDEX SCAN, then all 4 rows. Data survived the restart.
```

Two bugs were found and fixed during testing (not hidden): the tokenizer
hang above, and duplicate keys stored in *internal* B-tree nodes not being
coalesced into the existing bucket — `search()` returns the first bucket it
finds, so a second copy in a leaf was invisible. Inserts now check the
current node for an equal key before descending.

## Limitations — what does not work

- **No JOINs, `ORDER BY`, `LIMIT`, `GROUP BY`, aggregates, or subqueries.**
  One table per `SELECT`, one simple `WHERE` (with `AND`).
- **No `NULL`s** — every column needs a value on insert. No primary-key,
  uniqueness, foreign-key, or `NOT NULL` enforcement beyond that.
- **No transactions or WAL.** Durability is atomic file replacement only;
  a crash *between* statements is fine, but there is no rollback and no
  multi-statement atomicity.
- **Single process, single writer.** No locking, no concurrent clients,
  no network server. Two processes writing the same directory will
  overwrite each other.
- **Whole-table JSON rewrite on every write.** Fine for tens/hundreds of
  rows; not a storage engine for large tables. Reads load the whole table
  into memory.
- **Indexes are rebuilt on open** (not persisted), so opening a big
  database pays a rebuild cost.
- **Tiny SQL parser.** One statement at a time, no quoted semicolons
  inside strings in `--file` mode, no quoted identifiers, `TEXT` values
  are compared exactly.

## Next steps to make it fuller

1. Persist the B-tree itself (or use a page file) instead of rebuilding it.
2. Replace whole-file JSON with a page-based storage file + a write-ahead
   log (WAL) for real transactions and crash recovery.
3. Add `ORDER BY` / `LIMIT`, `NULL`s, primary keys and uniqueness.
4. Add JOINs and a slightly less tiny parser.
5. Add locking / a socket server so more than one client can use it.
6. Benchmark: measure full-scan vs index-scan time at 10k/100k rows and
   tune `t`.

## Live / Deploy

**"Live" for a database does not mean a hosted DB server.** There is no
server to deploy, and none is needed. The live artefact is the static demo
page in `demo/` — it explains the database, shows real example commands
and outputs, and tells visitors how to run it. It is plain HTML + CSS with
**no JavaScript and no external assets**, so it works on any static host.

### Option A — GitHub Pages (recommended, free)

```bash
# After the repo is created on GitHub (see "Taking it live" below):
#   Settings → Pages → Build and deployment → Source: Deploy from a branch
#   Branch: main, Folder: / (root)  — then the demo is served from /demo/
#   or: Folder: /demo  — then the demo is served from /
# No build step, no environment variables, no secrets.
# Live URL will be: https://<username>.github.io/database-from-scratch/demo/
#                   (or https://<username>.github.io/database-from-scratch/ if Folder is /demo)
```

### Option B — any static host (Netlify Drop, Vercel, Cloudflare Pages, S3)

```bash
# Drag-and-drop the demo/ folder, or point the host at it.
# No build command, no environment variables. Publish directory: demo/
```

### Option C — preview locally (no hosting at all)

```bash
python3 -m http.server 8000 --directory demo
# then open http://localhost:8000/
# (or just open demo/index.html directly in a browser — it has no fetch calls)
```

Nothing to deploy for the database itself: it is a local program. Visitors
clone the repo and run it on their own machine (see [How to run it](#how-to-run-it)).

## Taking it live

What is needed — nothing has been done yet, because GitHub is not
connected and the final repo name is not confirmed:

1. **GitHub account / username** — not known yet. Boss will connect GitHub.
2. **Create the repo** `database-from-scratch` on GitHub (empty, no README
   initialisation — this folder already has one; or initialise it and let
   this content replace it). Suggested name: `database-from-scratch`.
3. **Upload this folder as that repo** (from inside this folder):
   ```bash
   git init
   git add README.md LICENSE .gitignore minidb.py cli.py demo.sql test_db.py demo/
   git commit -m "minidb: database from scratch (B-tree index, persistent storage, CLI)"
   git branch -M main
   git remote add origin https://github.com/<username>/database-from-scratch.git
   git push -u origin main
   ```
   Note: `demo.db/`, `__pycache__/` and `test-output.txt` are gitignored
   and must **not** be committed — the commands above list the files
   explicitly so data files are never added by accident. Do not use
   `git add .` without checking `git status` first.
4. **Enable GitHub Pages** for the `demo/` page (Option A above) — a
   couple of clicks in repo Settings, no code change.
5. **No environment variables, no secrets, no API keys, no paid services,
   no build step** at any point. The only thing that needs a value is
   `<username>` in the clone / Pages URL, which is known once GitHub is
   connected.

## License

MIT — see [LICENSE](LICENSE).
