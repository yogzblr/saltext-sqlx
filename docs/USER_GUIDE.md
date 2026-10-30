# saltext-sqlx: install and use

`saltext-sqlx` adds one Salt execution module, `sqlx`, that runs SQL on PostgreSQL, MySQL/MariaDB, Oracle and SQL Server
and returns the **database's real error** (for example `[E_SQL] postgresql 42P01: relation "orders" does not exist`)
instead of Salt's `[]`, `False` or a generic "minion has an error". `SPEC.md` is the full contract; this guide is the
short path from nothing to a working call.

> **Verification status.** PostgreSQL 16, MySQL 8.0 and MariaDB 10.11 are tested against real servers. Oracle and SQL
> Server are **not**: their error mapping, TLS and call-time overrides are unit-tested only (task T1b in `TASKS.md`).

## 1. Requirements

| Item | Requirement |
|---|---|
| Salt | 3006 or newer (developed and tested on 3008) |
| Python | 3.10 or newer (Salt 3008 onedir ships 3.11) |
| SQLAlchemy | 2.0.x (installed as a dependency, pinned `>=2.0,<2.1`) |
| Database driver | one per dialect, installed as an extra (below) |
| Network | minion must reach the database host and port |

Drivers and licences: PostgreSQL `psycopg[binary]` (LGPL-3.0, unmodified pip package), MySQL/MariaDB `PyMySQL` (MIT),
Oracle `oracledb` thin mode (Apache-2.0/UPL, no Oracle client needed), SQL Server `pymssql` (LGPL). No GPL package is used.

## 2. Install

Install **on every minion that will talk to a database** (the module runs on the minion, not the master).

```bash
# Salt onedir (the normal case): use salt-pip so the package lands in Salt's own Python
salt-pip install 'saltext-sqlx[postgres]'                 # one dialect
salt-pip install 'saltext-sqlx[postgres,mysql]'           # several
salt-pip install 'saltext-sqlx[postgres,mysql,oracle,mssql]'

# Salt installed from system packages or a virtualenv
pip install 'saltext-sqlx[postgres]'

# Straight from GitHub (until a release is on PyPI)
salt-pip install 'saltext-sqlx[postgres] @ git+https://github.com/yogzblr/saltext-sqlx.git'
```

Then restart the minion so it loads the extension, and check it:

```bash
systemctl restart salt-minion
salt 'db-minion-01' sys.list_functions sqlx        # query, execute, check_query_result, check_row_count, ping, table_list, psql_query
```

If `sqlx` is missing, run `salt-call --local -l debug sys.list_modules` on the minion and look for the `sqlx` line in the
loader output; the module is not offered when SQLAlchemy cannot be imported. A missing database driver only affects
profiles of that dialect (`[E_DRIVER_MISSING]`).

Notes:
- A Salt upgrade replaces the onedir Python, so **reinstall** the extension afterwards.
- Air-gapped hosts: mirror the wheels for each driver for Salt's Python version (and hashes) into your internal index.
- `pymssql` wheel availability for Salt's Python 3.11 onedir has not been checked.

## 3. Configure a profile

Connection details live in **profiles** in the minion config or pillar, under `sqlx:profiles:<name>`. Callers choose a
profile; they do not send connection details (unless you opt in, section 8).

```yaml
# /etc/salt/minion.d/sqlx.conf   (or pillar)
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql            # postgresql | mysql | oracle | mssql  (use mysql for MariaDB)
      host: db1.internal
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg      # an sdb:// reference; a literal is allowed but visible in config
      read_only: true                      # default true
      allow_execute: false                 # default false
      statement_timeout_ms: 30000          # default 30000, hard max 300000
      connect_timeout_s: 10
      row_limit: 500                       # default 500, hard max 10000
```

**Use `sdb://` for passwords.** Restart the minion (or refresh config) after changing minion config.

**The read-only setting is a backstop, not the control.** It is bypassable from inside the session (observed on PostgreSQL,
MySQL and MariaDB). Make the profile connect as a database account that has only the privileges it needs (a read-only
role for `query`). Put writes on a **separate** profile with its own write-capable account and `allow_execute: true`.

Per-dialect example values: PostgreSQL port 5432, MySQL/MariaDB 3306, Oracle 1521 (`database` is the service name),
SQL Server 1433.

### TLS to the database (certificate directory)

