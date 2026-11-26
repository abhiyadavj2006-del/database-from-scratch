/* test_minidb_js.js — Node harness for demo/minidb.js (the pure-JS MiniDB port).
 *
 * Rebuilt 2026-11-26: the original harness lived in /tmp and was wiped by
 * service restarts, so this copy lives durably in tests/ next to the engine.
 * Run with:  node tests/test_minidb_js.js     (from 02-database/)
 * No network, no dependencies. Exit code 0 iff every check passes.
 *
 * Coverage (91 checks): demo.sql replay (incl. both EXPLAINs + rollback),
 * statement types & WHERE operators, constraint violations that must fail
 * and change nothing, UPDATE/DELETE semantics, transactions, no-PK tables,
 * toJSON/fromJSON round-trip (what the console page stores in localStorage),
 * and all 13 example queries from demo/console.html, run in sequence.
 * Expected values were taken from the engine's actual behaviour (verified
 * 2026-11-26) and from demo.sql / demo/console.html in this repo.
 */
"use strict";

var fs = require("fs");
var path = require("path");
var MiniDB = require("../demo/minidb.js");

var ROOT = path.join(__dirname, "..");
var DEMO_SQL = fs.readFileSync(path.join(ROOT, "demo.sql"), "utf8");
var CONSOLE_HTML = fs.readFileSync(path.join(ROOT, "demo", "console.html"), "utf8");

