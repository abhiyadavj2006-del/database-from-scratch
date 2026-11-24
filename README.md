# database-from-scratch — minidb

A minimal but **real** persistent database, built from scratch in Python
(standard library only). One file of 4 KiB pages, B-trees on slotted pages,
a clock page cache, journal transactions, a SQL pipeline
(tokenizer → parser → analyzer → planner → iterator plans), and a TCP
server — following the architecture of the source video.

## Live web version — try it in your browser

**https://abhiyadavj2006-del.github.io/database-from-scratch/demo/console.html**

A pure-JavaScript twin of `minidb` that runs 100% in the browser — no server,
no build step, no CDN, no account. It has a SQL console (query editor, results
table, schema viewer, `.tables` / `.schema` helpers, example queries), the same
SQL surface as the Python version (`CREATE TABLE`, `INSERT`, `SELECT` with
`WHERE` / `ORDER BY` / `LIMIT`, `UPDATE`, `DELETE`, `DROP TABLE`,
`PRIMARY KEY` / `NOT NULL` / `UNIQUE`, `NULL`s, `EXPLAIN`, transactions),
and saves your data in the browser (`localStorage`). It ships as
`demo/console.html` + `demo/minidb.js`; the classic static demo page is still
at `demo/index.html` and links to it. The Python version remains the real
on-disk engine (B-trees, journal, TCP server); the web console is for trying
the SQL instantly.

## Source

This project follows **“Writing My Own Database From Scratch”** by
**Antonio Sarosi** (Tony Saro), first episode of the #mkown series,
published 2024-05-09, ~42 minutes, ~346K views:

- Video: <https://www.youtube.com/watch?v=5Pc18ge9ohI>
- Source code (Rust, ~25,000 lines): <https://github.com/antoniosarosi/mkdb>
- The video's own references: SQLite 2.8.1 source code, CMU Intro to
  Database Systems 2023.

**Attribution note — please read.** The Instagram reel that suggested
this project credits it as “Write a database from scratch by Code With
Sep”. No YouTube channel or video by that name could be found: the
video that matches the reel's description word-for-word (the #mkown
series, the mkdb repo, 25,000 lines, the chapter list below) is
Antonio Sarosi's. “Code With Sep” appears to be the reel creator's
label for it, not the channel's name. The implementation here follows
Sarosi's video and mkdb, and credits him above.

**What was actually read.** The full video was not watched end-to-end
in this session: no verbatim transcript of it is indexed anywhere
reachable (YouTube's page fetch returns the description and chapters
only; no transcript site has this video). What the work is based on:
the video page itself (description + full chapter list), the mkdb
repo README, DeepWiki architecture docs for mkdb (overview + storage
layer), and two independent study-guide summaries of the video
(studley.ai, acedra.ai) that agree with each other and with the
chapters. Chapter list, from the video page:

```
00:00 What is Mkown? · 00:37 What Do We Know About Databases?
02:18 Research Time Lapse · 02:53 Initial Ideas · 03:04 Naive Storage
Format · 05:31 Why Binary Trees Are Not Enough For Indexes
07:46 Why B-Trees Are Better Than Binary Trees · 12:21 Software
Architecture · 13:17 Dev Time Lapse · 14:13 Project Demo
18:23 Database Internals · 19:24 File System Structure
20:31 Slotted Pages · 23:45 Rows, Tables And Indexes
26:50 Overflow Pages · 28:30 Sophisticated BTree Balancing Algorithm
29:53 Parsing and AST · 31:54 Query Planner
34:23 K-Way External Merge Sort · 36:50 Cache Replacement Algorithm
38:19 Transactions: Commit & Rollback · 39:27 Network Protocol
39:56 Final Thoughts
```

The video's own summary: a basic ACID-compliant database that can
process only one transaction at a time, “something similar to the first
versions of SQLite from the early 2000s, but with less features (and
probably much worse performance)”, built with only the standard library.
This Python version keeps the same deal: stdlib only, single writer,
learning-first.

## What it does