```yaml
sqlx:
  _policy:
    ssl_certs_dir_root: /etc/salt/sqlx-certs          # the only place certificates may live
  profiles:
    orders_pg_tls:
      dialect: postgresql
      host: db1.internal                              # must be in the server certificate's SANs for verify-full
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg
      ssl_certs_dir: /etc/salt/sqlx-certs/orders_pg   # needs ca.pem; client.pem + client.key for mutual TLS
      ssl_mode: verify-full                           # default; or verify-ca
```

Rules: the directory must be under the policy root (symlink escapes are refused); the CA file is required; a client
certificate needs its key and the other way round; a private key readable by group or others is refused (`chmod 600`).
File names default to `ca.pem`, `client.pem`, `client.key`; override with `ssl_files: {ca: ..., cert: ..., key: ...}`.
Verified on PostgreSQL, MySQL and MariaDB; Oracle wallet mapping is unverified; SQL Server is refused.

## 4. Use it

```bash
salt 'db-minion-01' sqlx.ping profile=orders_pg
salt 'db-minion-01' sqlx.query profile=orders_pg sql="select id, total from orders where status = :s" params='{"s": "open"}' limit=50
salt 'db-minion-01' sqlx.table_list profile=orders_pg schema=public pattern='ord%'
salt 'db-minion-01' sqlx.check_row_count profile=orders_pg sql="select 1 from orders" operator='">="' expected=1
salt 'db-minion-01' sqlx.check_query_result profile=orders_pg sql="select count(*) from orders" operator='"=="' expected=3 retry_timeout_s=30
salt 'db-minion-01' sqlx.execute profile=orders_pg_rw sql="update orders set status = 'x' where id = :i" params='{"i": 7}' dry_run=true
```

| Function | Purpose |
|---|---|
| `query` | Run a read statement. Returns `columns`, `rows`, `row_count`, `truncated`, `elapsed_ms`. Always rolls back; never commits |
| `execute` | Run INSERT/UPDATE/DELETE (verbs configurable). Needs `allow_execute: true` on the profile. `dry_run=true` runs it and rolls back |
| `check_query_result` | Compare one value to `expected` with an operator; can retry until a timeout (Robot Framework style). A failed comparison is `ok: false`, not an error |
| `check_row_count` | Same for the number of rows |
| `ping` | Connect and report the server version |
| `table_list` | List tables (and views with `include_views=true`), optionally by `schema` and a LIKE-style `pattern` |
| `psql_query` | Salt-compatible `postgres.psql_query(query, user, host, port, maintenance_db, password, runas, write)`; see section 8 |

### Writing SQL for `sql=`

- Use **bind parameters**: `where status = :s` with `params='{"s": "open"}'`. Never put values into the SQL text.
- Casts are `CAST(:x AS integer)`, never `:x::integer`.
- A literal colon in a string is written `\:`.
- One statement per call. Multiple statements (`a; b`) are refused.
- Results are capped (default 500 rows, 1 MiB; hard max 10 000 rows, 8 MiB). When a cap is hit `truncated` is true. A call can only lower caps.

### Command-line quoting

Salt parses each `key=value` as YAML, so comparison operators need YAML quotes inside shell quotes:
`operator='"=="'`, `operator='">"'`. Unquoted `operator===` fails with a confusing "multiple values for argument 'profile'".
`params` and nested values are JSON/YAML: `params='{"s": "open", "n": 5}'`.

### From salt-api

```bash
curl -sS https://salt-api.example:8000/run -H 'Content-Type: application/json' \
  -d '[{"client":"local","tgt":"db-minion-01","fun":"sqlx.query",
        "kwarg":{"profile":"orders_pg","sql":"select id,total from orders where id >= CAST(:s AS INTEGER) order by id","params":{"s":2},"limit":10},
        "eauth":"pam","username":"sre_ops","password":"..."}]'
```

`kwarg` values are JSON-typed (numbers stay numbers, `params` is an object). The return looks like
`{"return":[{"db-minion-01":{"columns":[...],"rows":[...],"row_count":2,"truncated":false,"elapsed_ms":3,"profile":"orders_pg"}}]}`.

## 5. Reading errors

A failure always carries a code and the database's own text, never the SQL, bound values or secrets:

```
ERROR: [E_SQL] postgresql 42601: syntax error at or near "selec"
ERROR: [E_AUTH_FAILED] postgresql: connection failed: ... password authentication failed for user "salt_ro"
```

Remote jobs show `ERROR: [E_...]`; validation errors show `ERROR executing 'sqlx.query': [E_...]`; `salt-call` shows
`Error running 'sqlx.query': [E_...]`. The retcode is 1 in all cases. Match on the `[E_CODE]` token.