var passed = 0, failed = 0, failures = [];
function check(name, cond) {
  if (cond) { passed++; }
  else { failed++; failures.push(name); console.log("FAIL: " + name); }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function names(rows) { return rows.map(function (r) { return r.name; }); }
function throws(fn, re) {
  try { fn(); return false; }
  catch (e) { return re ? re.test(e.message) : true; }
}
function seed() {
  var db = new MiniDB.Database();
  db.executeScript(DEMO_SQL);
  return db;
}

/* ------------------------------------------------------------------ */
/* 1. demo.sql replay (22 checks)                                     */
/* ------------------------------------------------------------------ */
(function () {
  var db = new MiniDB.Database();
  var res = db.executeScript(DEMO_SQL);
  check("demo.sql: 18 statements", res.length === 18);
  check("demo.sql: CREATE TABLE message", res[0].result.message === "Table users created.");
  check("demo.sql: INSERT Alice message", res[1].result.message === "1 row inserted into users (row id 1).");
  check("demo.sql: INSERT Dave (column list) message", res[4].result.message === "1 row inserted into users (row id 4).");
  check("demo.sql: CREATE INDEX message", res[5].result.message === "Index idx_users_age created on users(age). B-tree height=1, entries=4.");
  check("demo.sql: SELECT * message", res[6].result.message === "4 row(s). Plan: FULL TABLE SCAN (4 row(s) examined)");
  check("demo.sql: age > 28 rows", eq(names(res[7].result.rows), ["Alice", "Carol"]));
  check("demo.sql: age > 28 plan is INDEX RANGE SCAN",
    res[7].result.message.indexOf("INDEX RANGE SCAN on users(age)") !== -1);
  check("demo.sql: age = 30 row", eq(res[8].result.rows, [{ id: 1, name: "Alice", age: 30, email: "alice@example.com" }]));
  check("demo.sql: age = 30 plan is INDEX SCAN",
    res[8].result.message.indexOf("INDEX SCAN on users(age)") !== -1);
  check("demo.sql: ORDER BY age DESC LIMIT 2", eq(names(res[9].result.rows), ["Carol", "Alice"]));
  check("demo.sql: EXPLAIN age=30 uses IndexScan",
    res[10].result.message.indexOf("IndexScan(users.age = 30)") !== -1);
  check("demo.sql: EXPLAIN name='Bob' uses SeqScan",
    res[11].result.message.indexOf("SeqScan(users)") !== -1);
  check("demo.sql: BEGIN message", res[12].result.message === "Transaction started.");
  check("demo.sql: UPDATE message", res[13].result.message === "1 row(s) updated in users.");
  check("demo.sql: ROLLBACK message", res[14].result.message === "Transaction rolled back.");
  check("demo.sql: rollback restores Alice age 30",
    res[15].result.rows[0].age === 30);
  check("demo.sql: SHOW TABLES message", res[16].result.message === "1 table(s).");
  check("demo.sql: DESCRIBE message", res[17].result.message === "Table users: 4 column(s).");
  var all = db.execute("SELECT * FROM users;").rows;
  check("demo.sql: 4 users, Dave email NULL",
    all.length === 4 && all[3].name === "Dave" && all[3].email === null);
})();

/* ------------------------------------------------------------------ */
/* 2. Statement types & WHERE operators (17 checks)                   */
/* ------------------------------------------------------------------ */
(function () {
  var db = seed();
  check("types: INTEGER/VARCHAR(255)/REAL/BOOLEAN aliases accepted",
    db.execute("CREATE TABLE a (i INTEGER, v VARCHAR(255), r REAL, b BOOLEAN);").message === "Table a created.");
  check("types: alias row stored with coerced values",
    eq(db.execute("SELECT * FROM a;").rows, []) &&
    db.execute("INSERT INTO a VALUES (1, 'x', 1.5, TRUE);").message === "1 row inserted into a (row id 0)." &&
    eq(db.execute("SELECT * FROM a;").rows, [{ i: 1, v: "x", r: 1.5, b: true }]));
  var t = new MiniDB.Database();
  t.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT);");
  check("insert: multi-row with column list message",
    t.execute("INSERT INTO t (name, id) VALUES ('b', 2), ('a', 1);").message === "2 row(s) inserted into t.");
  check("insert: rows come back in PRIMARY KEY order",
    eq(t.execute("SELECT * FROM t;").rows, [{ id: 1, name: "a" }, { id: 2, name: "b" }]));
  check("select: column projection returns 4 names",
    eq(names(db.execute("SELECT name FROM users;").rows), ["Alice", "Bob", "Carol", "Dave"]));
  check("where: != ", eq(names(db.execute("SELECT name FROM users WHERE age != 30;").rows), ["Bob", "Carol", "Dave"]));
  check("where: <=", eq(names(db.execute("SELECT name FROM users WHERE age <= 28;").rows), ["Bob", "Dave"]));
  check("where: >=", eq(names(db.execute("SELECT name FROM users WHERE age >= 35;").rows), ["Carol"]));
  check("where: AND", eq(names(db.execute("SELECT name FROM users WHERE age > 25 AND age < 35;").rows), ["Alice", "Dave"]));
  check("where: OR", eq(names(db.execute("SELECT name FROM users WHERE name = 'Bob' OR age >= 35;").rows), ["Bob", "Carol"]));
  check("where: IS NOT NULL", db.execute("SELECT name FROM users WHERE email IS NOT NULL;").rows.length === 3);
  var tabs = db.execute(".tables").rows;
  var usersRow = tabs.filter(function (r) { return r.table === "users"; })[0];
  check("dot: .tables lists users with 4 rows and age+email indexes",
    !!usersRow && usersRow.rows === 4 && usersRow.indexes === "age, email");
  check("describe: id is PK + indexed",
    eq(db.execute("DESCRIBE users;").rows[0],
      { column: "id", type: "INT", indexed: "yes", pk: "yes", not_null: "yes", unique: "yes" }));
  check("dot: .help first line",
    db.execute(".help").message.split("\n")[0] === "minidb (web) — SQL supported here:");
  check("bad: unsupported statement throws",
    throws(function () { db.execute("FROB users;"); }, /unsupported statement/));
  check("bad: unknown dot-command throws",
    throws(function () { db.execute(".nope"); }, /unknown command/));
  check("ddl: DROP TABLE then SELECT throws no such table",
    db.execute("DROP TABLE a;").message === "Table a dropped." &&
    throws(function () { db.execute("SELECT * FROM a;"); }, /no such table "a"/));
})();

/* ------------------------------------------------------------------ */
/* 3. Constraint violations: must fail AND change nothing (15 checks) */
/* ------------------------------------------------------------------ */
(function () {
  var db = seed();
  var bad = [
    ["INSERT INTO users VALUES (1, 'X', 40, 'x@e.com');", /duplicate PRIMARY KEY value 1/],
    ["INSERT INTO users VALUES (9, 'X', 40, 'alice@example.com');", /duplicate value "alice@example.com" for UNIQUE column "email"/],
    ["INSERT INTO users VALUES (9, NULL, 40, 'x@e.com');", /column "name" may not be NULL/],
    ["INSERT INTO users VALUES (NULL, 'X', 40, 'x@e.com');", /column "id" may not be NULL/],
    ["INSERT INTO users VALUES (9, 'X', 'abc', 'x@e.com');", /cannot coerce "abc" to INT/],
    ["INSERT INTO users VALUES (9, 'X', 40);", /4 column\(s\) but 3 value\(s\)/],
    ["SELECT * FROM nope;", /no such table "nope"/],
    ["SELECT nope FROM users;", /no column "nope"/],
    ["SELECT * FROM users WHERE nope = 1;", /no column "nope" in WHERE/],
    ["CREATE TABLE users (id INT);", /table "users" already exists/],
    ["CREATE INDEX idx_users_age ON users (age);", /index already exists on users\(age\)/],
    ["CREATE INDEX ON users (id);", /"id" is the PRIMARY KEY/],
    ["INSERT INTO users (id, name, nope) VALUES (9, 'X', 1);", /no column "nope"/],
    ["INSERT INTO users VALUES (9, 'X', 1.5, 'x@e.com');", /cannot coerce 1\.5 to INT/]
  ];
  bad.forEach(function (c, i) {
    check("constraint " + (i + 1) + ": " + c[0].slice(0, 44),
      throws(function () { db.execute(c[0]); }, c[1]));
  });
  check("constraint: all 14 failures left the data untouched",
    db.execute("SELECT * FROM users;").rows.length === 4);
})();

