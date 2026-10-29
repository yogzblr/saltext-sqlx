# saltext-sqlx — SECURITY

## 1. Posture

This module runs **arbitrary SQL chosen by the caller** (a human, or an AI agent through an MCP server). It cannot make
arbitrary SQL safe. Safety comes from four places, in this order: the database role, the profile allowlist, Salt eauth,
and the module's limits. The module is a convenience and a second line of defence, not the boundary.

## 2. Verified fact: the in-session read-only guard is bypassable

Observed on three databases (task T1, 2026-10-29; full tables in `docs/WIRE_SHAPES.md` section 3). The same results were
obtained with SQLAlchemy 2.0.55 and 2.1.6. Test account `rw` (DML rights), a guard statement, then caller SQL through the
same connection, then `INSERT` and `COMMIT`; "persisted" was read back on a second connection.

**PostgreSQL 16.15** (psycopg 3.3.6), after `SET TRANSACTION READ ONLY`:

| Caller SQL sequence | Result |
|---|---|
| `INSERT` | refused, SQLSTATE 25006 |
| `SET TRANSACTION READ WRITE` then `INSERT` | **write succeeded and persisted** |
| `COMMIT` then `INSERT` | **write succeeded and persisted** |
| connection option `default_transaction_read_only=on`, then `SET TRANSACTION READ WRITE`, then `INSERT` | **write succeeded and persisted** |

**MySQL 8.0.46 and MariaDB 10.11.14** (PyMySQL 1.2.3). Every guard refused a plain `INSERT` (error 1792, SQLSTATE 25006).
Bypasses (W = write succeeded and persisted, R = refused; MySQL / MariaDB where they differ):

| Guard statement | `SET SESSION TRANSACTION READ WRITE` | `SET TRANSACTION READ WRITE` | `START TRANSACTION READ WRITE` | `SET SESSION transaction_read_only=0` | `SET SESSION tx_read_only=0` | `COMMIT` then write |
|---|---|---|---|---|---|---|
| `SET SESSION TRANSACTION READ ONLY` (the `init_command` variant gave the same result in the first five columns; `COMMIT` not tested for it) | W | W | W | W / R (variable unknown, 1193) | R (unknown, 1193) / W | R |
| `SET TRANSACTION READ ONLY` (next transaction only) | W | W | W | W / R (1193) | R (1193) / W | W |
| `START TRANSACTION READ ONLY` | R | R (error 1568) | **W** | R (1792) / R (1193) | R (1193) / R (1792) | **W** |

Every guard tested on MySQL and MariaDB is therefore bypassable by a statement a caller can send through `query`.
`START TRANSACTION READ ONLY` resisted the most variants and still fell to `START TRANSACTION READ WRITE` and to
`COMMIT` followed by a write.

**The database role did hold** on PostgreSQL, MySQL and MariaDB: account `ro` was refused by the database for INSERT, UPDATE, DELETE
and CREATE TABLE, and for SELECT in a schema it was not granted (PostgreSQL 42501; MySQL and MariaDB 1142). Account `rw`
used through a transaction that is rolled back left no row behind.

**Oracle and SQL Server: NOT RUN.** No server could be started (Docker Hub, MCR and the Oracle registry returned HTTP 403 in
the build environment), so neither `SET TRANSACTION READ ONLY` on Oracle nor the role behaviour on either was observed.
Treat them as unprotected by the guard until observed. The owner must run them on a machine with registry access, with
`tests-support/up.sh` and `scripts/probes/` (task T1b).

Therefore **every profile used by `query` must connect as a role that has only read privileges** (SELECT on the needed
schemas; no INSERT/UPDATE/DELETE/DDL; no EXECUTE on write-capable functions; no superuser; on Oracle no `ANY` system privileges such as `SELECT ANY TABLE`
beyond what is needed, and no `EXECUTE` on write-capable packages). `read_only: true` stays on as a backstop and as documentation of intent.

## 3. Threats and controls