| Code | Meaning | Typical fix |
|---|---|---|
| `E_PROFILE_UNKNOWN` / `E_PROFILE_INVALID` | profile missing or its settings are wrong | check `sqlx:profiles:<name>` on that minion |
| `E_OPTION_UNKNOWN` / `E_OPTION_INVALID` | a call keyword is misspelt, mistyped or out of range | check spelling and YAML quoting |
| `E_SECRET` | `sdb://` reference did not resolve | check the sdb profile and key |
| `E_POLICY` | refused by policy (host not allowed, certificate path or key mode, literal password) | see `SECURITY.md` |
| `E_EXECUTE_DISABLED` | `execute` on a profile without `allow_execute: true` | use the write profile |
| `E_DRIVER_MISSING` | the driver for that dialect is not installed | `salt-pip install 'saltext-sqlx[<extra>]'` |
| `E_CONNECT` / `E_AUTH_FAILED` | cannot connect / wrong credentials or certificate | check host, port, firewall, account |
| `E_SQL` / `E_SQL_DATA` / `E_CONSTRAINT` | SQL error / bad data / constraint violation | the message is the database's own |
| `E_READ_ONLY` | write attempted in a read-only session | use a write profile |
| `E_TIMEOUT` | statement exceeded the timeout | tune `timeout_ms` or the query |
| `E_INTERNAL` | unexpected exception (class name only) | report it |

## 6. Who may call what (Salt eauth)

The module is a convenience and a second line of defence; the database role, the profile list and Salt `external_auth` are
the real boundary. Restrict by function **and by profile**:

```yaml
external_auth:
  pam:
    sre_ops:
      - 'db-minion-*':
          - sqlx.query:
              kwargs: {profile: '^(orders_pg|hr_oracle)$'}
          - sqlx.ping:
              kwargs: {profile: '^(orders_pg|hr_oracle)$'}
    dba_writers:
      - 'db-minion-*':
          - sqlx.execute:
              kwargs: {profile: '^orders_pg_rw$'}
```

A function listed **without** `kwargs` has no profile limit. Always anchor the regex. Arguments a rule does not name are not
restricted by eauth.

## 7. What Salt stores

Salt keeps the job load in the master job cache and on the event bus: the **SQL text and bound `params` are stored**. Passwords
are not (profiles hold them, resolved on the minion). Do not put secrets in `sql` or `params`; keep `row_limit` small and
set `keep_jobs`.

## 8. Optional: connection details in the call (off by default)

For compatibility with `postgres.psql_query` and for ad-hoc work, an operator can allow chosen fields to be supplied at call time.
Nothing changes until a policy is set:

```yaml
sqlx:
  default_profile: orders_pg
  _policy:
    allow_call_params: [host, port, database, username, password]   # explicit names only
    allowed_hosts: [db1.internal, db2.internal, 10.20.0.0/16]        # required when host is allowed
```

```bash
salt 'db-minion-01' sqlx.psql_query "select 1 as one" user=ro host=db1.internal port=5432 maintenance_db=orders password=sdb://vault/orders_ro
salt 'db-minion-01' sqlx.ping host=db2.internal port=5432 username=ro database=orders password=sdb://vault/orders_ro
```

Rules that no policy relaxes: the password from a call must be an `sdb://` reference (a literal needs
`allow_call_literal_password: true` and ends up in the job cache); changing `host`, `port`, `username` or `dialect` drops
the profile's password unless `inherit_password: true`; a caller can lower limits but never raise them, never enable
`execute`, never turn `read_only` off. `runas` is not supported. Pin `host` in your eauth regexes too. Details: `SPEC.md`
3.2 and `SECURITY.md`.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| `'sqlx.query' is not available` | extension installed in Salt's Python (`salt-pip list`)? minion restarted? |
| `[E_DRIVER_MISSING]` | install the extra for that dialect |
| `[E_PROFILE_UNKNOWN]` | profile defined on **that minion** (config/pillar), name spelt correctly; pillar refreshed |
| `[E_SECRET]` | `sdb://` URI resolves: `salt-call --local sdb.get sdb://vault/orders_pg` |
| `[E_AUTH_FAILED]` / `[E_CONNECT]` | host/port reachable from the minion, firewall, account, `pg_hba.conf` or MySQL host grants, TLS settings |
| `multiple values for argument 'profile'` | YAML-quote operators: `operator='"=="'` |
| Garbled text from PostgreSQL | the database must be UTF8, not SQL_ASCII |
| A write succeeded through `query` | the account has write privileges; fix the role, `read_only` is only a backstop |

Reporting a problem: include the `[E_CODE]` line, the dialect, and the profile name (never the password).
