# saltext-sqlx — ARCHITECTURE

## 1. Layout

```
src/saltext/sqlx/
  modules/sqlx_mod.py      thin: __virtual__, public functions, Salt dunders, exception translation
  lib/
    options.py             call-kwarg validation (drops __*, unknown -> E_OPTION_UNKNOWN), caps
    profiles.py            load + validate sqlx:profiles:<name> from __salt__['config.get'] / pillar
    secrets.py             sdb:// resolution via an injected callable; masking helpers
    dialects.py            per-dialect adapter table (driver name, URL builder, connect args allowlist,
                           session setup SQL, timeout hook, ping SQL, version query)
    engine.py              build engine (NullPool) from a validated profile; apply session setup
    runner.py              run_query / run_execute: transaction handling, fetchmany(limit+1), byte cap
    serialize.py           value -> JSON conversion
    errors.py              exception -> (code, message) mapping; the ONE error path
    checks.py              operator table + retry loop for check_* functions
  loader/ (copier-generated)  entry point: [project.entry-points."salt.loader"]
tests/{unit,functional,integration}/
```

Rules: `lib/` has **no Salt dunders** and no `__salt__` access; everything it needs is passed in (profile dict, a
`resolve_secret` callable). `modules/sqlx_mod.py` stays under ~150 lines.

## 2. Call flow

```
salt-api -> master -> minion job -> sqlx_mod.query(**kwargs)
  1  options.validate(kwargs)                     # drop __pub_*, unknown -> E_OPTION_UNKNOWN, clamp caps
  2  profiles.load(name)                          # E_PROFILE_UNKNOWN / E_PROFILE_INVALID
  3  secrets.resolve(profile)                     # sdb:// -> value, E_SECRET on failure, never logged
  4  dialects.adapter(profile.dialect)            # E_DRIVER_MISSING if import fails
  5  engine.build(profile, adapter)               # NullPool, connect args from allowlist
  6  with engine.connect() as conn:
       adapter.session_setup(conn, profile)       # timeout; read-only guard (best effort)
       runner.run_query(conn, sql, params, caps)  # text(sql), fetchmany(limit+1), ROLLBACK
  7  serialize -> dict
  except Exception as exc: errors.translate(exc, profile) -> raise CommandExecutionError("[E_..] ...")
  finally: engine.dispose()
```

## 3. Engine and transaction handling

- `sqlalchemy.create_engine(url, poolclass=NullPool, connect_args=...)`; one engine per call, disposed in `finally`.
  Minion jobs are short, and a pool would hold sockets in a long-lived process.
- `query`: execute, read, then **always** `conn.rollback()`. No `commit()` call exists on this path (source scan test).
- `execute`: the only code path that may call `conn.commit()`, and only when `allow_execute and not dry_run`.
- Use `sqlalchemy.text(sql)` with bind params for all caller SQL. Do not use `exec_driver_sql` for caller SQL (the `%`
  problem in section 4 of SPEC). The module's own statements (timeouts, read-only guard, ping) use `exec_driver_sql`
  with constant strings and integer values formatted by the module after `int()` validation.

## 4. Row reading

```python
result = conn.execute(text(sql), params or {})
if result.returns_rows:
    cols = list(result.keys())
    raw = result.fetchmany(limit + 1)
    truncated = len(raw) > limit
    rows = [serialize_row(r) for r in raw[:limit]]
    # then enforce max_bytes while serialising: stop, set truncated=True
else:
    cols, rows = [], []
```

## 5. Error translation

```python
def translate(exc, dialect):
    orig = getattr(exc, "orig", exc)
    code = sqlstate(orig) or vendor_code(orig)          # psycopg: orig.sqlstate; pymysql: orig.args[0]; oracledb: orig.args[0].code
    msg  = primary_message(orig)                        # psycopg: orig.diag.message_primary; others: args
    return map_to_E_code(dialect, code, type(exc)), msg
```

Map on `code` first (SQLSTATE class 22/23/42/25/57/28/08, vendor codes from the SPEC table), then on the SQLAlchemy
class. Unknown → `E_INTERNAL` with the class name only. Verified fields for psycopg 3: `orig.sqlstate`, `orig.diag.message_primary`.

## 6. Read-only: layers

1. **Database role** (required, documented in SECURITY.md). The only real control.
2. Session guard from the dialect adapter (`SET TRANSACTION READ ONLY` where it exists). Best effort: verified that caller
   SQL can switch it off (`SET TRANSACTION READ WRITE`, or `COMMIT` then write) on PostgreSQL.
3. `query` never commits.
4. `execute` is off unless the profile enables it, and has a verb filter and `dry_run`.

## 7. Profile loading and secrets

`profiles.load` reads `config.get("sqlx")` (minion config merged with pillar). Secrets: `password` may be `sdb://...`;
`secrets.resolve` calls `__salt__["sdb.get"]` through the injected callable. Resolved values live in local variables
only and are masked in any log record (`MaskingFilter` on the module logger for keys matching `password|secret|token`).

## 8. Dialect adapter interface

```python
class Adapter(Protocol):
    name: str                      # "postgresql"
    driver: str                    # "psycopg"
    pip_extra: str
    def url(self, profile, password) -> sqlalchemy.engine.URL: ...      # URL.create(), never string-format
    def connect_args(self, profile) -> dict: ...                        # allowlisted only
    def session_setup(self, conn, profile, timeout_ms, read_only) -> None: ...
    ping_sql: str
    def version(self, conn) -> str: ...
```

Build URLs with `sqlalchemy.engine.URL.create(...)` so passwords with `@`, `:` or `/` need no escaping.

## 9. Concurrency and safety

Each Salt job runs in its own process/thread; nothing is shared between calls, so no locks. `statement_timeout_ms`
bounds server time; `connect_timeout_s` bounds connect time; the minion's own job timeout is the outer bound.
