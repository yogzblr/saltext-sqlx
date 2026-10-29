# saltext-sqlx — SECURITY

## 1. Posture

This module runs **arbitrary SQL chosen by the caller** (a human, or an AI agent through an MCP server). It cannot make
arbitrary SQL safe. Safety comes from four places, in this order: the database role, the profile allowlist, Salt eauth,
and the module's limits. The module is a convenience and a second line of defence, not the boundary.

## 2. Verified fact: the in-session read-only guard is bypassable

On PostgreSQL 16 (psycopg 3.3.6, SQLAlchemy 2.0.55), after `SET TRANSACTION READ ONLY`:

| Caller SQL sequence | Result |
|---|---|
| `INSERT` | refused, SQLSTATE 25006 |
| `SET TRANSACTION READ WRITE` then `INSERT` | **write succeeded** |
| `COMMIT` then `INSERT` | **write succeeded** |
| connection option `default_transaction_read_only=on`, then `SET TRANSACTION READ WRITE`, then `INSERT` | **write succeeded** |

Therefore **every profile used by `query` must connect as a role that has only read privileges** (SELECT on the needed
schemas; no INSERT/UPDATE/DELETE/DDL; no EXECUTE on write-capable functions; no superuser; on Oracle no `ANY` system privileges such as `SELECT ANY TABLE`
beyond what is needed, and no `EXECUTE` on write-capable packages). `read_only: true` stays on as a backstop and as documentation of intent.
The module's behaviour on other databases is unverified (T1).

## 3. Threats and controls

| Threat | Control |
|---|---|
| Caller writes through `query` | Read-only DB role (section 2); `query` never commits |
| Caller uses `execute` without permission | `allow_execute` false by default; verb filter; `dry_run`; separate profile/role with write rights |
| Verb filter bypass (comments, CTEs, `;`) | Filter is best-effort only; the **role** limits what `execute` can do, so give the write profile minimal grants |
| Caller points the minion at an arbitrary host (SSRF, credential relay) | Profiles only; call arguments cannot set host, port, driver, URL or `connect_args` (D1) |
| Secrets in the Salt job cache | `password` is `sdb://` only in production; `allow_inline_secrets` is not provided. The call never carries a secret. Salt stores job args and returns on the master and event bus |
| SQL text and results in the job cache | They are stored by Salt. Prefer `params` for values, keep `row_limit` small, set `keep_jobs`, consider an external job cache disabled for this function. Treat results as data in the master's job cache for DPDP and sovereignty purposes |
| Error text leaking data | `DETAIL` lines (for example `Key (email)=(x) already exists`) are excluded unless `error_detail: true`; SQL text and bound values are never included |
| Driver option abuse (`ssl_context`, callables, local file read via `local_infile`, `allowLoadLocalInfile`) | Per-driver `connect_args` allowlist; reject anything not listed |
| Resource exhaustion | Row, byte, time and retry caps; hard maxima in code |
| Credential in logs/URLs | Build URLs with `URL.create`; never log a URL or password; masking filter; verified that a failed-connect exception message did not contain the password (psycopg) — re-verify per driver in T1 |
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
- [ ] `error_detail: false` verified not to leak `DETAIL` on a unique violation
- [ ] eauth regex example from section 4 tested through `salt-api`
- [ ] Dependency licences listed in `NOTICE` (MIT, BSD, Apache-2.0, LGPL accepted)
