# saltext-sqlx — SPEC

The contract. If code and this file disagree, fix this file in the same change.

`saltext.sqlx` is a Salt execution-module extension (virtualname `sqlx`) that runs SQL against Postgres, MySQL/MariaDB,
Oracle and SQL Server through **one** code path (SQLAlchemy Core) and returns **the database's real error** when a
statement fails. Its API shape is modelled on the Robot Framework `DatabaseLibrary` (Apache-2.0).

## 1. Why it exists

Salt's built-in SQL modules lose errors: `postgres.psql_query` returns `[]` on any `psql` failure; `mysql.query` catches
only `OperationalError` and returns `False`, while syntax errors escape as a raw traceback. This module has exactly one
error path (section 8), so every database fails the same, readable way.

## 2. Robot DatabaseLibrary mapping

| Robot keyword | Function | Notes |
|---|---|---|
| `Connect To Database` (+ `alias`) | **profile** (config/pillar) | Salt jobs are stateless. `alias` becomes `profile`. |
| `Query` | `sqlx.query` | Read-only. |
| `Execute Sql String` | `sqlx.execute` | **Disabled per profile until `allow_execute: true`.** |
| `Check Query Result` | `sqlx.check_query_result` | Returns data, not an error, when the check fails. |
| `Check Row Count` | `sqlx.check_row_count` | Same. |
| (none) | `sqlx.ping`, `sqlx.table_list` | Connectivity test; schema listing via `sqlalchemy.inspect`. |
| `Switch Database`, `Disconnect From All Databases`, `Set Auto Commit` | not implemented | No connection state exists between jobs. |

## 3. Profiles

Profiles live under `sqlx:profiles:<name>` in minion config or pillar. **Callers choose a profile; they do not supply
connection details** (D1).

```yaml
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql          # postgresql | mysql | oracle | mssql
      driver: psycopg              # psycopg | pymysql | oracledb | pymssql  (must match dialect, see section 7)
      host: db1.internal
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg      # sdb:// reference, or a literal (see SECURITY.md)
      query: {}                            # URL query args, e.g. {sslmode: verify-full}
      connect_args: {}                     # allowlisted driver args only (section 7)
      read_only: true                      # default true
      allow_execute: false                 # default false
      execute_verbs: [INSERT, UPDATE, DELETE]   # best-effort first-keyword filter, NOT a security boundary
      statement_timeout_ms: 30000          # default 30000, hard max 300000
      connect_timeout_s: 10
      row_limit: 500                       # default 500, hard max 10000
      max_bytes: 1048576                   # response size cap, default 1 MiB, hard max 8 MiB
      error_detail: false                  # include the server's DETAIL line in errors (can contain row data)
```

Rules: unknown keys in a profile are `[E_PROFILE_INVALID]`. `host`/`port`/`database`/`username` are required. Call
arguments may **lower** `row_limit`, `statement_timeout_ms` and `max_bytes`, never raise them.

## 4. Functions

