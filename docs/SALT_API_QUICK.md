# sqlx through salt-api: one page

The full, tested version with every function and database is [SALT_API.md](SALT_API.md). Everything here is taken from it.

## Call

```bash
# 1. log in once (the token lives 12 hours)
TOKEN=$(curl -sS -k "$API/login" -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<password>","eauth":"<eauth>"}' | jq -r '.return[0].token')

# 2. call any sqlx function with POST / (POST /run ignores the token header; it needs eauth, username and password in the body)
curl -sS -k "$API/" -H "X-Auth-Token: $TOKEN" -H 'Accept: application/json' -H 'Content-Type: application/json' -d '[{
  "client": "local", "tgt": "db-minion-01", "fun": "sqlx.query",
  "kwarg": {"profile": "orders_pg", "sql": "select id from orders where id >= CAST(:min AS INTEGER)", "params": {"min": 2}}
}]'
```

JSON needs no shell quoting: `port` is an integer, `params` an object, `"operator": ">="` is a plain string. Binds are `:name` on every database.
The answer is `{"return": [{"<minion>": {...}}]}`. A failure is still HTTP 200 with the text under the minion id
(`"ERROR executing 'sqlx.query': [E_SQL] postgresql 42P01: relation \"x\" does not exist"`); only a failed login is HTTP 401.

## One call per database (connection in the call, no profile)

Add `"client":"local","tgt":"<minion>","fun":"sqlx.query"` around each `kwarg`. Use an `sdb://` reference for the password.

| Database | `kwarg` |
|---|---|
| PostgreSQL | `{"dialect":"postgresql","host":"db1","port":5432,"database":"orders","username":"ro","password":"sdb://vault/pg_ro","sql":"select 1"}` |
| MySQL | `{"dialect":"mysql","host":"db2","port":3306,"database":"scratch","username":"ro","password":"sdb://vault/my_ro","sql":"select 1"}` |
| MariaDB | same as MySQL (`"dialect":"mariadb"` is accepted in a call and means `mysql`) |
| Oracle * | `{"dialect":"oracle","host":"db3","port":1521,"database":"FREEPDB1","username":"ro","password":"sdb://vault/ora_ro","sql":"select 1 from dual"}` (`database` is the **service name**) |
| SQL Server * | `{"dialect":"mssql","host":"db4","port":1433,"database":"orders","username":"ro","password":"sdb://vault/ms_ro","sql":"select 1 as x"}` (no `LIMIT`: use `top` / `offset ... fetch`) |

\* Not run against a server in the build environment; the request is written from the code. PostgreSQL, MySQL and MariaDB were run for real.
By profile, replace the connection fields with `"profile":"<name>"`. PostgreSQL also has `sqlx.psql_query` with Salt's names
(`query`, `user`, `host`, `port`, `maintenance_db`, `password`).

## Functions and their own keywords

| Function | Keywords (R = required) |
|---|---|
| `query` | `sql` R, `params`, `limit`, `timeout_ms`, `max_bytes` |
| `execute` | `sql` R, `params`, `dry_run`, `timeout_ms` (profile with `allow_execute: true` only) |
| `check_query_result` | `sql` R, `operator` R, `expected`, `params` |
| `check_row_count` | `sql` R, `operator` R, `expected` R, `params`, `retry_timeout_s`, `retry_pause_s` |
| `ping` | none |
| `table_list` | `schema`, `pattern`, `include_views` |

## What a caller can send

| | Fields | Result if not allowed |
|---|---|---|
| **By default** (no `sqlx:_policy`) | the keywords above, and the connection: `dialect`, `driver`, `host`, `port`, `database`, `username`, `password` (literal or `sdb://`), with or without `profile` | |
| **Only if the operator lists it** in `sqlx:_policy:allow_call_params` | `ssl_mode`, `ssl_certs_dir`, `ssl_files`, `query`, `connect_args`, `read_only` (only `true`), `statement_timeout_ms` and `connect_timeout_s` (only lower) | `[E_POLICY] '<field>' may not be set from a call` |
| **Never** | `allow_execute`, `execute_verbs`, `error_detail`, `row_limit`, `_policy` | `[E_OPTION_UNKNOWN]` |
| | raising `limit`, `timeout_ms`, `max_bytes` or any timeout | clamped to the lowest of call, profile and hard maximum |
| | `read_only: false` | `[E_POLICY]` |
| | `execute` or `psql_query write=true` on a connection with no profile | `[E_EXECUTE_DISABLED]` |
| | link-local and cloud-metadata hosts (`169.254.0.0/16`, `fe80::/10`, `fd00:ec2::254`, `100.100.100.200`), also as decimal, hex or IPv4-mapped spellings | `[E_POLICY]` |
| | a profile's password to a different host, port, username or dialect | not sent |

A wrong key is `[E_OPTION_UNKNOWN]`, a wrong type or range `[E_OPTION_INVALID]`. Any other host the minion can reach is allowed by default, loopback and private addresses included.

## Tighten it (minion config, or pillar)

```yaml
sqlx:
  _policy:
    allow_call_params: []                  # profiles only: the strict contract
    # allow_call_params: [host, database, username, password]   # or a shorter list
    # allowed_hosts: [db1.internal, '*.db.corp.example', 10.20.0.0/16]
    # allowed_ports: [5432, 3306]
    # allowed_dialects: [postgresql, mysql]
    # allow_adhoc_connection: false
    # allow_call_literal_password: false   # only sdb:// references
```

eauth can pin which functions and which `profile` an API user may send, but it cannot forbid a keyword: a user pinned to a profile
can still add `host=` to it. Only the minion policy closes that.

## Password

A literal `password` in a call is stored in the master job cache, the event bus and the minion debug log in clear text, and anyone who can
read jobs sees it. Prefer `sdb://<profile>/<key>`.