| Feature | Status |
|---|---|
| `CREATE TABLE` / `DROP TABLE`, `SHOW TABLES`, `DESCRIBE` | ✅ works |
| `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `NULL` values | ✅ works |
| `INSERT INTO … VALUES …` (single + multi-row, with/without column list) | ✅ works |
| `SELECT` columns or `*`, `WHERE` with `= != < > <= >=`, `AND`, `OR`, `IS [NOT] NULL` | ✅ works |
| `ORDER BY … ASC/DESC`, `LIMIT`, `OFFSET` (K-way external merge sort for big results) | ✅ works |
| `UPDATE … SET … WHERE …`, `DELETE FROM … WHERE …` | ✅ works |
| `CREATE INDEX` — a real on-disk B-tree per indexed column | ✅ works |
| `EXPLAIN SELECT …` — prints the full plan tree (`Limit → Project → Sort → Filter → IndexScan`) | ✅ works |
| `BEGIN` / `COMMIT` / `ROLLBACK` + autocommit, journal + crash recovery | ✅ works |
| Persistence to disk: one page file, indexes stored on disk (no rebuild on open) | ✅ works |
| CLI / REPL (`cli.py`), script mode, piped input | ✅ works |
| TCP server + client (`server.py`, length-prefixed protocol) | ✅ works |
| JOINs, `GROUP BY` / aggregates, subqueries, `ALTER TABLE`, `DROP INDEX` | ❌ not implemented |
| MVCC, concurrent writers, WAL (journal instead, as in the video) | ❌ not implemented |

Types: `INT` (`INTEGER`), `TEXT` (`VARCHAR`), `FLOAT` (`REAL`),
`BOOL` (`BOOLEAN`). Strings use single quotes. Every `PRIMARY KEY` /
`NOT NULL` column needs a value on insert; other columns default to
`NULL`.

## Folder structure

```
database-from-scratch/            <- this folder = one standalone repo
├── README.md                     <- this file
├── LICENSE                       <- MIT
├── .gitignore                    <- excludes database data, caches, local output
├── minidb.py                     <- SQL pipeline + tables + transactions
├── pager.py                      <- page file, slotted pages, clock cache, journal
├── diskbtree.py                  <- on-disk B-tree + overflow pages
├── server.py                     <- TCP server + client
├── cli.py                        <- CLI / REPL
├── demo.sql                      <- demo script (create, insert, index, select, txn)
├── test_db.py                    <- baseline tests (31 checks)
├── test_advanced.py              <- advanced tests (59 checks)
└── demo/
    └── index.html                <- static web demo page (GitHub Pages-ready)
```

Files **not** in the repo (created when you run it, excluded by `.gitignore`):
`demo.db/` (the demo database directory: `data.db` + transient
`data.db-journal`), `__pycache__/`, `test-output.txt`.

## How to run it

Requirements: **Python 3.10+ only**. No packages to install, no environment
variables, no network, no Docker, no Java.

```bash
# 1. Get the code
git clone https://github.com/abhiyadavj2006-del/database-from-scratch.git
cd database-from-scratch

# 2. Run the tests
python3 test_db.py            # 31 baseline checks
python3 test_advanced.py      # 59 advanced checks

# 3. Run the demo script (creates demo.db/ with a users table)
python3 cli.py demo.db --file demo.sql

# 4. Interactive REPL (the demo data is still there — persistence check)
python3 cli.py demo.db

# 5. TCP server (another terminal) + client
python3 server.py demo.db 8000
python3 server.py --client 127.0.0.1 8000
```

In the REPL, try:

```sql
SELECT * FROM users;
SELECT name, age FROM users WHERE age > 28;
SELECT * FROM users WHERE age = 30;
SELECT name FROM users ORDER BY age DESC LIMIT 2;
EXPLAIN SELECT * FROM users WHERE age = 30;
BEGIN; UPDATE users SET age = 31 WHERE id = 1; ROLLBACK;
SELECT * FROM users WHERE id = 1;   -- age is back to 30
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

A database is a **directory** holding **one file**, `data.db`: a sequence
of fixed-size pages (`PAGE_SIZE = 4096`, one disk block). Page number ×
4096 is the file offset, so any page is one seek away.

