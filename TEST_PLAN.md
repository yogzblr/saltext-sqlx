# saltext-sqlx — TEST PLAN

Unit tests prove validation, mapping, limits and serialisation with no network. Functional tests prove behaviour against
**real databases** for every dialect. Do not mock SQLAlchemy engines in functional tests: the bugs this module exists to
avoid (lost errors, wrong exception classes, encoding) only show up against a real server.

## 1. Layers

| Layer | Needs | Where |
|---|---|---|
| Unit | Python only (SQLite via `sqlite+pysqlite` is allowed for generic runner tests) | `tests/unit/` |
| Functional | Docker + per-dialect containers | `tests/functional/`, marker per dialect (`pg`, `mysql`, `mariadb`, `oracle`, `mssql`) |
| Salt integration | `pytest-salt-factories` master + minion | `tests/integration/` |
| Manual smoke | real `salt-api` | SPEC section 11 |

Run matrix: Salt 3008.x (primary), 3007.x and 3006.x (compat); SQLAlchemy 2.0.x now, 2.1.x when D4 is resolved.

## 2. Test databases (PostgreSQL, MySQL and MariaDB observed natively; Oracle and SQL Server NOT RUN)

| Dialect | Suggested image | Notes |
|---|---|---|
| PostgreSQL | `postgres:16` | **Create the database as UTF8.** A SQL_ASCII database makes psycopg 3 return `bytes` for text and SQLAlchemy crashes at first connect with `TypeError: cannot use a string pattern on a bytes-like object` (seen when a cluster was created under the C locale). `initdb -E UTF8` or `POSTGRES_INITDB_ARGS="-E UTF8"` |
| MySQL | `mysql:8.0` | Observed on 8.0.46. `MAX_EXECUTION_TIME` raises 3024 for a SELECT over a table, is silent for a table-less `SELECT SLEEP()` and does not limit `INSERT ... SELECT` |
| MariaDB | `mariadb:10.11` | Observed on 10.11.14. Timeout variable is `max_statement_time` (seconds, fractions accepted); read-only variable is `tx_read_only` |
| Oracle | `gvenzl/oracle-free` | thin mode, no Instant Client |
| SQL Server | `mcr.microsoft.com/mssql/server` | requires accepting the vendor EULA in the compose file; ARM hosts may need emulation |

The Docker images and compose file have **not been run**: T1 observed PostgreSQL, MySQL and MariaDB from native packages, and Oracle and SQL Server not at all (registries returned HTTP 403). Changes to the suggestions are in `tests-support/NOTES.md`; the owner confirms the images and runs the Oracle and SQL Server probes (T1b).
Each database gets two accounts: `ro` (SELECT only) and `rw` (DML on a scratch schema), plus a seeded `orders` table with
text, numeric, timestamp, UUID/GUID, binary, JSON and NULL columns.

## 3. Unit tests

| File | Must cover |
|---|---|
| `test_options.py` | `__pub_*`/`__kwarg__` dropped; unknown key → `E_OPTION_UNKNOWN`; caps can only be lowered; wrong types → `E_OPTION_INVALID` |
| `test_profiles.py` | required keys; unknown key → `E_PROFILE_INVALID`; dialect/driver mismatch; defaults; call cannot override host/driver/url/connect_args |
| `test_secrets.py` | `sdb://` via injected callable; empty → `E_SECRET`; masking: no exception text or log record contains the secret, parametrised over every error path |
| `test_dialects.py` | URL built with `URL.create` for passwords containing `@ : / % ?`; connect_args allowlist; session-setup SQL uses `int()`-validated values |
| `test_errors.py` | table-driven: (dialect, sqlstate/vendor code, class) → code; includes the nine observed PostgreSQL rows, the MySQL/MariaDB rows from SPEC section 8 and the raw non-`DBAPIError` exceptions (`TypeError`, `RuntimeError`, `AttributeError`, `ValueError`, `InvalidRequestError`) with the code section 8 assigns each (`E_CONNECT`, `E_DRIVER_MISSING`, `E_OPTION_INVALID`, else `E_INTERNAL`); Oracle and SQL Server only the connect-refused rows (all else `E_INTERNAL` until T1b); a pre-prefixed `[E_...]` exception passes through; unknown → `E_INTERNAL` with class name only; `error_detail` false drops DETAIL; SQL text and params never in a message |
| `test_serialize.py` | each type in SPEC section 9; NaN/Infinity; very large ints; `bytes`; nested JSON; everything passes `json.dumps` |
| `test_runner.py` (SQLite) | `limit+1` truncation; `max_bytes` truncation; no-row statements; `query` always rolls back (assert via a second connection); `execute` commits only when allowed and not `dry_run` |
| `test_checks.py` | every operator; numeric-string comparison; missing cell → `ok:false`; truncated row count → `ok:null`; retry loop honours cap using a fake clock |
| `test_text_binds.py` | the three observed `text()` traps: `:name::int` is **not** a bind; `'{"a":1}'` needs `\:`; `%` fine under `text()` |
| `test_guard.py` | source scan: `commit(` only in `runner.run_execute`; no `exec_driver_sql(` called with caller SQL; no import of a driver outside `dialects.py` |
| `test_module.py` | `__virtual__` with/without SQLAlchemy; each function with an injected fake runner; returns JSON-serialisable |