All arguments are keyword-friendly and JSON-typed, so they pass unchanged through the Salt REST API
(`"kwarg": {...}`). Keys beginning `__` (Salt's `__pub_*`) are dropped before validation. Any other unknown keyword is
`[E_OPTION_UNKNOWN]`.

### 4.1 `sqlx.query(profile, sql, params=None, limit=None, timeout_ms=None, max_bytes=None)`
Runs one statement inside a transaction that is **always rolled back**. Returns:

```json
{"profile": "orders_pg", "columns": ["id", "total"], "rows": [[1, "12.50"]], "row_count": 1,
 "truncated": false, "elapsed_ms": 4}
```

`rows` are positional lists (smaller, and it matches Robot's `${rows}[0][1]`); `columns` gives names. `truncated` is true
when `limit` or `max_bytes` cut the result (fetch `limit+1` to know). A statement that returns no rows (for example a
`SET`) returns `columns: []`, `rows: []`.

### 4.2 `sqlx.execute(profile, sql, params=None, dry_run=False, timeout_ms=None)`
Refused with `[E_EXECUTE_DISABLED]` unless the profile has `allow_execute: true`. Then: first keyword must be in
`execute_verbs` else `[E_POLICY]`; runs in a transaction; commits only if `dry_run` is false. Returns
`{"profile", "rows_affected", "committed": bool, "dry_run": bool, "elapsed_ms"}`. With `dry_run: true` the transaction is
rolled back and `rows_affected` reports what would have changed (default for `dry_run` is a decision, D2).

### 4.3 `sqlx.check_query_result(profile, sql, operator, expected, params=None, row=0, column=0, retry_timeout_s=0, retry_pause_s=1)`
Runs `sql` as in 4.1, takes `rows[row][column]`, compares with `expected`. Operators: `==`, `!=`, `<`, `<=`, `>`, `>=`,
`contains`, `not_contains`, `is_null`, `is_not_null`. Numeric strings are compared as numbers when both sides parse as
numbers. Returns `{"ok": bool, "actual": ..., "expected": ..., "operator": ..., "attempts": n}`. A failed comparison is
`ok: false`, **not** an error. A missing row/column is `ok: false` with `actual: null` and `reason: "no_such_cell"`.
`retry_timeout_s` is capped at 10 (long waits belong to the caller).

### 4.4 `sqlx.check_row_count(profile, sql, operator, expected, params=None, retry_timeout_s=0, retry_pause_s=1)`
Same, comparing `len(rows)` (after the row limit; if `truncated`, returns `ok: null, reason: "truncated"`).

### 4.5 `sqlx.ping(profile)`
Connects and runs the dialect's trivial statement (`SELECT 1`; Oracle `SELECT 1 FROM DUAL`). Returns
`{"ok": true, "dialect", "server_version", "elapsed_ms"}`. Failures are errors with the usual codes.

### 4.6 `sqlx.table_list(profile, schema=None, pattern=None, include_views=False)`
Uses `sqlalchemy.inspect(...)`. Returns `{"schema", "tables": [...], "views": [...]}`.

## 5. Parameters and statement text

- Callers pass values via `params` (a JSON object). Binds in the SQL are written `:name`. **Write casts as
  `CAST(:name AS INTEGER)`, never `:name::int`** — SQLAlchemy `text()` does not treat `:name::int` as a bind (verified).
- A literal colon followed by a word or digit inside a string, for example `'{"a":1}'`, is mis-read as a bind by
  `text()` (verified: `A value is required for bind parameter '1'`). Callers escape it as `\:`. The module does not
  rewrite the SQL (D3).
- `%` is fine in `text()` for psycopg (verified). Never use `exec_driver_sql` for caller SQL: with psycopg `%` must be
  doubled even with no parameters (verified).
- One statement per call. Multi-statement strings are not parsed or blocked by the module; the database role is the
  control (SECURITY.md).
- Stored procedures with multiple result sets, OUT parameters and cursors are not supported (D7).

## 6. Limits (hard caps in code, profile and call can only lower them)

| Limit | Default | Hard max |
|---|---|---|
| rows | 500 | 10 000 |
| statement timeout | 30 s | 300 s |
| response bytes | 1 MiB | 8 MiB |
| connect timeout | 10 s | 60 s |
| `retry_timeout_s` | 0 | 10 |

## 7. Dialects and drivers

Labels. **observed**: seen on a real server by task T1 on 2026-10-29, evidence in `docs/WIRE_SHAPES.md` (`WS n` = its
section n). **unverified**: not run; kept from vendor documentation and must not be relied on until a server is available.
Servers actually run: PostgreSQL 16.15, MySQL 8.0.46, MariaDB 10.11.14, with SQLAlchemy 2.0.55 and 2.1.6, psycopg 3.3.6,
PyMySQL 1.2.3. Oracle and SQL Server could not be started in the T1 environment (registries blocked), so their rows are
unverified except where stated.

| dialect | driver (pip extra) | URL | Licence (package metadata) | timeout mechanism | in-session read-only |
|---|---|---|---|---|---|
| `postgresql` | `psycopg[binary]` (extra `postgres`) | `postgresql+psycopg://` | LGPL-3.0-only | `SET statement_timeout = <ms>`: cancels at the limit, SQLSTATE 57014. **observed** [WS 2] | `SET TRANSACTION READ ONLY`: refuses writes (25006) but **bypassable** (SECURITY.md section 2). **observed** [WS 3] |
| `mysql` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` | MIT | `SET SESSION MAX_EXECUTION_TIME=<ms>`: raises 3024 (SQLSTATE `HY000`) for a SELECT that reads tables; a table-less `SELECT SLEEP(n)` is cut short **silently** (returns 1, no error); `INSERT ... SELECT` is **not limited**. **observed** on 8.0.46 only [WS 2]. The earlier "MySQL >= 5.7.8" is **unverified**. | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792, SQLSTATE 25006) but **bypassable** by four statements. `START TRANSACTION READ ONLY` resists the most statements but is also bypassable. **observed** [WS 3] |
| `mariadb` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` (SQLAlchemy sets `dialect.is_mariadb`, `False` on MySQL and `True` on MariaDB; use it to pick the variable) | MIT | `SET SESSION max_statement_time=<seconds>`: fractional values accepted; raises 1969 (SQLSTATE `70100`) for SELECT and for `INSERT ... SELECT`. **observed** on 10.11.14 [WS 2]. `MAX_EXECUTION_TIME` does not exist here (1193). | `SET SESSION TRANSACTION READ ONLY`: refuses writes (1792) but **bypassable**; the session variable is `tx_read_only` (`transaction_read_only` does not exist, 1193). **observed** [WS 3] |
| `oracle` | `oracledb` thin mode (extra `oracle`) | `oracle+oracledb://` | UPL-1.0 OR Apache-2.0 | `connection.call_timeout` (ms): **unverified** (NOT RUN) | `SET TRANSACTION READ ONLY` as first statement: **unverified** (NOT RUN) |
| `mssql` | `pymssql` (extra `mssql`) | `mssql+pymssql://` | LGPL-2.1 | `timeout` / `login_timeout` connect args (s): **unverified** (NOT RUN) | none, role only: **unverified** (NOT RUN) |

Notes, all **observed** unless stated.
- MySQL and MariaDB share one driver and one URL scheme but differ in the timeout variable, the read-only variable, the
  error message wording, and SQLSTATE for the same vendor code (for example 1366 is `HY000` on MySQL and `22007` on MariaDB).
  The adapter must detect the flavour (`dialect.is_mariadb`) and may not assume one set of statements for both.
- PyMySQL 1.2.3 negotiated TLS with MySQL 8 by itself (self-signed server certificate, nothing verified by the caller). With
  `ssl_disabled=True`, MySQL 8's default `caching_sha2_password` needs the `cryptography` package: without it PyMySQL
  raises a bare `RuntimeError`; with it, the first connection after the server's auth cache was cleared raised a bare
  `AttributeError` and the next succeeded [WS 6]. `oracledb` already depends on `cryptography`; the `mysql` extra does not.
  Whether to add it is a dependency decision (R16) and was not made in T1.
- A PostgreSQL database created as SQL_ASCII fails on the first connect with a bare `TypeError: cannot use a string pattern
  on a bytes-like object` (not a `DBAPIError`) [WS 7].
- Choosing the MySQL/MariaDB read-only statement is a T4 decision; no tested statement is bypass-proof (WS 3).
- `connect_args` keys are checked against a per-driver allowlist; `ssl_context`, callables and file paths outside the TLS
  directory are rejected. (Design rule, not a database behaviour.)
- `mysqlclient` (GPL) is excluded. LGPL drivers are accepted by the owner as unmodified pip dependencies (decision
  recorded 2026-10-29).

SQLAlchemy is pinned `>=2.0,<2.1` in `pyproject.toml`. T1 ran the same cases on SQLAlchemy 2.1.6: PostgreSQL, MySQL and
MariaDB results and the client-side Oracle and SQL Server results were identical to 2.0.55, and the `text()` traps in
section 5 still occur [WS 8]. Widening the pin remains decision D4.

## 8. Errors

One path: any exception raised while connecting or executing is mapped to
`CommandExecutionError("[E_CODE] <dialect> <sqlstate-or-vendor-code>: <message>")`. Salt returns this as
`ERROR: [E_CODE] ...` with retcode 1. Validation failures raise `SaltInvocationError` with the same prefix style.
**Catching `DBAPIError` is not enough**: these reached the caller as other types (observed, WS 5-7):
`TypeError` (PostgreSQL SQL_ASCII, first connect), `RuntimeError` and `AttributeError` (PyMySQL authentication),
`ValueError` (PyMySQL with `exec_driver_sql` and a literal `%`), and `sqlalchemy.exc.StatementError` for a mis-read bind.
Those map to `E_INTERNAL` unless a rule below says otherwise.

| Code | Meaning | Source (observed unless marked unverified) |
|---|---|---|
| `E_PROFILE_UNKNOWN` | profile name not configured | validation |
| `E_PROFILE_INVALID` | missing/unknown/inconsistent profile key | validation |
| `E_OPTION_UNKNOWN` / `E_OPTION_INVALID` | bad call keyword or value | validation |
| `E_SECRET` | `sdb://` reference failed or resolved empty | secrets |
| `E_POLICY` | verb not allowed, limit above cap, forbidden driver arg | policy |
| `E_EXECUTE_DISABLED` | `execute` on a profile without `allow_execute` | policy |
| `E_DRIVER_MISSING` | driver package not installed | import. A missing optional dependency also surfaced as a bare `RuntimeError` naming `cryptography` (PyMySQL); mapping that to this code is a T5 choice |
| `E_CONNECT` | network/TLS/host failure | PostgreSQL `psycopg.OperationalError`, no SQLSTATE, text `Connection refused`; MySQL/MariaDB `OperationalError` 2003 (no SQLSTATE); Oracle `oracledb.exceptions.OperationalError` `DPY-6005`; SQL Server `pymssql.exceptions.OperationalError` 20009 (the last two are client-side rows, no server needed) |
| `E_AUTH_FAILED` | bad credentials | PostgreSQL: `psycopg.OperationalError` with **no SQLSTATE** (28P01 is not observable), only the text `FATAL:  password authentication failed`; MySQL/MariaDB: 1045 (SQLSTATE 28000). Access to an unknown/forbidden database as a non-admin gave 1044 (SQLSTATE 42000) on MySQL/MariaDB and `FATAL:  database ... does not exist` (no SQLSTATE) on PostgreSQL. Oracle `ORA-01017` and SQL Server 18456: **unverified** |
| `E_SQL` | syntax error, unknown object/column, permission denied | PostgreSQL `ProgrammingError`, class 42. MySQL/MariaDB: 1064 and 1146 are `ProgrammingError`, but 1054 (unknown column) and 1142 (permission denied) are `OperationalError`; all have SQLSTATE class 42, so key on the code, not the class |
| `E_SQL_DATA` | data exception (division by zero, bad cast) | PostgreSQL `DataError`, class 22. MySQL/MariaDB: 1365 (division by zero, only in data-changing statements; `SELECT 1/0` returns NULL) is `OperationalError`, SQLSTATE 22012; 1366 (bad value) is `DataError` with SQLSTATE `HY000` on MySQL and `22007` on MariaDB, so key on the vendor code |
| `E_CONSTRAINT` | integrity violation | PostgreSQL 23505 `IntegrityError`; MySQL/MariaDB 1062 `IntegrityError`, SQLSTATE 23000 |
| `E_READ_ONLY` | write attempted in a read-only context | PostgreSQL 25006 (`InternalError`); MySQL/MariaDB 1792 (`OperationalError`, SQLSTATE 25006). Oracle: **unverified** |
| `E_TIMEOUT` | statement timeout/cancel | PostgreSQL 57014 (`OperationalError`); MySQL 3024 (`HY000`); MariaDB 1969 (`70100`). MySQL does not raise for a table-less `SELECT SLEEP()` (WS 2). Oracle and SQL Server: **unverified** |
| `E_INTERNAL` | anything else; message is the exception **class name only** | catch-all, including the raw exceptions listed above |

Observed on PostgreSQL 16.15 (re-verified 2026-10-29; the five original rows are unchanged):

| Input | SQLAlchemy / driver class | SQLSTATE | Code |
|---|---|---|---|
| `selec 1` | ProgrammingError / SyntaxError | 42601 | `E_SQL` |
| `select * from nope` | ProgrammingError / UndefinedTable | 42P01 | `E_SQL` |
| `select nope from t` | ProgrammingError / UndefinedColumn | 42703 | `E_SQL` |
| `select 1/0` | DataError / DivisionByZero | 22012 | `E_SQL_DATA` |
| duplicate key | IntegrityError / UniqueViolation | 23505 | `E_CONSTRAINT` |
| insert as a SELECT-only role | ProgrammingError / InsufficientPrivilege | 42501 | `E_SQL` |
| insert under `READ ONLY` | InternalError / ReadOnlySqlTransaction | 25006 | `E_READ_ONLY` |
| `pg_sleep(3)` with 300 ms timeout | OperationalError / QueryCanceled | 57014 | `E_TIMEOUT` |
| wrong password / unknown database / port closed | OperationalError / `psycopg.OperationalError` | none | `E_AUTH_FAILED` / `E_CONNECT` by message text only |

Observed on MySQL 8.0.46 and MariaDB 10.11.14 (PyMySQL 1.2.3; both servers unless two values are shown, MySQL / MariaDB):

| Input | SQLAlchemy / driver class | SQLSTATE | Vendor code | Code |
|---|---|---|---|---|
| `selec 1` | ProgrammingError / ProgrammingError | 42000 | 1064 | `E_SQL` |
| `select * from nope` | ProgrammingError / ProgrammingError | 42S02 | 1146 | `E_SQL` |
| `select nope from t` | OperationalError / OperationalError | 42S22 | 1054 | `E_SQL` |
| `select 1/0` | no error, returns `NULL` | - | - | none |
| `insert ... select 1/0` | OperationalError / OperationalError | 22012 | 1365 | `E_SQL_DATA` |
| string bound to an integer column | DataError / DataError | HY000 / 22007 | 1366 | `E_SQL_DATA` |
| duplicate key | IntegrityError / IntegrityError | 23000 | 1062 | `E_CONSTRAINT` |
| insert as a SELECT-only user | OperationalError / OperationalError | 42000 | 1142 | `E_SQL` |
| insert under `READ ONLY` | OperationalError / OperationalError | 25006 | 1792 | `E_READ_ONLY` |
| timeout | OperationalError / OperationalError | HY000 / 70100 | 3024 / 1969 | `E_TIMEOUT` |
| wrong password | OperationalError / OperationalError | 28000 | 1045 | `E_AUTH_FAILED` |
| port closed | OperationalError / OperationalError | none | 2003 | `E_CONNECT` |
| client `read_timeout` expired | OperationalError / OperationalError | none | 2013 | not decided |

Oracle and SQL Server: only the connection-refused rows are observed (see `E_CONNECT`). All other classes and codes for
them are **unverified**.

Message text. Observed fields: psycopg `exc.orig.diag.message_primary` (plus `message_hint` on some errors and
`message_detail` where the server sends one); PyMySQL `exc.orig.args == (code, message)` and `exc.orig.sqlstate`; oracledb
`exc.orig.args[0]` is an error object with `full_code` and `message`; pymssql `exc.orig.args == ((code, b"message"),)`
(bytes). Use the message of `exc.orig` and **never `str()` of the SQLAlchemy exception**: it appends `[SQL: ...]` and
`[parameters: ...]` with the bound values. `detail` is included only if `error_detail: true`, which works on
PostgreSQL (the row value is in `message_detail`) but not on MySQL/MariaDB, where the row value is part of the primary
message (`Duplicate entry 'a@example.com'`). The server's primary message can also echo a bound value (PostgreSQL 22P02,
MySQL/MariaDB 1366), so "never include bound values" cannot be guaranteed for the server's own text. Never include the SQL
text or the URL. No account password was found in any exception text or traceback in any observed case (WS 9).
Mapping must be keyed on the vendor code and SQLSTATE first, exception class second; SQLSTATE is absent for PostgreSQL
connect-phase errors and for MySQL/MariaDB client-side errors, and differs between MySQL and MariaDB for the same vendor
code, so for the MySQL family the vendor code is the primary key.

## 9. Return-value rules

Plain JSON only. Conversions: `Decimal`→string (no float rounding), `datetime`/`date`/`time`→ISO-8601 string,
`timedelta`→string, `UUID`→string, `bytes`→`{"$bytes_b64": "..."}`, `dict`/`list` (jsonb, arrays) pass through,
anything else→`str()`. Confirmed on PostgreSQL: numeric→Decimal, timestamptz→datetime, uuid→UUID, bytea→bytes,
jsonb→dict, array→list, interval→timedelta.

## 10. Install

`pip install saltext-sqlx[postgres,mysql,oracle,mssql]` through `salt-pip` (Salt 3008's onedir is Python 3.11, so
reinstall after a Salt upgrade). `__virtual__` returns `(False, reason)` if SQLAlchemy is missing; each driver is imported
lazily, so a missing Oracle driver only affects Oracle profiles.

## 11. REST example

```bash
curl -sS -k https://salt-api.example:8000/run -H 'Content-Type: application/json' -d '[{
  "client":"local","tgt":"db-minion-01","fun":"sqlx.query",
  "kwarg":{"profile":"orders_pg","sql":"select id,total from orders where status = :s","params":{"s":"open"},"limit":50},
  "eauth":"pam","username":"<user>","password":"<password>"}]'
```

## 12. Open decisions (assumption used if unanswered)

| ID | Decision | Assumption |
|---|---|---|
| D1 | Allow ad-hoc connection details in the call? | No. Profile only. |
| D2 | Default for `execute(dry_run)` | `false` (matches Robot); consider `true` for agent use. |
| D3 | Rewrite SQL to escape literal colons? | No; document `\:`. |
| D4 | Support SQLAlchemy 2.1.x | Pin `<2.1` until tested. |
| D5 | Keep `table_list` in P0 | Yes. |
| D6 | Column-level result masking | Deferred. |
| D7 | Multiple result sets / OUT params | Not supported. |
| D8 | `pyodbc` as an alternative MSSQL driver | Deferred; keep the driver table pluggable. |