```
demo.db/
└── data.db          # 5 pages for the demo (20 KiB)
    ├── page 0      # header: magic "MINIDB01", page count,
    #               # schema-tree root, free-page list head
    ├── page 1      # root of the schema B-tree (the minidb_meta
    #               # catalog: table name -> columns, PK, roots)
    ├── pages 2-3   # users table B-tree + age index B-tree
    └── page 4      # email UNIQUE index B-tree
```

- **Slotted pages.** A B-tree node is one page: a header (type, leaf
  flag, cell count, cell-area start, right-child page), a slot
  directory that grows right from the page start (one 4-byte
  offset+length entry per cell), and cell bytes that grow left from
  the page end. Reordering cells shifts slot entries instead of
  moving variable-length row bytes; the fixed-size slots are what a
  search binary-searches over.
- **Index-organized tables.** The table itself is a B-tree keyed by
  its primary key, so each cell is a full row (like SQLite). Tables
  without a `PRIMARY KEY` get a hidden auto-increment row id as the
  key. A secondary index is a B-tree keyed by `(indexed value,
  primary key)`; looking a row up by email costs two O(log n)
  searches — index, then table — instead of a physical row address
  that balancing could move.
- **Overflow pages.** A row bigger than a quarter of a page
  (1024 bytes) keeps its first chunk in the cell; the cell's last 4
  bytes point at the first overflow page, whose header points at the
  next one. Reading reassembles the chain. (Verified with a ~9.6 KB
  value.)
- **Clock cache.** Hot pages sit in a second-chance cache (default
  128 frames) with a reference bit and a dirty bit per frame. The
  clock hand clears reference bits and evicts the first frame it
  finds with ref = 0, writing it back first if dirty.
- **Journal transactions.** Before a page is first modified inside a
  transaction, its original bytes are appended to `data.db-journal`
  (fsynced). `COMMIT` flushes the dirty pages and deletes the
  journal; `ROLLBACK` copies the originals back. A journal file found
  when the database is opened means the last transaction never
  committed, so the same restore runs as crash recovery. A failed
  statement inside an explicit transaction marks it aborted — only
  `ROLLBACK` can end it. One transaction / one writer at a time.
- **Free list.** Dropped tables/indexes free their pages (including
  overflow chains) onto a free list in the header, and later
  allocations reuse them instead of growing the file.

## SQL pipeline

```
SQL text -> tokenizer -> parser (AST) -> analyzer -> planner -> plan
```

- **Tokenizer** (`_tokenize` in `minidb.py`): keywords, identifiers,
  numbers, `'strings'`, `-- comments`, operators (`= != <> < > <= >=`).
- **Parser** (`_Parser`): recursive descent producing AST nodes
  (`Select`, `Insert`, `Update`, `Delete`, `CreateTable`,
  `CreateIndex`, `DropTable`, `Begin`/`Commit`/`Rollback`, …). WHERE
  is an OR of ANDs of comparisons, plus `IS [NOT] NULL`.
- **Analyzer** (`_analyze_where`): table/column existence, literal
  coercion to the column's type (a wrong-type literal is an error,
  not a silent 0 rows), `NOT NULL` / `PRIMARY KEY` / `UNIQUE`
  enforcement on write.
- **Planner** (`_extract_index_condition` + plan nodes): one usable
  equality/range condition on the primary key or an indexed column
  becomes an `IndexScan`; anything else is a `SeqScan`, with the
  remaining predicate as a `Filter`.
