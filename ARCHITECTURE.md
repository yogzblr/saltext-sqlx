# saltext-sqlx — ARCHITECTURE

## 1. Layout

```
src/saltext/sqlx/
  modules/sqlx_mod.py      thin: __virtual__, public functions, Salt dunders, exception translation
  lib/
    options.py             validate (drops __*, unknown -> E_OPTION_UNKNOWN) and apply_caps (clamps to the lowest of
                           call, profile, hard maximum)
    profiles.py            validate_profile / load_profile for sqlx:profiles:<name> (config or pillar passed in)
    secrets.py             resolve_secret / resolve_profile_secrets (sdb:// via an injected getter), mask,
                           MaskingFilter (add_secret)
    dialects.py            per-dialect adapter table (driver name, URL builder, connect args allowlist,
                           session setup SQL, timeout hook, ping SQL, version query). Single source of truth for the
                           dialect -> driver mapping: T4 moves profiles.DIALECT_DRIVERS here and profiles.py imports it
    engine.py              build engine (NullPool) from a validated profile; apply session setup
    runner.py              run_query / run_execute: transaction handling, fetchmany(limit+1), byte cap
    serialize.py           serialize_value / serialize_row / build_result (value -> strict JSON, limit and max_bytes)
    errors.py              exception -> (code, message) mapping; the ONE error path
    checks.py              operator table + retry loop; check_query_result / check_row_count take an injected run callable
  loader/ (copier-generated)  entry point: [project.entry-points."salt.loader"]
tests/{unit,functional,integration}/
```

Rules: `lib/` has **no Salt dunders** and no `__salt__` access; everything it needs is passed in (profile dict, a
sdb getter, passed to `secrets.resolve_secret` / `resolve_profile_secrets`). `modules/sqlx_mod.py` stays under ~150 lines.

## 2. Call flow

```
salt-api -> master -> minion job -> sqlx_mod.query(**kwargs)
  1  options.validate(function, kwargs)           # drop __*, unknown -> E_OPTION_UNKNOWN, bad value -> E_OPTION_INVALID
  2  profiles.load_profile(name, config)          # E_PROFILE_UNKNOWN / E_PROFILE_INVALID
     options.apply_caps(opts, profile)            # clamp limit/timeout_ms/max_bytes/retry_timeout_s silently
  3  secrets.resolve_profile_secrets(profile, __salt__["sdb.get"])
                                                  # sdb:// -> value on a copy, E_SECRET on failure, never logged
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

The serialising and the byte cap are `serialize.build_result(columns, raw_rows, limit, max_bytes)`: it keeps at most `limit`
rows, adds rows while `len(compact_json(columns)) + len(compact_json(rows))` (UTF-8 bytes) stays within `max_bytes`, drops the
first row that does not fit and everything after it, and sets `truncated`. The runner adds `profile`, `elapsed_ms` and
the other fields afterwards; they are not counted (SPEC section 4.1).

## 5. Error translation

```python
def translate(exc, dialect):
    orig = getattr(exc, "orig", exc)
    code = sqlstate(orig) or vendor_code(orig)          # psycopg: orig.sqlstate; pymysql: orig.args[0]; oracledb: orig.args[0].code
    msg  = primary_message(orig)                        # psycopg: orig.diag.message_primary; others: args
    return map_to_E_code(dialect, code, type(exc)), msg
```

Map on the vendor code first for the MySQL family (SQLSTATE is absent for client-side errors and differs between MySQL and
MariaDB for the same code), on SQLSTATE for PostgreSQL, and only then on the SQLAlchemy class. Unknown → `E_INTERNAL` with the
class name only. The `try` must catch `Exception`, not only `DBAPIError`: `TypeError`, `RuntimeError`, `AttributeError`,
`ValueError` and `sqlalchemy.exc.InvalidRequestError` were observed reaching the caller (SPEC section 8). Take the message from
`exc.orig`, never `str()` of the SQLAlchemy exception (it embeds the SQL and the bound values). Observed fields: psycopg 3
`orig.sqlstate`, `orig.diag.message_primary`; PyMySQL `orig.args == (code, message)` and `orig.sqlstate`.

## 6. Read-only: layers

1. **Database role** (required, documented in SECURITY.md). The only real control.
2. Session guard from the dialect adapter (`SET TRANSACTION READ ONLY` where it exists). Best effort: observed that caller
   SQL can switch it off on PostgreSQL, MySQL and MariaDB (`SET TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE`, or
   `COMMIT` then write; SECURITY section 2). Oracle and SQL Server: NOT RUN.
3. `query` never commits.
4. `execute` is off unless the profile enables it, and has a verb filter and `dry_run`.

## 7. Profile loading and secrets

`profiles.load_profile(name, config)` takes `config.get("sqlx")` (minion config merged with pillar), passed in by the module.
Secrets: `password` may be `sdb://...` (or absent); `secrets.resolve_profile_secrets(profile, getter)` returns a deep copy with
`password` resolved by `secrets.resolve_secret`, where `getter` is `__salt__["sdb.get"]`. Any failure, an empty or non-string
result, or a result equal to the reference raises `CommandExecutionError("[E_SECRET] ...")` naming the reference and the
exception class only. Resolved values live in local variables only. Masking: `secrets.mask(text, secrets)` replaces known
values (raw, URL-quoted and repr-escaped forms) with `***`; `MaskingFilter` on the module logger masks the value of any
`key=value` pair whose key contains `password|secret|token`, any record attribute with such a name, and every value registered
with `MaskingFilter.add_secret(value)` once it has been resolved. It never drops a record.

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