## 4. Functional tests (every dialect)

| Area | Cases |
|---|---|
| `ping` | version string; wrong password → `E_AUTH_FAILED`; wrong port → `E_CONNECT`; unknown database |
| `query` | typed columns round-trip (SPEC section 9); NULLs; empty result; `limit` and `truncated`; binds with `CAST(:x AS ...)`; Unicode text; large value vs `max_bytes` |
| **error fidelity** | for each of: syntax error, missing table, missing column, division by zero, unique violation, permission denied, statement timeout — assert the code **and** that the message contains the server's own text. **Record every real exception class/code in `docs/WIRE_SHAPES.md` and correct SPEC section 8 where it differs.** |
| timeout | long sleep statement cancelled at `statement_timeout_ms` → `E_TIMEOUT` (each dialect's mechanism per SPEC section 7) |
| **read-only proofs** | as `ro` account: INSERT/UPDATE/DELETE/DDL refused by the database. As `rw` account through `query`: a write must not persist (rollback). Re-run the bypass attempts from SECURITY section 2 on every dialect and record the results |
| `execute` | disabled profile → `E_EXECUTE_DISABLED`; enabled: INSERT/UPDATE/DELETE with correct `rows_affected`; `dry_run` leaves data unchanged; verb outside `execute_verbs` → `E_POLICY`; commit visible from a second connection |
| `check_*` | pass, fail, retry-until-pass (insert from the harness mid-wait), truncated |
| `table_list` | schemas, views, pattern |
| encoding | PostgreSQL SQL_ASCII regression: expect a clear error or documented requirement, not a bare `TypeError` (wrap `engine.build` first-connect failures as `E_CONNECT`/`E_INTERNAL` with the class name) |

### Functional suites that exist today (`tests/functional/`)

| File | Covers | Databases |
|---|---|---|
| `test_engine_pg.py` | `engine.ping`, timeout and read-only session setup, call timeout can only lower the profile's, `NullPool` really closes, wrong password / port / unknown database, SQL_ASCII regression (needs `SQLX_TEST_PG_ASCII_DB`) | PostgreSQL (`pg`, needs `SQLX_TEST_PG_URL`) |
| `test_runner_functional.py` | `run_query` / `run_execute` against real servers: limits, truncation, rollback, commit visibility, `dry_run`, verbs | PostgreSQL, MySQL, MariaDB |
| `test_sqlx_module_functional.py` | the module functions with `__salt__` injected: `query` (incl. an `sdb://` profile), error fidelity, wrong password without the password in the text, `execute` disabled / dry run / commit / verb refused, `check_query_result`, `check_row_count`, `ping`, `table_list`, the audit line without statement text | PostgreSQL, MySQL, MariaDB (`pg`, `mysql`, `mariadb` markers) |

`tests/functional/modules/test_sqlx.py` is an empty placeholder. There is no Oracle or SQL Server functional suite (T1b), and no
functional test yet starts a real Salt minion (T10, section 5).

## 5. Salt integration and REST

Use `pytest-salt-factories`. 1) Call through `LocalClient().cmd(tgt, "sqlx.query", kwarg={...})` with JSON-typed kwargs
(`params` as an object, `limit` as an int) — the same load path as `salt-api` minus HTTP. 2) `__pub_*` keys do not
trigger `E_OPTION_UNKNOWN`; a typo'd option does. 3) With the real minion, a failing statement returns
`ERROR: [E_SQL] ...` and retcode 1 (assert with `full_return`); a validation failure returns `ERROR executing 'sqlx.query': [E_OPTION_INVALID] ...`, and `salt-call` prints `Error running 'sqlx.query': [E_...]` (SPEC section 8). Operators on the command line are YAML-quoted (`operator='">"'`, SPEC section 4). 4) `sdb://` password resolution with a throwaway profile.
5) eauth regex on `profile` from SECURITY section 4 refuses a non-matching profile. 6) Manual smoke with `curl` from SPEC
section 11; record the exact command in the README.

## 6. CI

`nox` sessions: `lint`, `unit` (3.10–3.12), `functional-<dialect>`, `integration`. Fail the build on coverage under 90%
for `src/saltext/sqlx/lib`, any `commit(` outside `run_execute`, and any dependency whose licence is outside MIT / BSD /
Apache-2.0 / LGPL (`pip-licenses`; GPL fails).
