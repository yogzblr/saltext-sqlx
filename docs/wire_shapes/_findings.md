## 9. What the observations mean for the contract

Each item cites the table that proves it. Items marked *SPEC* were wrong or unlabelled in `SPEC.md` and were corrected in
the same change.

1. **Only three of five databases could run.** PostgreSQL 16.15, MySQL 8.0.46 and MariaDB 10.11.14 ran every case.
   Oracle and SQL Server could not be started (section 0), so every server-side Oracle and SQL Server row is **NOT RUN**.
   Their only real rows are the client-side connection failures, which need no server. Versions other than the ones
   listed in section 0 were not tested (for example MySQL 5.7, MariaDB 11, PostgreSQL 17).
2. **PostgreSQL connect-phase errors have no SQLSTATE.** Wrong password, unknown database and unreachable port are all
   `psycopg.OperationalError` with `args(str)`, `sqlstate` absent and an empty `diag` (section 1, cases 8, 9, x1). They
   differ only in message text (`FATAL:  password authentication failed ...`, `FATAL:  database ... does not exist`,
   `Connection refused`). *SPEC* listed "PG 28P01/28000" as the E_AUTH_FAILED source; that code is not observable here.
3. **PyMySQL does not map errors to classes the way SQLSTATE classes suggest.** Missing column (1054) and permission
   denied (1142) arrive as `OperationalError`, not `ProgrammingError`; division by zero in DML (1365) is
   `OperationalError`, not `DataError`; a bad bound integer (1366) is `DataError`. `SQLSTATE` is available on server
   errors (`exc.orig.sqlstate`, also on PyMySQL 1.2.3) but is `None` for client-side failures (2003, 2013). The same
   situation differs between servers: error 1366 has SQLSTATE `HY000` on MySQL and `22007` on MariaDB; the timeout errors
   are `HY000` (MySQL 3024) and `70100` (MariaDB 1969). Mapping must therefore use the vendor code first (R2), and a
   class-42 rule would wrongly classify "access denied to database" (1044, SQLSTATE 42000) as a SQL error.
4. **`SELECT 1/0` is not an error on MySQL or MariaDB.** It returns `NULL` (section 1, case 4). The error (1365, SQLSTATE 22012)
   only appears in a data-changing statement (case 4b), so a "division by zero" check on these servers needs DML or a
   different statement. PostgreSQL raises in both forms.
5. **Statement timeout is per dialect and has traps** (section 2).
   - PostgreSQL: `SET statement_timeout = 300` cancels after 0.3 s with 57014, as before.
   - MySQL 8.0.46: `SET SESSION MAX_EXECUTION_TIME=300` interrupted `SELECT SLEEP(3)` after 0.3 s **without raising an error**
     (the statement returned `1`). A `SELECT` over a table did raise 3024 (`OperationalError`, SQLSTATE `HY000`). An
     `INSERT ... SELECT SLEEP(1)` was not interrupted at all (1.0 s, no error). `max_statement_time` is an unknown variable on
     MySQL (1193).
   - MariaDB 10.11.14: `SET SESSION max_statement_time=0.3` (seconds; the fractional value was accepted and read back as
     0.3) raised 1969 (SQLSTATE `70100`) for `SELECT SLEEP`, `SELECT ... FROM table` **and** `INSERT ... SELECT`.
     `MAX_EXECUTION_TIME` is an unknown variable on MariaDB (1193). SQLAlchemy reports `dialect.is_mariadb` as `False` on MySQL and
     `True` on MariaDB, which is a usable way to choose the variable.
   - PyMySQL `read_timeout=1` is a client-side alternative: it raised `OperationalError` 2013 after 1.0 s (message
     differs by server: `The read operation timed out` vs `timed out`). Whether the server kept running the statement
     afterwards was not observed.
   - Oracle `call_timeout` and pymssql `timeout`: NOT RUN.
   - Connect timeouts were **not** demonstrated: connecting to 192.0.2.1 returned immediately (`Connection refused` for
     PyMySQL, oracledb, pymssql; `server closed the connection unexpectedly` for psycopg), so the sandbox network does not
     black-hole addresses.