- **Executor** (iterator model, like mkdb's `Plan::try_next`): each
  plan node yields one tuple at a time —
  `SeqScan/IndexScan → Filter → Sort → Project → Limit`.
  `Sort` sorts in memory while the result fits
  (`sort_mem_rows`, default 1000); beyond that it writes sorted runs
  to temp files and K-way merges them with a heap, so results far
  larger than RAM still sort. `EXPLAIN` prints the plan tree.

## B-tree / index concepts

An **index** is a separate data structure that lets the database find
rows by a column value without scanning every row. Without one,
`WHERE age = 30` must read all N rows — O(N). With one, it follows
the index straight to the matching row ids — O(log N) plus the rows
returned.

This project implements the classic index structure, a **B-tree**
(see `DiskBTree` in `diskbtree.py`), one node per 4 KiB page:

- **Sorted keys, many per node.** Keys are the column values (or
  `(value, primary key)` pairs for secondary indexes), kept sorted
  inside every node. A page holds ~100 small cells, so a 2000-row
  tree is only 2 levels tall (verified by the tests).
- **Balanced by construction.** Every leaf is at the same depth.
  Search, insert and lookup cost O(log n).
- **Splits keep it balanced.** When a node no longer fits its page,
  it *splits* at its median key: the median moves up to the parent,
  the node becomes two half-full nodes, and if the root splits the
  tree grows one level taller. There is no separate rebalancing pass
  — splits are the mechanism.
- **Range queries are free.** Because keys are stored in order, an
  in-order walk yields rows sorted by the indexed column, so
  `WHERE age >= 30` is an *index range scan*, not a scan-and-filter.
  The walk prunes whole subtrees that cannot contain matching keys.
- **Deletes are tombstones.** A deleted cell stays in place, flagged
  deleted, and is skipped by every search and scan; re-inserting the
  same key reuses the cell. (mkdb does full B-tree removal with
  merging/borrowing; see “left out” below.)
- **The planner is observable, not a claim.** Every `SELECT` prints
  its plan and how many rows it examined; `EXPLAIN` prints the whole
  plan tree without running the query. Equality or range on an
  indexed column → `INDEX SCAN` / `INDEX RANGE SCAN`; anything else
  → `FULL TABLE SCAN`.

## What was tested

Run: `python3 test_db.py` — **all 31 checks passed** (2026-11-22,
Python 3.12.3), and `python3 test_advanced.py` — **all 59 checks
passed**. The tests use throwaway directories under `/tmp`.

Baseline (`test_db.py`, the original 31 — same checks, retargeted at
the new engine):

- **B-tree (on disk now):** search hit/miss, in-order keys sorted,
  range search, 200 inserts all searchable, height ≤ 5 for 200 rows,
  duplicate key *replaces* the value (on-disk keys are unique; the
  old in-memory bucket behaviour is gone — see “What changed”).
- **Database:** create table, insert 4 rows (both `VALUES` forms),
  select all (4 rows, correct content), `WHERE age > 28` → Alice +
  Carol, `WHERE name = 'Bob'`, `WHERE` with no matches → 0 rows.
- **Index use:** `CREATE INDEX` on `age`; `WHERE age = 30` returns
  Alice via `INDEX SCAN`; `WHERE age >= 30` via `INDEX RANGE SCAN`;
  `WHERE name = 'Bob'` (not indexed) falls back to `FULL TABLE SCAN`;
  `EXPLAIN` reports the index plan; index still correct after `UPDATE`.
- **UPDATE / DELETE:** `UPDATE … SET age = 31 WHERE id = 1` (index
  reflects it), `DELETE … WHERE id = 4` (3 rows left).
- **Persistence across restarts:** a *new* `Database` object on the
  same directory returns the same 3 rows, the index is *on disk now*
  (no rebuild on open) and still used (`INDEX SCAN`), and the single
  file `data.db` exists on disk.
- **Errors:** duplicate table, wrong number of values, unknown table,
  unknown column all raise errors instead of silently succeeding.
- **Tokenizer regression:** a `-- comment: with / punctuation` line in
  `demo.sql` used to hang the tokenizer forever; comments are now
  stripped and stray punctuation is consumed. Two checks pin this down.

Advanced (`test_advanced.py`, 59 checks):

- **Pager:** fresh file is one header page, allocation grows the
  file, the free list reuses freed pages, reads stay correct with a
  4-frame cache forcing evictions, cache hits/misses are recorded.
- **Disk B-tree:** 2000 inserts all searchable, height ≤ 4, range
  scan in order, a ~9.6 KB value round-trips through overflow pages
  and is replaced by a small value.
- **Transactions:** commit survives reopen; rollback leaves nothing;
  a journal left behind by a killed process is rolled back on the
  next open (crash recovery); an error mid-transaction aborts it
  (`COMMIT` then fails, `ROLLBACK` works, nothing leaks); `COMMIT` /
  `ROLLBACK` with no transaction are errors.
- **ORDER BY / LIMIT / OFFSET:** ASC/DESC, LIMIT, OFFSET, LIMIT
  larger than the table, external merge sort forced with a 5-row
  threshold (correct order, temp files cleaned up), NULLs sort last
  for ASC and first for DESC.
- **Constraints:** `INTEGER`/`VARCHAR` aliases, duplicate PK,
  duplicate UNIQUE, missing NOT NULL, NULL PK all rejected; `IS
  NULL` / `IS NOT NULL`; PK lookup uses INDEX SCAN; UPDATE of the PK
  moves the row; UNIQUE enforced on UPDATE; multi-row INSERT; OR in
  WHERE; EXPLAIN shows `Sort`/`Limit`/`IndexScan` nodes; a
  wrong-type literal in WHERE is an analyzer error.
- **No-PK tables + DROP:** duplicate rows allowed without a PK,
  DELETE removes all matches, DROP TABLE makes the table gone and
  frees its pages for reuse.
- **TCP server:** CREATE/INSERT/SELECT round-trip over the
  length-prefixed protocol, BEGIN + ROLLBACK over the wire, errors
  reported as `Error: …`.

The CLI demo was also run end-to-end and its output captured (see
`demo/index.html` for the full transcript):

```bash
python3 cli.py demo.db --file demo.sql
# Table users created.  (id INT PRIMARY KEY, name TEXT NOT NULL, ...)
# 1 row inserted into users (row id 1). … (4 rows)
# Index idx_users_age created on users(age). B-tree height=1, entries=4.
# SELECT * FROM users;            -> 4 rows, FULL TABLE SCAN
# SELECT name, age … WHERE age > 28; -> Alice, Carol, INDEX RANGE SCAN
# SELECT * FROM users WHERE age = 30; -> Alice, INDEX SCAN
# SELECT name … ORDER BY age DESC LIMIT 2; -> Carol, Alice
# EXPLAIN … WHERE age = 30;   -> IndexScan plan tree
# EXPLAIN … WHERE name = 'Bob'; -> Filter over SeqScan, no usable index
# BEGIN; UPDATE users SET age = 31 WHERE id = 1; ROLLBACK;
# SELECT * FROM users WHERE id = 1; -> Alice, age 30 (rollback worked)
```

Then, in a **separate process** (a real restart, not the same object):

```bash
printf "SELECT * FROM users WHERE age = 30;\nSELECT * FROM users;\n" \
  | python3 cli.py demo.db
# -> Alice via INDEX SCAN, then all 4 rows. Data survived the restart.
```

## What changed vs the starter version

The first version (2026-11-21) was a JSON-file database: one
`meta.json` + one `<table>.json` per table, whole-file rewrites,
an in-memory B-tree rebuilt on open, no transactions, no
`ORDER BY`/`LIMIT`, no constraints, no `NULL`s. This update
(2026-11-22) replaces the storage engine with the video's
architecture while keeping the same SQL surface and the same 31
baseline checks green:

- **Storage:** JSON files → one page file (`data.db`): 4 KiB pages,
  page-0 header, schema B-tree catalog, slotted pages, overflow
  pages, free list. Old-format directories are detected and refuse
  to open with a clear error (no silent data loss, no automatic
  migration — re-create from `demo.sql`).
- **Indexes:** in-memory B-tree rebuilt on open → on-disk B-trees,
  persisted in the file. Duplicate keys now *replace* instead of
  bucketing (unique keys on disk; secondary indexes use composite
  `(value, pk)` keys, so duplicates still work there).
- **Tables:** row lists → index-organized B-trees keyed by PRIMARY
  KEY (hidden auto-increment row id when there is none).
- **SQL:** ad-hoc statement matching → tokenizer → AST parser →
  analyzer → planner → iterator plans (`SeqScan/IndexScan → Filter
  → Sort → Project → Limit`), with `EXPLAIN` printing the plan tree.
- **New SQL:** `ORDER BY`/`LIMIT`/`OFFSET`, `OR`, `IS [NOT] NULL`,
  multi-row `INSERT`, `BEGIN`/`COMMIT`/`ROLLBACK`, `PRIMARY KEY` /
  `NOT NULL` / `UNIQUE`, `NULL`s, `INTEGER`/`VARCHAR`/`REAL`/
  `BOOLEAN` aliases.
- **New files:** `pager.py`, `diskbtree.py`, `server.py`,
  `test_advanced.py`. The in-memory `BTree` class is gone (its tests
  now target `DiskBTree`).

## Limitations — what does not work (and why)

- **No JOINs, `GROUP BY` / aggregates, subqueries, `ALTER TABLE`,
  `DROP INDEX`.** One table per `SELECT`. mkdb itself does not do
  JOINs either; each of these is a whole planner/executor feature,
  out of scope for this update.
- **No MVCC, no concurrent writers.** One transaction at a time,
  one writer lock — exactly the video's model (“a basic ACID
  compliant database that can process only one transaction at a
  time”). Production systems use WAL + MVCC instead of a journal;
  that is a different project, not a missing line.
