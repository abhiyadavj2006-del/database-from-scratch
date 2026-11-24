/* minidb.js — a pure-JavaScript, in-browser port of the Python minidb engine.
 *
 * Same SQL surface as minidb.py (tokenizer -> parser -> analyzer -> planner):
 *   CREATE TABLE / DROP TABLE, CREATE [UNIQUE] INDEX, SHOW TABLES, DESCRIBE,
 *   INSERT (single + multi-row, with/without column list),
 *   SELECT cols|* WHERE (= != <> < > <= >=, AND, OR, IS [NOT] NULL)
 *          ORDER BY col [ASC|DESC] [, ...] LIMIT n [OFFSET m],
 *   UPDATE ... SET ... WHERE ..., DELETE FROM ... WHERE ...,
 *   EXPLAIN SELECT ..., BEGIN / COMMIT / ROLLBACK,
 *   REPL helpers: .tables, .schema <table>, .help
 * Types: INT (INTEGER), TEXT (VARCHAR), FLOAT (REAL), BOOL (BOOLEAN).
 * Constraints: PRIMARY KEY, NOT NULL, UNIQUE, NULL values.
 *
 * Differences from the Python version (by design — there is no disk here):
 * rows live in memory (persisted by the console page via localStorage),
 * the "B-tree" is a sorted in-memory index: SELECTs still report the same
 * plan names (INDEX SCAN / INDEX RANGE SCAN / FULL TABLE SCAN) and EXPLAIN
 * prints the same plan tree shape, but there is no page file, no journal
 * file and no TCP server. Transactions snapshot the tables in memory.
 *
 * No dependencies, no network, no build step. Works in a browser
 * (window.MiniDB) and in Node (module.exports) so it can be tested
 * with `node test-minidb.js`.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.MiniDB = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var VALID_TYPES = ["INT", "TEXT", "FLOAT", "BOOL"];
  var TYPE_ALIASES = { INTEGER: "INT", VARCHAR: "TEXT", REAL: "FLOAT", BOOLEAN: "BOOL" };
  var KEYWORDS = {
    SELECT: 1, FROM: 1, WHERE: 1, AND: 1, OR: 1, ORDER: 1, BY: 1, LIMIT: 1,
    OFFSET: 1, INSERT: 1, INTO: 1, VALUES: 1, UPDATE: 1, SET: 1, DELETE: 1,
    CREATE: 1, TABLE: 1, INDEX: 1, DROP: 1, SHOW: 1, DESCRIBE: 1, EXPLAIN: 1,
    BEGIN: 1, COMMIT: 1, ROLLBACK: 1, PRIMARY: 1, KEY: 1, NOT: 1, NULL: 1,
    UNIQUE: 1, TRUE: 1, FALSE: 1, ASC: 1, DESC: 1, ON: 1, IS: 1, TRANSACTION: 1
  };

  /* ------------------------------------------------------------------ */
  /* Types                                                               */
  /* ------------------------------------------------------------------ */

  function coerce(value, colType) {
    if (value === null || value === undefined) return null;
    if (colType === "INT") {
      if (typeof value === "boolean") throw new Error("BOOL is not INT");
      if (typeof value === "string" && !/^-?\d+$/.test(value)) {
        throw new Error("cannot coerce " + JSON.stringify(value) + " to INT");
      }
      if (typeof value === "number" && !Number.isInteger(value)) {
        throw new Error("cannot coerce " + value + " to INT");
      }
      var n = Number(value);
      if (!Number.isInteger(n)) throw new Error("cannot coerce " + JSON.stringify(value) + " to INT");
      return n;
    }
    if (colType === "FLOAT") {
      var f = Number(value);
      if (typeof value === "string" && value.trim() === "") throw new Error("cannot coerce '' to FLOAT");
      if (isNaN(f)) throw new Error("cannot coerce " + JSON.stringify(value) + " to FLOAT");
      return f;
    }
    if (colType === "TEXT") return String(value);
    if (colType === "BOOL") {
      if (typeof value === "boolean") return value;
      if (typeof value === "string") {
        if (value.toUpperCase() === "TRUE" || value === "1") return true;
        if (value.toUpperCase() === "FALSE" || value === "0") return false;
      }
      throw new Error("cannot coerce " + JSON.stringify(value) + " to BOOL");
    }
    throw new Error("unknown type " + colType);
  }

  function compareValues(a, b) {
    if (typeof a === "number" && typeof b === "number") return a < b ? -1 : a > b ? 1 : 0;
    var sa = String(a), sb = String(b);
    return sa < sb ? -1 : sa > sb ? 1 : 0;
  }

  /* ------------------------------------------------------------------ */
  /* Tokenizer (mirrors minidb._tokenize)                                */
  /* ------------------------------------------------------------------ */

  function tokenize(sql) {
    sql = sql.replace(/--[^\n]*/g, " "); // strip -- line comments
    var tokens = [], i = 0, n = sql.length;
    while (i < n) {
      var ch = sql[i];
      if (/\s/.test(ch)) { i++; continue; }
      if (ch === "'") {
        var j = i + 1, out = "";
        while (j < n) {
          if (sql[j] === "'") {
            if (sql[j + 1] === "'") { out += "'"; j += 2; continue; }
            break;
          }
          out += sql[j]; j++;
        }
        if (j >= n) throw new Error("unterminated string literal");
        tokens.push({ t: "STR", v: out });
        i = j + 1;
        continue;
      }
      if ("(),;*=".indexOf(ch) !== -1) { tokens.push(ch); i++; continue; }
      if ("<>!".indexOf(ch) !== -1) {
        var two = sql.slice(i, i + 2);
        if (two === "<=" || two === ">=" || two === "!=" || two === "<>") {
          tokens.push(two === "<>" ? "!=" : two); i += 2;
        } else { tokens.push(ch); i++; }
        continue;
      }
      if (/[0-9]/.test(ch) || (ch === "-" && /[0-9]/.test(sql[i + 1] || ""))) {
        var k = i;
        if (sql[k] === "-") k++;
        while (k < n && /[0-9]/.test(sql[k])) k++;
        if (sql[k] === "." && /[0-9]/.test(sql[k + 1] || "")) {
          k++;
          while (k < n && /[0-9]/.test(sql[k])) k++;
        }
        tokens.push(sql.slice(i, k));
        i = k;
        continue;
      }
      if (/[A-Za-z_]/.test(ch)) {
        var m = i;
        while (m < n && /[A-Za-z0-9_]/.test(sql[m])) m++;
        tokens.push(sql.slice(i, m));
        i = m;
        continue;
      }
      // stray punctuation (e.g. ':') — consume it, never stall
      tokens.push(ch);
      i++;
    }
    return tokens;
  }

  function parseValue(tok) {
    if (tok && typeof tok === "object" && tok.t === "STR") return tok.v;
    if (typeof tok !== "string") throw new Error("cannot parse value " + JSON.stringify(tok));
    if (/^-?\d+$/.test(tok)) return parseInt(tok, 10);
    if (/^-?\d+\.\d+$/.test(tok)) return parseFloat(tok);
    if (tok.toUpperCase() === "TRUE") return true;
    if (tok.toUpperCase() === "FALSE") return false;
    if (tok.toUpperCase() === "NULL") return null;
    return tok; // bare identifier used as a value (e.g. in SET)
  }

  function tokText(tok) {
    if (tok && typeof tok === "object") return "'...'";
    return tok === null || tok === undefined ? "end of statement" : String(tok);
  }

  /* ------------------------------------------------------------------ */
  /* Parser (mirrors minidb._Parser)                                     */
  /* ------------------------------------------------------------------ */

  function Parser(tokens) {
    this.toks = tokens;
    this.i = 0;
  }
  Parser.prototype.peek = function () {
    return this.i < this.toks.length ? this.toks[this.i] : null;
  };
  Parser.prototype.next = function () { var t = this.peek(); this.i++; return t; };
  Parser.prototype.at = function (word) {
    var t = this.peek();
    return typeof t === "string" && t.toUpperCase() === word;
  };
  Parser.prototype.eat = function (word) {
    if (this.at(word)) { this.i++; return true; }
    return false;
  };
  Parser.prototype.expect = function (word) {
    if (!this.eat(word)) throw new Error("expected " + JSON.stringify(word) + ", got " + tokText(this.peek()));
  };
  Parser.prototype.expectIdent = function () {
    var tok = this.next();
    if (typeof tok !== "string" || KEYWORDS[tok.toUpperCase()]) {
      throw new Error("expected identifier, got " + tokText(tok));
    }
    return tok;
  };

  Parser.prototype.parseStatement = function () {
    var tok = this.peek();
    var kw = typeof tok === "string" ? tok.toUpperCase() : "";
    if (kw === "SHOW") { this.next(); this.expect("TABLES"); return { kind: "showTables" }; }
    if (kw === "DESCRIBE") { this.next(); return { kind: "describe", table: this.expectIdent() }; }
    if (kw === "EXPLAIN") {
      this.next();
      var inner = this.parseStatement();
      if (inner.kind !== "select") throw new Error("EXPLAIN only supports SELECT here");
      return { kind: "explain", stmt: inner };
    }
    if (kw === "CREATE") return this.parseCreate();
    if (kw === "DROP") { this.next(); this.expect("TABLE"); return { kind: "dropTable", name: this.expectIdent() }; }
    if (kw === "INSERT") return this.parseInsert();
    if (kw === "SELECT") return this.parseSelect();
    if (kw === "UPDATE") return this.parseUpdate();
    if (kw === "DELETE") return this.parseDelete();
    if (kw === "BEGIN") { this.next(); this.eat("TRANSACTION"); return { kind: "begin" }; }
    if (kw === "COMMIT") { this.next(); return { kind: "commit" }; }
    if (kw === "ROLLBACK") { this.next(); return { kind: "rollback" }; }
    throw new Error("unsupported statement: " + JSON.stringify(kw) +
      " (supported: CREATE TABLE/INDEX, DROP TABLE, INSERT, SELECT, UPDATE, DELETE, BEGIN/COMMIT/ROLLBACK, SHOW TABLES, DESCRIBE, EXPLAIN)");
  };

  Parser.prototype.parseCreate = function () {
    this.next(); // CREATE
    var unique = this.eat("UNIQUE");
    if (this.at("TABLE")) {
      this.next();
      var name = this.expectIdent();
      this.expect("(");
      var columns = [];
      while (true) {
        var col = this.expectIdent();
        var rawType = this.next();
        if (typeof rawType !== "string") throw new Error("expected a type for " + JSON.stringify(col));
        var colType = TYPE_ALIASES[rawType.toUpperCase()] || rawType.toUpperCase();
        if (VALID_TYPES.indexOf(colType) === -1) {
          throw new Error("unsupported type " + JSON.stringify(rawType) + " (use INT, TEXT, FLOAT, BOOL)");
        }
        if (this.at("(")) { this.next(); this.next(); this.expect(")"); } // VARCHAR(255) — width ignored
        var pk = false, notNull = false, uniq = false;
        while (true) {
          if (this.at("PRIMARY")) { this.next(); this.expect("KEY"); pk = true; }
          else if (this.at("NOT")) { this.next(); this.expect("NULL"); notNull = true; }
          else if (this.at("UNIQUE")) { this.next(); uniq = true; }
          else break;
        }
        columns.push({ name: col, type: colType, pk: pk, notNull: notNull, unique: uniq });
        if (!this.eat(",")) break;
      }
      this.expect(")");
      return { kind: "createTable", name: name, columns: columns };
    }
    if (this.at("INDEX")) {
      this.next();
      var idxName = "";
      if (!this.at("ON")) idxName = this.expectIdent();
      this.expect("ON");
      var table = this.expectIdent();
      this.expect("(");
      var icol = this.expectIdent();
      this.expect(")");
      return { kind: "createIndex", table: table, column: icol, name: idxName, unique: unique };
    }
    throw new Error("expected CREATE TABLE or CREATE INDEX");
  };

  Parser.prototype.parseInsert = function () {
    this.next(); // INSERT
    this.expect("INTO");
    var table = this.expectIdent();
    var columns = null;
    if (this.peek() === "(") {
      this.next();
      columns = [];
      while (true) {
        columns.push(this.expectIdent());
        if (!this.eat(",")) break;
      }
      this.expect(")");
    }
    this.expect("VALUES");
    var rows = [];
    while (true) {
      this.expect("(");
      var vals = [];
      while (true) {
        vals.push(parseValue(this.next()));
        if (!this.eat(",")) break;
      }
      this.expect(")");
      rows.push(vals);
      if (this.eat(",")) continue;
      break;
    }
    return { kind: "insert", table: table, columns: columns, rows: rows };
  };

  Parser.prototype.parseSelect = function () {
    this.next(); // SELECT
    var columns;
    if (this.peek() === "*") { this.next(); columns = ["*"]; }
    else {
      columns = [];
      while (true) {
        columns.push(this.expectIdent());
        if (!this.eat(",")) break;
      }
    }
    this.expect("FROM");
    var table = this.expectIdent();
    var where = null;
    if (this.eat("WHERE")) where = this.parseOr();
    var orderBy = [];
    if (this.eat("ORDER")) {
      this.expect("BY");
      while (true) {
        var col = this.expectIdent();
        var desc = false;
        if (this.eat("DESC")) desc = true;
        else this.eat("ASC");
        orderBy.push([col, desc]);
        if (!this.eat(",")) break;
      }
    }
    var limit = null, offset = 0;
    if (this.eat("LIMIT")) {
      var tok = this.next();
      if (typeof tok !== "string" || !/^\d+$/.test(tok)) throw new Error("LIMIT needs a non-negative integer");
      limit = parseInt(tok, 10);
      if (this.eat("OFFSET")) {
        var tok2 = this.next();
        if (typeof tok2 !== "string" || !/^\d+$/.test(tok2)) throw new Error("OFFSET needs a non-negative integer");
        offset = parseInt(tok2, 10);
      }
    }
    return { kind: "select", columns: columns, table: table, where: where, orderBy: orderBy, limit: limit, offset: offset };
  };

  Parser.prototype.parseUpdate = function () {
    this.next(); // UPDATE
    var table = this.expectIdent();
    this.expect("SET");
    var assignments = {};
    while (true) {
      var col = this.expectIdent();
      this.expect("=");
      assignments[col] = parseValue(this.next());
      if (!this.eat(",")) break;
    }
    var where = null;
    if (this.eat("WHERE")) where = this.parseOr();
    return { kind: "update", table: table, assignments: assignments, where: where };
  };

  Parser.prototype.parseDelete = function () {
    this.next(); // DELETE
    this.expect("FROM");
    var table = this.expectIdent();
    var where = null;
    if (this.eat("WHERE")) where = this.parseOr();
    return { kind: "delete", table: table, where: where };
  };

  Parser.prototype.parseOr = function () {
    var node = this.parseAnd();
    while (this.eat("OR")) node = { op: "OR", left: node, right: this.parseAnd() };
    return node;
  };
  Parser.prototype.parseAnd = function () {
    var node = this.parseCmp();
    while (this.eat("AND")) node = { op: "AND", left: node, right: this.parseCmp() };
    return node;
  };
  Parser.prototype.parseCmp = function () {
    var col = this.expectIdent();
    if (this.eat("IS")) {
      var negated = this.eat("NOT");
      this.expect("NULL");
      return { op: "IS NULL", col: col, negated: negated };
    }
    var op = this.next();
    if (["=", "!=", "<", ">", "<=", ">="].indexOf(op) === -1) {
      throw new Error("expected a comparison operator, got " + tokText(op));
    }
    return { op: op, col: col, value: parseValue(this.next()) };
  };

  /* ------------------------------------------------------------------ */
  /* WHERE evaluation + analysis (mirrors minidb._eval_expr /            */
  /* _analyze_where / _extract_index_condition)                          */
  /* ------------------------------------------------------------------ */

  function evalExpr(expr, row) {
    if (!expr) return true;
    if (expr.op === "AND") return evalExpr(expr.left, row) && evalExpr(expr.right, row);
    if (expr.op === "OR") return evalExpr(expr.left, row) || evalExpr(expr.right, row);
    if (expr.op === "IS NULL") {
      var isNull = row[expr.col] === null || row[expr.col] === undefined;
      return expr.negated ? !isNull : isNull;
    }
    var rv = row[expr.col];
    if (rv === null || rv === undefined || expr.value === null || expr.value === undefined) return false;
    switch (expr.op) {
      case "=": return rv === expr.value;
      case "!=": return rv !== expr.value;
      case "<": return rv < expr.value;
      case ">": return rv > expr.value;
      case "<=": return rv <= expr.value;
      case ">=": return rv >= expr.value;
    }
    throw new Error("unknown expression " + JSON.stringify(expr));
  }

  function analyzeWhere(expr, meta) {
    if (!expr) return null;
    var colTypes = {};
    meta.columns.forEach(function (c) { colTypes[c.name] = c.type; });
    if (expr.op === "AND" || expr.op === "OR") {
      return { op: expr.op, left: analyzeWhere(expr.left, meta), right: analyzeWhere(expr.right, meta) };
    }
    if (expr.op === "IS NULL") {
      if (!(expr.col in colTypes)) throw new Error("no column " + JSON.stringify(expr.col) + " in WHERE");
      return expr;
    }
    if (!(expr.col in colTypes)) throw new Error("no column " + JSON.stringify(expr.col) + " in WHERE");
    return { op: expr.op, col: expr.col, value: coerce(expr.value, colTypes[expr.col]) };
  }

  // Pull one usable index condition out of a WHERE tree, like minidb.
  // Returns [condition|null, remainingExpr|null].
  function extractIndexCondition(expr, meta) {
    if (!expr) return [null, null];
    var indexed = {};
    Object.keys(meta.indexes).forEach(function (c) { indexed[c] = true; });
    if (expr.op === "=" || expr.op === "!=" || expr.op === "<" || expr.op === ">" || expr.op === "<=" || expr.op === ">=") {
      if (expr.col === meta.pk || indexed[expr.col]) return [[expr.col, expr.op, expr.value], null];
      return [null, expr];
    }
    if (expr.op === "AND") {
      var l = extractIndexCondition(expr.left, meta);
      if (l[0]) {
        var rest = l[1] === null ? expr.right : { op: "AND", left: l[1], right: expr.right };
        return [l[0], rest];
      }
      var r = extractIndexCondition(expr.right, meta);
      if (r[0]) {
        var rest2 = r[1] === null ? expr.left : { op: "AND", left: expr.left, right: r[1] };
        return [r[0], rest2];
      }
      return [null, expr];
    }
    return [null, expr];
  }

  function sortKeyParts(row, orderBy) {
    // NULLs sort last for ASC and first for DESC (PostgreSQL defaults,
    // same as minidb._sort_key).
    return orderBy.map(function (ob) {
      var v = row[ob[0]];
      return { col: ob[0], desc: ob[1], v: v };
    });
  }

  function compareRows(a, b, orderBy) {
    var partsA = sortKeyParts(a, orderBy), partsB = sortKeyParts(b, orderBy);
    for (var i = 0; i < partsA.length; i++) {
      var pa = partsA[i], pb = partsB[i];
      if (pa.v === null && pb.v === null) continue;
      if (pa.v === null) return pa.desc ? -1 : 1;
      if (pb.v === null) return pa.desc ? 1 : -1;
      var c = compareValues(pa.v, pb.v);
      if (c !== 0) return pa.desc ? -c : c;
    }
    return 0;
  }

  /* ------------------------------------------------------------------ */
  /* Database                                                            */
  /* ------------------------------------------------------------------ */

  function Database() {
    this.tables = {};           // name -> meta
    this.txnState = "none";     // none | active | aborted
    this._snapshot = null;
  }

  Database.prototype._getMeta = function (name) {
    if (!this.tables[name]) throw new Error("no such table " + JSON.stringify(name));
    return this.tables[name];
  };

  Database.prototype._col = function (meta, name) {
    for (var i = 0; i < meta.columns.length; i++) {
      if (meta.columns[i].name === name) return meta.columns[i];
    }
    throw new Error("no column " + JSON.stringify(name));
  };

  Database.prototype.toJSON = function () {
    return { tables: this.tables };
  };

  Database.fromJSON = function (data) {
    var db = new Database();
    if (data && data.tables) db.tables = data.tables;
    return db;
  };

  Database.prototype.listTables = function () {
    var self = this;
    return Object.keys(this.tables).sort().map(function (name) { return self.tables[name]; });
  };

  /* -- transactions ---------------------------------------------------- */
  Database.prototype.begin = function () {
    if (this.txnState !== "none") throw new Error("transaction already in progress");
    this._snapshot = JSON.parse(JSON.stringify({ tables: this.tables }));
    this.txnState = "active";
    return "Transaction started.";
  };
  Database.prototype.commit = function () {
    if (this.txnState === "none") throw new Error("no transaction in progress");
    if (this.txnState === "aborted") throw new Error("transaction is aborted; use ROLLBACK");
    this._snapshot = null;
    this.txnState = "none";
    return "Transaction committed.";
  };
  Database.prototype.rollback = function () {
    if (this.txnState === "none") throw new Error("no transaction in progress");
    this.tables = this._snapshot.tables;
    this._snapshot = null;
    this.txnState = "none";
    return "Transaction rolled back.";
  };

  /* -- DDL ------------------------------------------------------------- */
  Database.prototype.createTable = function (name, columns) {
    if (this.tables[name]) throw new Error("table " + JSON.stringify(name) + " already exists");
    if (!columns.length) throw new Error("CREATE TABLE needs at least one column");
    columns.forEach(function (c) {
      if (VALID_TYPES.indexOf(c.type) === -1) throw new Error("unsupported type " + JSON.stringify(c.type));
    });
    var names = columns.map(function (c) { return c.name; });
    if (new Set(names).size !== names.length) throw new Error("duplicate column name");
    var pks = columns.filter(function (c) { return c.pk; }).map(function (c) { return c.name; });
    if (pks.length > 1) throw new Error("only one PRIMARY KEY column is supported");
    var indexes = {};
    columns.forEach(function (c) { if (c.unique && !c.pk) indexes[c.name] = true; });
    this.tables[name] = {
      name: name, columns: columns, pk: pks[0] || null,
      indexes: indexes, rows: [], nextRowid: 0
    };
    return "Table " + name + " created.";
  };

  Database.prototype.dropTable = function (name) {
    this._getMeta(name);
    delete this.tables[name];
    return "Table " + name + " dropped.";
  };

  Database.prototype.createIndex = function (tableName, column, indexName, unique) {
    var meta = this._getMeta(tableName);
    this._col(meta, column);
    if (meta.indexes[column]) throw new Error("index already exists on " + tableName + "(" + column + ")");
    if (column === meta.pk) throw new Error(JSON.stringify(column) + " is the PRIMARY KEY; the table B-tree already indexes it");
    if (unique) {
      var seen = {};
      meta.rows.forEach(function (row) {
        var v = row[column];
        if (v === null || v === undefined) return;
        if (seen[String(v) + "\u0000" + typeof v]) throw new Error("duplicate value " + JSON.stringify(v) + " for UNIQUE index on " + tableName + "(" + column + ")");
        seen[String(v) + "\u0000" + typeof v] = true;
      });
    }
    meta.indexes[column] = true;
    var label = indexName || ("idx_" + tableName + "_" + column);
    var entries = meta.rows.filter(function (r) { return r[column] !== null && r[column] !== undefined; }).length;
    return "Index " + label + " created on " + tableName + "(" + column + "). B-tree height=1, entries=" + entries + ".";
  };

  /* -- DML ------------------------------------------------------------- */
  Database.prototype._validateRow = function (meta, row) {
    var known = {};
    meta.columns.forEach(function (c) { known[c.name] = true; });
    Object.keys(row).forEach(function (k) {
      if (!known[k]) throw new Error("unknown column(s): " + JSON.stringify(k));
    });
    var full = {};
    meta.columns.forEach(function (c) {
      if (c.name in row) {
        var v = row[c.name];
        if (v === null || v === undefined) {
          if (c.pk || c.notNull) throw new Error("column " + JSON.stringify(c.name) + " may not be NULL");
          full[c.name] = null;
        } else {
          full[c.name] = coerce(v, c.type);
        }
      } else {
        if (c.pk || c.notNull) throw new Error("missing value for column " + JSON.stringify(c.name) + " (NOT NULL / PRIMARY KEY)");
        full[c.name] = null;
      }
    });
    return full;
  };

  Database.prototype._checkUnique = function (meta, full, excludeRowid) {
    var self = this;
    meta.columns.forEach(function (c) {
      if (!c.unique || c.pk) return;
      var v = full[c.name];
      if (v === null || v === undefined) return;
      meta.rows.forEach(function (r) {
        if (excludeRowid !== undefined && r.__rowid === excludeRowid) return;
        if (r[c.name] === v) throw new Error("duplicate value " + JSON.stringify(v) + " for UNIQUE column " + JSON.stringify(c.name));
      });
    });
  };

  Database.prototype.insert = function (tableName, row) {
    var meta = this._getMeta(tableName);
    var full = this._validateRow(meta, row);
    var key;
    if (meta.pk) {
      key = full[meta.pk];
      if (meta.rows.some(function (r) { return r[meta.pk] === key; })) {
        throw new Error("duplicate PRIMARY KEY value " + JSON.stringify(key));
      }
    } else {
      key = meta.nextRowid;
      meta.nextRowid += 1;
    }
    this._checkUnique(meta, full);
    full.__rowid = key;
    meta.rows.push(full);
    return "1 row inserted into " + tableName + " (row id " + key + ").";
  };

  Database.prototype.deleteRows = function (tableName, predicate) {
    var meta = this._getMeta(tableName);
    var doomed = meta.rows.filter(predicate);
    if (doomed.length) {
      var gone = {};
      doomed.forEach(function (r) { gone[r.__rowid] = true; });
      meta.rows = meta.rows.filter(function (r) { return !gone[r.__rowid]; });
    }
    return doomed.length + " row(s) deleted from " + tableName + ".";
  };

  Database.prototype.updateRows = function (tableName, assignments, predicate) {
    var meta = this._getMeta(tableName);
    var self = this;
    Object.keys(assignments).forEach(function (col) { self._col(meta, col); });
    var matches = meta.rows.filter(predicate);
    // Validate every new row first, so a failed UPDATE changes nothing.
    var planned = matches.map(function (row) {
      var next = {};
      Object.keys(row).forEach(function (k) { next[k] = row[k]; });
      Object.keys(assignments).forEach(function (col) {
        var c = self._col(meta, col);
        var val = assignments[col];
        if (val === null || val === undefined) {
          if (c.pk || c.notNull) throw new Error("column " + JSON.stringify(col) + " may not be NULL");
          next[col] = null;
        } else {
          next[col] = coerce(val, c.type);
        }
      });
      return { old: row, next: next };
    });
    planned.forEach(function (p) {
      if (meta.pk) {
        var newPk = p.next[meta.pk];
        if (newPk !== p.old[meta.pk] && meta.rows.some(function (r) { return r !== p.old && r[meta.pk] === newPk; })) {
          throw new Error("duplicate PRIMARY KEY value " + JSON.stringify(newPk));
        }
      }
      self._checkUnique(meta, p.next, p.old.__rowid);
    });
    planned.forEach(function (p) {
      Object.keys(p.next).forEach(function (k) { p.old[k] = p.next[k]; });
      if (meta.pk) p.old.__rowid = p.next[meta.pk];
    });
    return planned.length + " row(s) updated in " + tableName + ".";
  };

  /* -- query ----------------------------------------------------------- */
  // Builds the plan for a SELECT and runs it. Returns {rows, planText, scanDesc}.
  Database.prototype.planSelect = function (stmt) {
    var meta = this._getMeta(stmt.table);
    var where = stmt.where ? analyzeWhere(stmt.where, meta) : null;
    var extracted = extractIndexCondition(where, meta);
    var cond = extracted[0], remaining = extracted[1];
    var scan, scanDesc, examined = 0;
    // Rows in primary-key order, like the on-disk B-tree scan in minidb.
    var ordered = meta.rows.slice();
    if (meta.pk) {
      ordered.sort(function (a, b) { return compareValues(a[meta.pk], b[meta.pk]); });
    }
    if (cond) {
      var col = cond[0], op = cond[1], val = cond[2];
      var kind = op === "=" ? "INDEX SCAN" : "INDEX RANGE SCAN";
      var candidates = ordered.filter(function (row) {
        var rv = row[col];
        if (rv === null || rv === undefined) return false;
        switch (op) {
          case "=": return rv === val;
          case "!=": return rv !== val;
          case "<": return rv < val;
          case ">": return rv > val;
          case "<=": return rv <= val;
          case ">=": return rv >= val;
        }
        return false;
      });
      examined = candidates.length;
      scanDesc = kind + " on " + meta.name + "(" + col + ") using B-tree (height=1) — " +
        examined + " row id(s) via index, " + (op === "=" ? "no full scan" : "in key order");
      scan = { kind: kind, col: col, op: op, value: val, rows: candidates };
    } else {
      examined = ordered.length;
      scanDesc = "FULL TABLE SCAN (" + examined + " row(s) examined)";
      scan = { kind: "SeqScan", rows: ordered };
    }
    var rows = scan.rows.slice();
    if (remaining) rows = rows.filter(function (r) { return evalExpr(remaining, r); });
    if (stmt.orderBy && stmt.orderBy.length) {
      stmt.orderBy.forEach(function (ob) { this._col(meta, ob[0]); }, this);
      rows.sort(function (a, b) { return compareRows(a, b, stmt.orderBy); });
    }
    var columns;
    if (stmt.columns[0] === "*") {
      columns = meta.columns.map(function (c) { return c.name; });
    } else {
      stmt.columns.forEach(function (c) { this._col(meta, c); }, this);
      columns = stmt.columns;
    }
    var projected = rows.map(function (r) {
      var out = {};
      columns.forEach(function (c) { out[c] = r[c] === undefined ? null : r[c]; });
      return out;
    });
    var offset = stmt.offset || 0;
    var limited = projected.slice(offset, stmt.limit === null || stmt.limit === undefined ? undefined : offset + stmt.limit);
    return { rows: limited, columns: columns, scanDesc: scanDesc, scan: scan, remaining: remaining, where: where };
  };

  Database.prototype.explainSelect = function (stmt) {
    var meta = this._getMeta(stmt.table);
    var planned = this.planSelect(stmt);
    var lines = ["EXPLAIN:"];
    if (stmt.limit !== null && stmt.limit !== undefined || stmt.offset) {
      lines.push("Limit(" + stmt.limit + (stmt.offset ? " OFFSET " + stmt.offset : "") + ")");
    }
    var cols = planned.columns.join(", ");
    lines.push((stmt.limit !== null && stmt.limit !== undefined || stmt.offset ? "  " : "") + "Project(" + cols + ")");
    var depth = (stmt.limit !== null && stmt.limit !== undefined || stmt.offset) ? 2 : 1;
    var pad = function (d) { return new Array(d + 1).join("  "); };
    if (stmt.orderBy && stmt.orderBy.length) {
      lines.push(pad(depth) + "Sort(" + stmt.orderBy.map(function (ob) { return ob[0] + (ob[1] ? " DESC" : " ASC"); }).join(", ") + ")");
      depth++;
    }
    if (planned.remaining) { lines.push(pad(depth) + "Filter"); depth++; }
    var scan = planned.scan;
    if (scan.kind === "SeqScan") {
      lines.push(pad(depth) + "SeqScan(" + meta.name + ") — FULL TABLE SCAN (" + meta.rows.length + " row(s) in table)");
    } else {
      lines.push(pad(depth) + "IndexScan(" + meta.name + "." + scan.col + " " + scan.op + " " + JSON.stringify(scan.value) + ") — " +
        scan.kind + " on " + meta.name + "(" + scan.col + ") using B-tree (height=1)");
    }
    var text = lines.join("\n");
    if (scan.kind === "SeqScan") {
      var idx = Object.keys(meta.indexes).sort();
      text += "\nNo usable index for this predicate (indexes on this table: " + (idx.length ? idx.join(", ") : "none") + ").";
    } else {
      text += "\nUses index on " + meta.name + "(" + scan.col + "); no full table scan.";
    }
    return text;
  };

  /* -- dispatch (mirrors minidb.execute / _dispatch) ------------------- */
  Database.prototype.execute = function (sql) {
    sql = (sql || "").trim();
    if (!sql) return { kind: "info", rows: null, message: "" };
    if (sql.endsWith(";")) sql = sql.slice(0, -1).trim();
    if (!sql) return { kind: "info", rows: null, message: "" };
    // REPL dot-commands (cli.py: .tables, .help; plus .schema here).
    if (sql[0] === ".") {
      var low = sql.toLowerCase();
      if (low === ".tables") return this._showTables();
      if (low === ".help" || low === "help") return { kind: "info", rows: null, message: HELP_TEXT };
      if (low.indexOf(".schema") === 0) {
        var parts = sql.split(/\s+/).filter(Boolean);
        if (parts.length < 2) throw new Error(".schema needs a table name: .schema users");
        return this._describe(parts[1]);
      }
      if (low === ".exit" || low === ".quit") return { kind: "info", rows: null, message: "This is the web console — just close the tab when you are done. Your data stays in this browser (localStorage)." };
      throw new Error("unknown command " + JSON.stringify(sql) + " (try .help)");
    }
    var stmt = new Parser(tokenize(sql)).parseStatement();

    if (stmt.kind === "begin") return { kind: "info", rows: null, message: this.begin() };
    if (stmt.kind === "commit") return { kind: "info", rows: null, message: this.commit() };
    if (stmt.kind === "rollback") return { kind: "info", rows: null, message: this.rollback() };

    var mutating = ["insert", "update", "delete", "createTable", "createIndex", "dropTable"].indexOf(stmt.kind) !== -1;
    var implicit = mutating && this.txnState === "none";
    var snapshot = null;
    if (implicit) snapshot = JSON.parse(JSON.stringify({ tables: this.tables }));
    try {
      var result = this._dispatch(stmt);
      return result;
    } catch (err) {
      if (implicit && snapshot) this.tables = snapshot.tables;
      else if (this.txnState === "active") this.txnState = "aborted";
      throw err;
    }
  };

  Database.prototype._dispatch = function (stmt) {
    var self = this;
    if (stmt.kind === "showTables") return this._showTables();
    if (stmt.kind === "describe") return this._describe(stmt.table);
    if (stmt.kind === "explain") {
      return { kind: "info", rows: null, message: this.explainSelect(stmt.stmt) };
    }
    if (stmt.kind === "createTable") {
      return { kind: "info", rows: null, message: this.createTable(stmt.name, stmt.columns) };
    }
    if (stmt.kind === "createIndex") {
      return { kind: "info", rows: null, message: this.createIndex(stmt.table, stmt.column, stmt.name, stmt.unique) };
    }
    if (stmt.kind === "dropTable") {
      return { kind: "info", rows: null, message: this.dropTable(stmt.name) };
    }
    if (stmt.kind === "insert") {
      var meta = this._getMeta(stmt.table);
      var cols = stmt.columns || meta.columns.map(function (c) { return c.name; });
      cols.forEach(function (c) { self._col(meta, c); });
      stmt.rows.forEach(function (vals) {
        if (cols.length !== vals.length) {
          throw new Error(cols.length + " column(s) but " + vals.length + " value(s)");
        }
      });
      var messages = stmt.rows.map(function (vals) {
        var row = {};
        cols.forEach(function (c, i) { row[c] = vals[i]; });
        return self.insert(stmt.table, row);
      });
      if (messages.length === 1) return { kind: "info", rows: null, message: messages[0] };
      return { kind: "info", rows: null, message: messages.length + " row(s) inserted into " + stmt.table + "." };
    }
    if (stmt.kind === "select") {
      var planned = this.planSelect(stmt);
      return { kind: "rows", rows: planned.rows, message: planned.rows.length + " row(s). Plan: " + planned.scanDesc };
    }
    if (stmt.kind === "delete") {
      var dmeta = this._getMeta(stmt.table);
      var dwhere = stmt.where ? analyzeWhere(stmt.where, dmeta) : null;
      var dpred = dwhere ? function (row) { return evalExpr(dwhere, row); } : function () { return true; };
      return { kind: "info", rows: null, message: this.deleteRows(stmt.table, dpred) };
    }
    if (stmt.kind === "update") {
      var umeta = this._getMeta(stmt.table);
      var uwhere = stmt.where ? analyzeWhere(stmt.where, umeta) : null;
      var upred = uwhere ? function (row) { return evalExpr(uwhere, row); } : function () { return true; };
      return { kind: "info", rows: null, message: this.updateRows(stmt.table, stmt.assignments, upred) };
    }
    throw new Error("unsupported statement " + JSON.stringify(stmt.kind));
  };

  Database.prototype._showTables = function () {
    var rows = this.listTables().map(function (meta) {
      return {
        table: meta.name,
        columns: meta.columns.map(function (c) {
          return c.name + " " + c.type + (c.pk ? " PRIMARY KEY" : "") + (c.notNull ? " NOT NULL" : "") + (c.unique ? " UNIQUE" : "");
        }).join(", "),
        indexes: Object.keys(meta.indexes).sort().join(", ") || "(none)",
        rows: meta.rows.length
      };
    });
    return { kind: "rows", rows: rows, message: rows.length + " table(s)." };
  };

  Database.prototype._describe = function (table) {
    var meta = this._getMeta(table);
    var rows = meta.columns.map(function (c) {
      return {
        column: c.name,
        type: c.type,
        indexed: (c.name in meta.indexes || c.name === meta.pk) ? "yes" : "no",
        pk: c.pk ? "yes" : "no",
        not_null: (c.notNull || c.pk) ? "yes" : "no",
        unique: (c.unique || c.pk) ? "yes" : "no"
      };
    });
    return { kind: "rows", rows: rows, message: "Table " + table + ": " + rows.length + " column(s)." };
  };

  // Split a script on ';' outside string literals (demo.sql style).
  function splitStatements(text) {
    var out = [], buf = "", inStr = false, i = 0;
    while (i < text.length) {
      var ch = text[i];
      if (ch === "'") {
        if (inStr && text[i + 1] === "'") { buf += "''"; i += 2; continue; }
        inStr = !inStr; buf += ch; i++; continue;
      }
      if (ch === ";" && !inStr) { out.push(buf); buf = ""; i++; continue; }
      buf += ch; i++;
    }
    if (buf.trim()) out.push(buf);
    return out.map(function (s) { return s.trim(); }).filter(Boolean);
  }

  Database.prototype.executeScript = function (text) {
    var self = this;
    var results = [];
    splitStatements(text.replace(/--[^\n]*/g, " ")).forEach(function (stmt) {
      results.push({ sql: stmt, result: self.execute(stmt) });
    });
    return results;
  };

  var HELP_TEXT = [
    "minidb (web) — SQL supported here:",
    "  CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT, email TEXT UNIQUE);",
    "  CREATE INDEX idx_users_age ON users (age);   -- or: CREATE INDEX ON users (age);",
    "  INSERT INTO users VALUES (1, 'Alice', 30);",
    "  INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25), (3, 'Carol', 35);",
    "  SELECT * FROM users;",
    "  SELECT name, age FROM users WHERE age > 25 ORDER BY age DESC LIMIT 2;",
    "  SELECT * FROM users WHERE name = 'Alice' OR age IS NULL;",
    "  UPDATE users SET age = 31 WHERE id = 1;",
    "  DELETE FROM users WHERE id = 2;",
    "  BEGIN;  COMMIT;  ROLLBACK;   -- transactions (in-memory snapshot)",
    "  SHOW TABLES;  DESCRIBE users;  EXPLAIN SELECT * FROM users WHERE age = 30;",
    "  DROP TABLE users;",
    "  .tables  .schema users  .help",
    "Types: INT (INTEGER), TEXT (VARCHAR), FLOAT (REAL), BOOL (BOOLEAN).",
    "Strings use single quotes. NULL is supported; PRIMARY KEY / NOT NULL /",
    "UNIQUE are enforced. Data is saved in this browser (localStorage)."
  ].join("\n");

  return { Database: Database, tokenize: tokenize, splitStatements: splitStatements, HELP_TEXT: HELP_TEXT };
});
