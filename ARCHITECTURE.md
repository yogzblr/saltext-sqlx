# saltext-sqlx — ARCHITECTURE

## 1. Layout

```
src/saltext/sqlx/
  modules/sqlx_mod.py      thin: __virtual__, public functions, the one `_run` call path, Salt dunders, audit line
  lib/
    options.py             validate (drops __*, unknown -> E_OPTION_UNKNOWN) and apply_caps (clamps to the lowest of
                           call, profile, hard maximum)
    profiles.py            validate_profile / load_profile for sqlx:profiles:<name> (config or pillar passed in)
    secrets.py             resolve_secret / resolve_profile_secrets (sdb:// via an injected getter), mask,
                           MaskingFilter (add_secret)
    dialects.py            per-dialect adapter table (driver name, URL builder, query/connect_args allowlist,
                           session setup SQL, timeout hook, ping SQL, version query). Single source of truth for the
                           dialect -> driver mapping (DIALECT_DRIVERS); profiles.py imports it
    engine.py              build_engine (NullPool) from a validated profile; managed_engine; connection (applies session
                           setup, translates connect/setup/ping failures through errors.raise_for); ping
    runner.py              run_query / run_execute: transaction handling, fetchmany(limit+1), byte cap, execute policy
    serialize.py           serialize_value / serialize_row / build_result (value -> strict JSON, limit and max_bytes)
    errors.py              translate / describe / raise_for: exception -> code and message; the ONE error path
    checks.py              operator table + retry loop; check_query_result / check_row_count take an injected run callable
  loader/ (copier-generated)  entry point: [project.entry-points."salt.loader"]
tests/{unit,functional,integration}/
```

Rules: `lib/` has **no Salt dunders** and no `__salt__` access; everything it needs is passed in (profile dict, a
sdb getter, passed to `secrets.resolve_secret` / `resolve_profile_secrets`). `modules/sqlx_mod.py` stays a thin shell (about 240 lines today, mostly docstrings
and the audit line).

## 2. Call flow

```
salt-api -> master -> minion job -> sqlx_mod.query(**kwargs)           # every function goes through sqlx_mod._run
  0  overrides.prepare(function, kwargs, config) # split connection keywords off (feature off: unknown -> E_OPTION_UNKNOWN);
                                                  # merge flat + connection; default_profile / ad-hoc name
  1  options.validate(function, kwargs)           # drop __*, unknown -> E_OPTION_UNKNOWN, bad value -> E_OPTION_INVALID
  2  overrides.build_profile(call, name, config)  # no overrides: profiles.load_profile(name, config) unchanged.
                                                  # else policy gates (fields, host/CIDR/resolved IPs, ports, dialects,
                                                  # caps, read_only, ssl_mode, password form) -> E_POLICY, then
                                                  # profiles.validate_profile on the merged profile (tls.py included)
     execute only: profile allow_execute false -> E_EXECUTE_DISABLED   (before any secret or connection)
  3  overrides.resolve_secrets(profile, __salt__["sdb.get"])   # = secrets.resolve_profile_secrets
                                                  # sdb:// -> value on a copy, E_SECRET on failure, never logged
     MaskingFilter.add_secret(password)           # the resolved value is masked in this module's log records
  4  options.apply_caps(opts, profile)            # clamp limit/timeout_ms/max_bytes/retry_timeout_s silently
  5  engine.connection(profile, password, timeout_ms, read_only)
       dialects.adapter(dialect)                  # E_PROFILE_INVALID for an unknown dialect
       engine.build_engine                        # adapter.load_driver (E_DRIVER_MISSING), adapter.url (URL.create),
                                                  # adapter.connect_args (allowlist, E_POLICY), NullPool
       adapter.session_setup(conn, profile, timeout_ms, read_only)   # timeout; read-only guard (best effort)
  6  runner.run_query(conn, sql, params, limit=, max_bytes=)   # text(sql), fetchmany(limit+1), ROLLBACK
     runner.run_execute(conn, sql, params, dry_run=, execute_verbs=)   # verb check, then commit unless dry_run
  7  serialize.build_result -> dict (inside run_query); the module adds "profile"
  except Exception as exc:
       errors.translate(exc, dialect, error_detail=...)   # only to put the code in the audit line
       errors.raise_for(exc, dialect, error_detail=...)   # raises CommandExecutionError("[E_..] ...")
  finally: audit log line (SECURITY section 5); the engine is disposed by engine.connection / managed_engine
```

`execute` calls `engine.connection(..., read_only=False)`, so the best-effort read-only guard is not applied to it
(`allow_execute: true` wins over `read_only: true`, SPEC section 4.2).

## 3. Engine and transaction handling