6. **No in-session read-only guard held on any runnable dialect** (section 3). Every guard refused a plain write
   (PG 25006; MySQL/MariaDB 1792, SQLSTATE 25006), and every guard was bypassed by at least one statement a caller can send through `query`:
   - PostgreSQL: `SET TRANSACTION READ WRITE`, and `COMMIT` then write, and the same flip after a connection-level
     `default_transaction_read_only=on` (this reproduces the earlier result).
   - MySQL: `SET SESSION TRANSACTION READ ONLY` is bypassed by `SET SESSION TRANSACTION READ WRITE`,
     `SET TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE` and `SET SESSION transaction_read_only=0`; it survives `COMMIT`.
     `SET TRANSACTION READ ONLY` (next transaction only) is also bypassed by `COMMIT` then write. `START TRANSACTION READ ONLY`
     resisted `SET SESSION ... READ WRITE`, `SET TRANSACTION READ WRITE` (error 1568) and `transaction_read_only=0`, but not
     `START TRANSACTION READ WRITE` or `COMMIT` then write. The server variable `tx_read_only` is unknown (1193).
   - MariaDB: same pattern; the variable is `tx_read_only` (`transaction_read_only` is unknown, 1193) and it bypasses the
     guard.
   - A guard set through PyMySQL `init_command` is bypassed the same way as a statement guard.
   - Oracle `SET TRANSACTION READ ONLY` and SQL Server: NOT RUN.
   The `ro` account was refused by the database for INSERT, UPDATE, DELETE and CREATE TABLE and for SELECT in an
   unrelated schema on all three (section 1, cases 6, 6b-6e). A write by `rw` through a transaction that is rolled back did not persist
   (case x4).
7. **Not everything that reaches the caller is a `DBAPIError`.** Observed raw exceptions: `TypeError` on first connect to a
   SQL_ASCII PostgreSQL database (section 7, same text as BUILD_PROMPT); `RuntimeError` and `AttributeError` from PyMySQL
   authentication (section 6); `ValueError` from `exec_driver_sql` with a literal `%` under PyMySQL (section 5);
   `sqlalchemy.exc.StatementError`/`InvalidRequestError` for a literal colon bind trap. `errors.translate` has to
   catch `Exception` around connect and execute, not only `DBAPIError`.
8. **Error text can contain data and the statement.** `str(sqlalchemy_exc)` appends `[SQL: ...]` and, for statements with binds,
   `[parameters: ...]` including the bound values (x3 rows). The server's own message can also echo a bound value
   (PostgreSQL 22P02 primary message, MySQL/MariaDB 1366) and, on MySQL/MariaDB, a row value in the *primary* message
   (`Duplicate entry 'a@example.com' ...`, 1062). PostgreSQL keeps that value in `diag.message_detail`
   (`Key (email)=(a@example.com) already exists.`), which `error_detail: false` can drop. The primary message of the
   other two cannot be filtered that way.
9. **No password appeared** in `str(e)`, `repr(e)`, `repr(e.orig)`, `repr(e.orig.args)` or the formatted traceback for any
   observed case on PostgreSQL, MySQL, MariaDB, nor in the Oracle and SQL Server connection-failure rows. This covers the
   cases in section 1 only; successful-connect logging and driver debug output were not examined.
10. **PyMySQL 1.2.3 negotiated TLS with MySQL 8 without being asked** (cipher `TLS_AES_256_GCM_SHA384`, section 6). The server
    certificate is auto-generated and self-signed, so the connection was encrypted but no certificate was verified by
    the caller. With `ssl_disabled=True`, MySQL 8's default `caching_sha2_password` needs the `cryptography` package:
    without it PyMySQL raises `RuntimeError`; with it, the first connection after `FLUSH PRIVILEGES` raised
    `AttributeError: 'NoneType' object has no attribute 'is_auth_switch_request'` and the next succeeded. MariaDB
    (`mysql_native_password`) never needed it. `oracledb` declares `cryptography>=3.2.1` as a dependency and PyMySQL has an
    optional `rsa` extra for it (package metadata), so the Oracle extra brings it in; the MySQL extra alone does not.