/* ------------------------------------------------------------------ */
/* 4. UPDATE / DELETE semantics (8 checks)                            */
/* ------------------------------------------------------------------ */
(function () {
  var db = seed();
  check("update: PK move message",
    db.execute("UPDATE users SET id = 10 WHERE id = 4;").message === "1 row(s) updated in users.");
  check("update: PK move finds Dave under the new id",
    eq(db.execute("SELECT * FROM users WHERE id = 10;").rows,
      [{ id: 10, name: "Dave", age: 28, email: null }]));
  check("update: duplicate UNIQUE value rejected",
    throws(function () { db.execute("UPDATE users SET email = 'alice@example.com' WHERE id = 2;"); },
      /duplicate value "alice@example.com"/));
  check("update: failed UNIQUE update left Bob's email untouched",
    eq(db.execute("SELECT email FROM users WHERE id = 2;").rows, [{ email: "bob@example.com" }]));
  var t = new MiniDB.Database();
  t.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT);");
  t.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b');");
  check("update: no match reports 0 rows",
    t.execute("UPDATE t SET name = 'z' WHERE id = 99;").message === "0 row(s) updated in t.");
  check("delete: no match reports 0 rows",
    t.execute("DELETE FROM t WHERE id = 99;").message === "0 row(s) deleted from t.");
  check("delete: no WHERE deletes every row",
    t.execute("DELETE FROM t;").message === "2 row(s) deleted from t.");
  check("delete: table is empty afterwards",
    t.execute("SELECT * FROM t;").rows.length === 0);
})();

/* ------------------------------------------------------------------ */
/* 5. Transactions (9 checks)                                         */
/* ------------------------------------------------------------------ */
(function () {
  var db = seed();
  check("txn: BEGIN twice throws",
    (function () { db.execute("BEGIN;"); return throws(function () { db.execute("BEGIN;"); }, /transaction already in progress/); })());
  check("txn: COMMIT without a transaction throws",
    (function () { db.execute("ROLLBACK;"); return throws(function () { db.execute("COMMIT;"); }, /no transaction in progress/); })());
  check("txn: ROLLBACK without a transaction throws",
    throws(function () { db.execute("ROLLBACK;"); }, /no transaction in progress/));
  db.execute("BEGIN;");
  check("txn: error inside a transaction aborts it",
    throws(function () { db.execute("INSERT INTO users VALUES (1, 'X', 1, 'x@e.com');"); },
      /duplicate PRIMARY KEY value 1/));
  check("txn: COMMIT after an abort throws",
    throws(function () { db.execute("COMMIT;"); }, /transaction is aborted; use ROLLBACK/));
  check("txn: ROLLBACK after abort leaves 4 rows",
    db.execute("ROLLBACK;").message === "Transaction rolled back." &&
    db.execute("SELECT * FROM users;").rows.length === 4);
  db.execute("BEGIN;");
  db.execute("UPDATE users SET age = 31 WHERE id = 1;");
  check("txn: COMMIT message",
    db.execute("COMMIT;").message === "Transaction committed.");
  check("txn: committed UPDATE survives",
    db.execute("SELECT * FROM users WHERE id = 1;").rows[0].age === 31);
  check("txn: failed statement outside a txn leaves data untouched",
    throws(function () { db.execute("INSERT INTO users VALUES (1, 'X', 1, 'x@e.com');"); },
      /duplicate PRIMARY KEY/) &&
    db.execute("SELECT * FROM users;").rows.length === 4);
})();

/* ------------------------------------------------------------------ */
/* 6. No-PK table (1 check)                                           */
/* ------------------------------------------------------------------ */
(function () {
  var db = new MiniDB.Database();
  db.execute("CREATE TABLE t (a INT, b TEXT);");
  db.execute("INSERT INTO t VALUES (1, 'x'), (1, 'x');");
  check("no-PK: duplicate rows are allowed",
    eq(db.execute("SELECT * FROM t;").rows, [{ a: 1, b: "x" }, { a: 1, b: "x" }]));
})();