- `sqlalchemy.create_engine(url, poolclass=NullPool, connect_args=...)`; one engine per call, disposed in `finally`.
  Minion jobs are short, and a pool would hold sockets in a long-lived process.
- `query`: execute, read, then **always** `conn.rollback()`. No `commit()` call exists on this path (source scan test,
  `tests/unit/lib/test_guard.py`).
- `execute`: `runner.run_execute` is the only code path that may call `conn.commit()`, and only when `dry_run` is false. It
  does not check `allow_execute`: `sqlx_mod._run` refuses with `E_EXECUTE_DISABLED` before a secret is resolved or a
  connection is opened. `dry_run` must be a real bool, the first keyword must be in `execute_verbs`, and any exception rolls back.
- Use `sqlalchemy.text(sql)` with bind params for all caller SQL. Do not use `exec_driver_sql` for caller SQL (the `%`
  problem in section 5 of SPEC). The module's own statements (timeouts, read-only guard, ping) use `exec_driver_sql`
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
errors.translate(exc, dialect, *, error_detail=False) -> (code, message)
errors.describe(exc, dialect, error_detail=False)     -> Translation(code, native, message)   # namedtuple
errors.raise_for(exc, dialect, error_detail=False)    # raises; message "[E_CODE] <dialect> <native>: <message>"
```

`describe` does the work; `translate` returns its `code` and `message`; `raise_for` builds the exception. The order inside
`describe`:

1. A `SaltInvocationError` / `CommandExecutionError` whose text starts with a valid `[E_...]` is passed through (same class and
   text, so validation, policy and secret errors keep their code); any other one is `E_INTERNAL`.
2. A SQLAlchemy `DBAPIError`: take `exc.orig` (the driver exception) and hand it to the dialect function (`_pg` for
   PostgreSQL, `_my` for `mysql` and `mariadb`, `_oracle`, `_mssql`). Map on the vendor code first for the MySQL family (SQLSTATE
   is absent for client-side errors and differs between MySQL and MariaDB for the same code), on SQLSTATE for PostgreSQL (message
   text only where PostgreSQL gives none), and never on the SQLAlchemy class alone. Unknown code -> `E_INTERNAL` with the driver
   class name only.
3. Anything else (`TypeError`, `RuntimeError`, `AttributeError`, `ValueError`, `InvalidRequestError`, `OSError`): the few
   observed cases are mapped by `_raw` (SPEC section 8); the rest is `E_INTERNAL` with the class name only.

The caller must catch `Exception`, not only `DBAPIError`; `sqlx_mod._run` and `engine.connection` do. The message comes from
`exc.orig`, never from `str()` of the SQLAlchemy exception (it embeds the SQL and the bound values); it is whitespace-normalised
and cut at 1000 characters, and row values echoed by the server are replaced by `...` unless `error_detail` is true. Observed
fields: psycopg 3 `orig.sqlstate`, `orig.diag.message_primary`; pg8000 `orig.args[0]` is a dict (`C` SQLSTATE, `M` message, `H` hint, `D` detail); PyMySQL `orig.args == (code, message)` and `orig.sqlstate`.
`raise_for` raises from inside a handler and then clears `__cause__` and `__context__`, so the original text cannot reach the
caller through chaining. The engine no longer classifies errors itself: `engine.connection` calls `raise_for` and only removes
the password and any URL from the final text as a second line of defence.

## 6. Read-only: layers

1. **Database role** (required, documented in SECURITY.md). The only real control.
2. Session guard from the dialect adapter (`SET TRANSACTION READ ONLY` where it exists). Best effort: observed that caller
   SQL can switch it off on PostgreSQL, MySQL and MariaDB (`SET TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE`, or
   `COMMIT` then write; SECURITY section 2). Oracle and SQL Server: NOT RUN.
3. `query` never commits.
4. `execute` is off unless the profile enables it, and has a verb filter (best effort, not a boundary) and `dry_run`. It runs
   without the session guard (`read_only=False`), so for `execute` the database role is the only read-only control.

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
    driver: str                    # "pg8000" (default) or "psycopg" for postgresql (SPEC D15)
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

## 10. Call-time connection parameters

`lib/overrides.py` (policy, merge, gates) and `lib/psql_compat.py` (argument translation and result shape of
`sqlx.psql_query`) are the only new modules; `sqlx_mod._run` calls `prepare`, `build_profile`, `resolve_secrets`, `overridden`
and `target`, and `profiles.validate_profile` honours a per-profile `allow_call_params` list. Neither module has Salt dunders
or driver imports; the policy mapping is passed in by the module. SPEC section 3.2 is the contract, SECURITY section 3 the
threat table.
