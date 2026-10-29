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

| dialect | driver (pip extra) | URL | Licence | timeout mechanism | in-session read-only |
|---|---|---|---|---|---|
| `postgresql` | `psycopg[binary]` (extra `postgres`) | `postgresql+psycopg://` | LGPL-3.0 | `SET statement_timeout = <ms>` ✔ | `SET TRANSACTION READ ONLY` ✔ |
| `mysql` | `PyMySQL` (extra `mysql`) | `mysql+pymysql://` | MIT | `SET SESSION MAX_EXECUTION_TIME=<ms>` (MySQL ≥ 5.7.8, SELECT only); MariaDB: `SET SESSION max_statement_time=<s>` | `SET SESSION TRANSACTION READ ONLY` |
| `oracle` | `oracledb` thin mode (extra `oracle`) | `oracle+oracledb://` | Apache-2.0 / UPL | `connection.call_timeout` (ms) | `SET TRANSACTION READ ONLY` as first statement |
| `mssql` | `pymssql` (extra `mssql`) | `mssql+pymssql://` | LGPL-2.1 | `timeout` / `login_timeout` connect args (s) | none; role only |

Only the **PostgreSQL row is verified** (SQLAlchemy 2.0.55 + psycopg 3.3.6 + PostgreSQL 16). The other rows are from
documentation and must be pinned by task T1 against real servers. `mysqlclient` (GPL) is excluded. LGPL drivers are
accepted by the owner as unmodified pip dependencies (decision recorded 2026-10-29). `connect_args` keys are checked
against a per-driver allowlist; `ssl_context`, callables and file paths outside the TLS directory are rejected.

SQLAlchemy is pinned `>=2.0,<2.1` until T1 has also run the suite against 2.1.x (D4).

## 8. Errors

One path: any `sqlalchemy.exc.DBAPIError` (and driver exceptions during connect) is mapped to
`CommandExecutionError("[E_CODE] <dialect> <sqlstate-or-vendor-code>: <message>")`. Salt returns this as
`ERROR: [E_CODE] ...` with retcode 1. Validation failures raise `SaltInvocationError` with the same prefix style.

| Code | Meaning | Source |
|---|---|---|
| `E_PROFILE_UNKNOWN` | profile name not configured | validation |
| `E_PROFILE_INVALID` | missing/unknown/inconsistent profile key | validation |
| `E_OPTION_UNKNOWN` / `E_OPTION_INVALID` | bad call keyword or value | validation |
| `E_SECRET` | `sdb://` reference failed or resolved empty | secrets |
| `E_POLICY` | verb not allowed, limit above cap, forbidden driver arg | policy |
| `E_EXECUTE_DISABLED` | `execute` on a profile without `allow_execute` | policy |
| `E_DRIVER_MISSING` | driver package not installed | import |
| `E_CONNECT` | network/TLS/host failure | `OperationalError` before auth |
| `E_AUTH_FAILED` | bad credentials | driver code (PG 28P01/28000, MySQL 1045, Oracle ORA-01017, MSSQL 18456) |
| `E_SQL` | syntax error, unknown object/column, permission denied | `ProgrammingError`, SQLSTATE class 42 |
| `E_SQL_DATA` | data exception (division by zero, bad cast) | `DataError`, class 22 |
| `E_CONSTRAINT` | integrity violation | `IntegrityError`, class 23 |
| `E_READ_ONLY` | write attempted in a read-only context | PG 25006 and equivalents |
| `E_TIMEOUT` | statement timeout/cancel | PG 57014 and equivalents |
| `E_INTERNAL` | anything else; message is the exception **class name only** | catch-all |

Observed on PostgreSQL 16 (verified):

| Input | SQLAlchemy / driver class | SQLSTATE | Code |
|---|---|---|---|
| `selec 1` | ProgrammingError / SyntaxError | 42601 | `E_SQL` |
| `select * from nope` | ProgrammingError / UndefinedTable | 42P01 | `E_SQL` |
| `select 1/0` | DataError / DivisionByZero | 22012 | `E_SQL_DATA` |
| insert under `READ ONLY` | InternalError / ReadOnlySqlTransaction | 25006 | `E_READ_ONLY` |
| `pg_sleep(3)` with 300 ms timeout | OperationalError / QueryCanceled | 57014 | `E_TIMEOUT` |

Message text: the server's primary message (psycopg `diag.message_primary`; other drivers `exc.args`), plus `hint` when
present. `detail` is included only if `error_detail: true`. Never include the SQL text, bound values, or the URL.
Mapping must be keyed on SQLSTATE/vendor code first, exception class second.

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