| Threat | Control |
|---|---|
| Caller writes through `query` | Read-only DB role (section 2); `query` never commits |
| Caller uses `execute` without permission | `allow_execute` false by default; verb filter; `dry_run`; separate profile/role with write rights |
| Verb filter bypass (comments, CTEs, `;`) | Filter is best-effort only; the **role** limits what `execute` can do, so give the write profile minimal grants |
| Caller points the minion at an arbitrary host (SSRF, credential relay) | Profiles only; call arguments cannot set host, port, driver, URL or `connect_args` (D1) |
| Secrets in the Salt job cache | `password` is `sdb://` only in production; `allow_inline_secrets` is not provided. The call never carries a secret. Salt stores job args and returns on the master and event bus |
| SQL text and results in the job cache | They are stored by Salt. Prefer `params` for values, keep `row_limit` small, set `keep_jobs`, consider an external job cache disabled for this function. Treat results as data in the master's job cache for DPDP and sovereignty purposes |
| Error text leaking data | The module never adds the SQL text, `params`, the URL or a secret to a message, and never uses `str()` of the SQLAlchemy exception (it appends `[SQL: ...]` and `[parameters: ...]` with bound values; observed). `DETAIL` lines (for example `Key (email)=(x) already exists`) are excluded unless `error_detail: true`. **Limit, observed:** the server's own primary message can still echo a bound value (PostgreSQL 22P02, MySQL/MariaDB 1366) and, on MySQL/MariaDB, a row value (`Duplicate entry '...'`, 1062), which `error_detail: false` cannot remove. Keep secrets out of `params` and out of unique keys that the caller can probe |
| Driver option abuse (`ssl_context`, callables, local file read via `local_infile`, `allowLoadLocalInfile`) | Per-driver `connect_args` allowlist; reject anything not listed |
| Resource exhaustion | Row, byte, time and retry caps; hard maxima in code |
| Credential in logs/URLs | Build URLs with `URL.create`; never log a URL or password; masking filter (`secrets.MaskingFilter`, `secrets.mask`). Observed (T1): the account password did not appear in `str`, `repr`, `repr(orig)`, `repr(orig.args)` or the formatted traceback of any case on PostgreSQL, MySQL and MariaDB, nor in the Oracle and SQL Server connection-failure rows. Not examined: successful-connect logging and driver debug output. Oracle and SQL Server server-side cases: **NOT RUN** (owner task T1b) |
| Unreviewed LGPL code paths | LGPL drivers (psycopg, pymssql) are accepted as unmodified pip dependencies (owner decision 2026-10-29); do not vendor or patch them |
| Supply chain on an air-gapped host | Mirror wheels for each driver for Salt's Python version; pin versions; check hashes |

## 4. Authorization in Salt

Use `external_auth` to restrict who may call which function and which profile. Salt supports regex limits on `args` and
`kwargs`; arguments you do not list are unrestricted, so list `profile` explicitly:

```yaml
external_auth:
  pam:
    sre_ops:
      - 'db-minion-*':
          - sqlx.query:
              kwargs: {profile: '^(orders_pg|hr_oracle)$'}
          - sqlx.ping
    dba_writers:
      - 'db-minion-*':
          - sqlx.execute:
              kwargs: {profile: '^orders_pg_rw$'}
```

Give `execute` its own function entry and its own profile with a different DB account, so that read access never implies
write access.

## 5. Audit

Log one line per call at INFO: function, profile, dialect, statement SHA-256 prefix (first 12 hex), row count or
`rows_affected`, elapsed ms, outcome code. Never log the SQL text, params, rows or secrets. Include the Salt `__pub_user`
when present so actions are attributable.

## 6. Pre-release checklist

- [ ] A read-only role test on every supported dialect proves writes fail at the database (not in the module)
- [ ] Source scan: `commit(` appears only in `runner.run_execute`
- [ ] No `exec_driver_sql` on caller SQL
- [ ] Secret never appears in any log record, exception, or return (parametrised over every error path)
- [ ] Call arguments cannot set host/port/driver/URL/connect_args
- [ ] `connect_args` allowlist reviewed per driver
- [ ] `error_detail: false` verified not to leak `DETAIL` on a unique violation (PostgreSQL only; on MySQL/MariaDB the value is in the primary message, so document it instead)
- [ ] The Oracle and SQL Server probes have been run by the owner (T1b) and section 2 and SPEC sections 7-8 updated from them
- [ ] eauth regex example from section 4 tested through `salt-api`
- [ ] Dependency licences listed in `NOTICE` (MIT, BSD, Apache-2.0, LGPL accepted)