- **Journal, not WAL.** mkdb's transactions copy original pages to
  a journal (this is what the video teaches at 38:19). A real WAL
  would log *changes* and allow concurrent readers; the journal is
  simpler and is what the source does.
- **Classic B-tree splits, not B*-tree balancing.** mkdb balances
  on demand (redistributes cells to siblings before splitting).
  This uses CLRS splits at the median — same O(log n), simpler to
  verify, slightly less dense pages.
- **Tombstone deletes.** mkdb implements full B-tree removal with
  merging/borrowing; here a deleted cell is flagged and skipped,
  and space is reclaimed only when a whole tree is dropped. Correct,
  but a delete-heavy workload leaves dead cells in pages.
- **Rows are JSON bytes inside cells**, not mkdb's packed binary
  tuple format, and keys compare after decoding rather than by raw
  `memcmp`. Simpler and robust in Python; slower per comparison
  than a binary layout, irrelevant at this scale.
- **No `O_DIRECT`, no page checksums, no `CHECK`/`DEFAULT`/
  foreign keys.** mkdb uses `O_DIRECT` + file locking and a
  metadata cache; this version uses normal buffered I/O + fsync and
  reloads the catalog on open/rollback.
- **The TCP protocol is length-prefixed** (4-byte length + UTF-8
  SQL), like mkdb's `proto.rs` — not the Redis-inspired text
  protocol the video narrates. Any client that frames messages
  this way works; `server.py --client` is included.