/* ------------------------------------------------------------------ */
/* 7. toJSON/fromJSON round-trip — what the console stores in          */
/*    localStorage (5 checks)                                         */
/* ------------------------------------------------------------------ */
(function () {
  var db = seed();
  var restored = MiniDB.Database.fromJSON(JSON.parse(JSON.stringify(db.toJSON())));
  check("round-trip: rows survive",
    eq(restored.execute("SELECT * FROM users WHERE age = 30;").rows,
      [{ id: 1, name: "Alice", age: 30, email: "alice@example.com" }]));
  check("round-trip: age index survives (EXPLAIN still IndexScan)",
    restored.execute("EXPLAIN SELECT * FROM users WHERE age = 30;").message
      .indexOf("IndexScan(users.age = 30)") !== -1);
  check("round-trip: PRIMARY KEY still enforced",
    throws(function () { restored.execute("INSERT INTO users VALUES (1, 'X', 1, 'x@e.com');"); },
      /duplicate PRIMARY KEY/));
  check("round-trip: DESCRIBE still reports 4 columns",
    restored.execute("DESCRIBE users;").message === "Table users: 4 column(s).");
  check("round-trip: .tables still reports 4 rows",
    restored.execute(".tables").rows[0].rows === 4);
})();

/* ------------------------------------------------------------------ */
/* 8. The 13 example queries from demo/console.html, in sequence      */
/*    (16 checks). Examples are extracted from console.html itself.   */
/* ------------------------------------------------------------------ */
(function () {
  var m = CONSOLE_HTML.match(/var EXAMPLES = \[([\s\S]*?)\];/);
  if (!m) throw new Error("setup failure: EXAMPLES block not found in console.html");
  var examples = [];
  // Example strings contain only single quotes, so a plain "..." match works.
  var re = /"([^"]*)"/g, g;
  while ((g = re.exec(m[1]))) examples.push(g[1]);
  if (examples.length !== 13) throw new Error("setup failure: expected 13 examples, got " + examples.length);

  var db = seed();
  check("examples[0]: SELECT * FROM users → 4 rows",
    db.execute(examples[0]).rows.length === 4);
  check("examples[1]: age > 28 → Alice, Carol",
    eq(names(db.execute(examples[1]).rows), ["Alice", "Carol"]));
  check("examples[2]: age = 30 → Alice",
    eq(names(db.execute(examples[2]).rows), ["Alice"]));
  check("examples[3]: ORDER BY age DESC LIMIT 2 → Carol, Alice",
    eq(names(db.execute(examples[3]).rows), ["Carol", "Alice"]));
  check("examples[4]: email IS NULL → Dave",
    eq(names(db.execute(examples[4]).rows), ["Dave"]));
  check("examples[5]: Bob OR age >= 35 → Bob, Carol",
    eq(names(db.execute(examples[5]).rows), ["Bob", "Carol"]));
  check("examples[6]: EXPLAIN age=30 → IndexScan",
    db.execute(examples[6]).message.indexOf("IndexScan(users.age = 30)") !== -1);
  check("examples[7]: UPDATE age=31 message",
    db.execute(examples[7]).message === "1 row(s) updated in users.");
  check("examples[7]: Alice age is now 31",
    db.execute("SELECT * FROM users WHERE id = 1;").rows[0].age === 31);
  check("examples[8]: INSERT Eve message",
    db.execute(examples[8]).message === "1 row inserted into users (row id 5).");
  check("examples[8]: 5 users now",
    db.execute("SELECT * FROM users;").rows.length === 5);
  check("examples[9]: duplicate-PK insert correctly errors",
    throws(function () { db.execute(examples[9]); }, /duplicate PRIMARY KEY value 1/));
  check("examples[10]: DELETE Eve message",
    db.execute(examples[10]).message === "1 row(s) deleted from users.");
  check("examples[10]: back to 4 users",
    db.execute("SELECT * FROM users;").rows.length === 4);
  check("examples[11]: SHOW TABLES",
    db.execute(examples[11]).message === "1 table(s).");
  check("examples[12]: DESCRIBE users",
    db.execute(examples[12]).message === "Table users: 4 column(s).");
})();

console.log("----");
console.log(passed + " passed, " + failed + " failed (of " + (passed + failed) + " checks)");
if (failures.length) console.log("failures:\n  " + failures.join("\n  "));
process.exit(failed === 0 ? 0 : 1);