11. **SQLAlchemy 2.1.6 behaved identically to 2.0.55** on every record for PostgreSQL, MySQL and MariaDB, and for the
    client-side Oracle and SQL Server records (section 8). The `text()` traps still occur on 2.1.6: `:a::int` is not a bind,
    `'{"a":1}'` raises `A value is required for bind parameter '1'`, `\:` escapes it, `CAST(:a AS ...)` and `%` inside
    `text()` work, and compiled output is identical on all five dialects. Only the documentation URL in SQLAlchemy's
    error text changed (`/e/20/` to `/e/21/`).
12. **`text()` and `%`.** On PyMySQL, SQLAlchemy doubles `%` in `text()` (`%%`) and the statement works; `exec_driver_sql`
    with a literal `%` raised `ValueError` (PostgreSQL/psycopg raised `psycopg.ProgrammingError`). For pymssql the
    compiled statement keeps a single `%` although its paramstyle is `pyformat`; whether that breaks a statement that
    has both a literal `%` and binds is NOT RUN.
13. **Types** (section 4): MySQL and MariaDB return `str` for the JSON column (MariaDB stores it as `LONGTEXT`; MySQL has a
    JSON type but `text()` still returned `str`) and for the `CHAR(36)` identifier, `bytes` for `BINARY`/`VARBINARY`,
    `Decimal` for `DECIMAL`, `datetime` (microsecond precision kept) for `DATETIME(3)`, `None` for NULL. Neither has a
    native UUID type, so the seed uses `CHAR(36)` and `BINARY(16)`. PostgreSQL returned `dict` and `UUID` for `jsonb` and
    `uuid`. Oracle and SQL Server: NOT RUN.
14. **Licences** (package metadata of the installed versions): psycopg 3.3.6 `LGPL-3.0-only`, PyMySQL 1.2.3 `MIT`,
    oracledb 26.1.0 `UPL-1.0 OR Apache-2.0`, pymssql 2.4.6 LGPL 2.1 text, cryptography 50.0.2 `Apache-2.0 OR BSD-3-Clause`,
    SQLAlchemy 2.0.55 `MIT`.

## 10. Not verified (explicitly)

- Oracle: `call_timeout` as the timeout mechanism, `SET TRANSACTION READ ONLY`, any ORA code or exception class for SQL
  errors, `ORA-01017` for a wrong password, behaviour of the bypass attempts, type mapping.
- SQL Server: `timeout` / `login_timeout` as timeout mechanisms, error 18456, the claim that no in-session read-only
  exists, any exception class for SQL errors, `%` with binds under pymssql, type mapping.
- Any server version other than PostgreSQL 16.15, MySQL 8.0.46 and MariaDB 10.11.14; any Python other than 3.13.16;
  any driver version other than those in section 0.
- Docker images (`docker-compose.yml` was never started), including whether the official images match the Ubuntu builds.
- Whether the server keeps running a statement after PyMySQL's `read_timeout` fires.
- That connect timeouts fire (the sandbox network never black-holes).
- TLS certificate verification behaviour of any driver.

## 11. Reproduce

```bash
tests-support/up.sh                                   # PostgreSQL, MySQL, MariaDB (native); NOT RUN: oracle, mssql
python -m venv /tmp/v && /tmp/v/bin/pip install 'sqlalchemy>=2.0,<2.1' 'psycopg[binary]' PyMySQL oracledb pymssql
for d in postgresql mysql mariadb oracle mssql; do /tmp/v/bin/python -I scripts/probes/probe_$d.py; done
/tmp/v/bin/python -I scripts/probes/probe_postgresql_sqlascii.py
/tmp/v/bin/python -I scripts/probes/probe_mysql_auth.py --label with-cryptography     # then again in a venv without cryptography
/tmp/v/bin/python -I scripts/probes/render_wire_shapes.py
```
Repeat the probes in a venv with `sqlalchemy==2.1.6` to refresh section 8 (the renderer expects both result sets).