## Live / Deploy

**"Live" for a database does not mean a hosted DB server.** There is no
server to deploy, and none is needed. The live artefact is the static demo
page in `demo/` — it explains the database, shows real example commands
and outputs, and tells visitors how to run it. It is plain HTML + CSS with
**no JavaScript and no external assets**, so it works on any static host.
(The TCP server in `server.py` is for local use; it binds to
`127.0.0.1` only and is not a deployment target.)

### Option A — GitHub Pages (recommended, free)

```bash
# The repo already exists (see "Taking it live" below):
#   Settings → Pages → Build and deployment → Source: Deploy from a branch
#   Branch: main, Folder: / (root)  — then the demo is served from /demo/
#   or: Folder: /demo  — then the demo is served from /
# No build step, no environment variables, no secrets.
# Live URL: https://abhiyadavj2006-del.github.io/database-from-scratch/demo/
#           (or https://abhiyadavj2006-del.github.io/database-from-scratch/ if Folder is /demo)
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

The repo already exists: **`database-from-scratch`** under
`abhiyadavj2006-del` on GitHub, with GitHub Pages live. This update
was done locally and **not pushed** — pushing is a separate step for
Boss. To publish it (from inside this folder):

```bash
git add README.md LICENSE .gitignore minidb.py pager.py diskbtree.py \
        server.py cli.py demo.sql test_db.py test_advanced.py demo/
git commit -m "minidb: pager + on-disk B-tree + transactions + ORDER BY + TCP server (video-faithful rewrite)"
git push origin main
```

Note: `demo.db/`, `__pycache__/` and `test-output.txt` are gitignored
and must **not** be committed — the commands above list the files
explicitly so data files are never added by accident. Do not use
`git add .` without checking `git status` first. New files in this
update that must be included: `pager.py`, `diskbtree.py`,
`server.py`, `test_advanced.py`. No environment variables, no
secrets, no API keys, no paid services, no build step at any point.

## License

MIT — see [LICENSE](LICENSE).
