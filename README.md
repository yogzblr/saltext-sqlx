A call reaches a database in one of two ways:

- it **sends the connection details itself** (dialect, host, port, database, username, password) from the `salt` CLI,
  `salt-call` or salt-api, as Salt's own `postgres.psql_query` does. This needs **no minion configuration**; an operator
  narrows it or switches it off with `sqlx:_policy` (see "Connection details in the call"); or
- it names a **profile**, configured under `sqlx:profiles:<name>` in minion config or pillar (below).

# saltext-sqlx
Salt execution module that runs SQL on PostgreSQL, MySQL/MariaDB, Oracle and SQL Server and returns the database's real
error. **Start with [docs/USER_GUIDE.md](docs/USER_GUIDE.md)** (install, connecting, usage, errors, eauth); `SPEC.md` is the contract.

## Install

```bash
pip install saltext-sqlx                      # SQLAlchemy only; no database driver
pip install 'saltext-sqlx[postgres]'          # pg8000 (pure Python; used when psycopg is not installed)
pip install 'saltext-sqlx[postgres-psycopg]'  # psycopg[binary], only for profiles with `driver: psycopg`
pip install 'saltext-sqlx[mysql]'             # PyMySQL
pip install 'saltext-sqlx[oracle]'            # oracledb
pip install 'saltext-sqlx[mssql]'             # pymssql
```

On a Salt onedir install use `salt-pip install ...` instead of `pip`.

## Usage

The execution module has the virtualname `sqlx`. A call reaches a database in one of two ways:

- it names a **profile**, configured under `sqlx:profiles:<name>` in minion config or pillar (below); or
- it sends the **connection details itself** (dialect, host, port, database, username, `sdb://` password, TLS,
  timeouts) from the `salt` CLI, `salt-call` or salt-api, over a profile or with no profile at all. A minion policy
  switches this on and limits it; see "Connection details in the call".

`SPEC.md` is the contract.

Profile example (SPEC section 3):

```yaml
sqlx:
  profiles:
    orders_pg:
      dialect: postgresql          # required: postgresql | mysql | oracle | mssql
      driver: pg8000               # optional; PostgreSQL: pg8000 or psycopg (default: psycopg if installed, else pg8000); the other dialects have one driver
      host: db1.internal
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg      # sdb:// reference, or a literal (see SECURITY.md)
      read_only: true                      # default true
      allow_execute: false                 # default false
      statement_timeout_ms: 30000          # default 30000, hard max 300000
      connect_timeout_s: 10
      row_limit: 500                       # default 500, hard max 10000
```

TLS with a certificate directory (SPEC section 3.1, decision D12). The operator sets the root once; a profile names a
directory below it. Nothing about it can be chosen by a caller:

```yaml
sqlx:
  _policy:
    ssl_certs_dir_root: /etc/salt/sqlx-certs        # required for any ssl_certs_dir, else [E_POLICY]
  profiles:
    orders_pg_tls:
      dialect: postgresql                           # also mysql (MySQL and MariaDB); oracle is UNVERIFIED; mssql is refused
      host: db1.internal                            # must be in the server certificate's SANs for verify-full
      port: 5432
      database: orders
      username: salt_ro
      password: sdb://vault/orders_pg               # omit for certificate-only accounts
      ssl_certs_dir: /etc/salt/sqlx-certs/orders_pg # ca.pem (required); client.pem + client.key (optional, mutual TLS)
      ssl_mode: verify-full                         # default; or verify-ca (checks the chain, not the host name)
      # ssl_files: {ca: ca.crt, cert: client.crt, key: client.key}   # only if your file names differ
```

Keep private keys at mode `0600` (or `0640` owned by root); a key with group or world access is refused. Verified on
PostgreSQL 16, MySQL 8.0 and MariaDB 10.11 against throw-away certificates (`tests-support/tls/`). **Oracle and SQL Server
TLS are unverified**: Oracle's argument mapping is unit-tested only, and `ssl_certs_dir` is rejected for SQL Server.

```bash
salt 'db-minion-01' sqlx.ping profile=orders_pg
salt 'db-minion-01' sqlx.query profile=orders_pg sql="select id, total from orders where status = :s" params='{"s": "open"}' limit=50
```

A failing statement comes back with the database's own text and retcode 1, without the SQL or the bound values:
`ERROR: [E_SQL] postgresql 42P01: relation "t10_nope" does not exist`.

### The read-only guard is NOT the control

`read_only: true` sets a read-only session as a best-effort guard. It is bypassable from inside the session (observed on
PostgreSQL, MySQL and MariaDB; `SECURITY.md` section 2). **Point every profile at a database account that has only the
privileges it needs (a read-only role for `query`)**; the database role is the control. Keep `allow_execute: false`
except on a profile that uses a separate, write-capable account, and restrict who can call `sqlx.execute` with
`external_auth` (below).

### CLI quoting caveat

On the command line Salt parses each `key=value` with YAML, so comparison operators must be YAML-quoted, inside shell
quotes: `operator='"=="'`. Unquoted, `operator===` is split at the first `=` and fails with
`Passed invalid arguments ... got multiple values for argument 'profile'`, and `operator=>` fails with
`[E_OPTION_INVALID] option 'operator' must be a non-empty string` (both observed, `tests/integration/salt_path`).

```bash
salt 'db-minion-01' sqlx.check_query_result profile=orders_pg sql="select count(*) from orders" operator='"=="' expected=3
```

Validation errors are wrapped by the minion as `ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] ...`; errors raised by
the database path read `ERROR: [E_SQL] ...`. Both have retcode 1. Salt adds `__pub_*` keys to every job; they are
ignored, a misspelt option is `E_OPTION_UNKNOWN`.

### Usage via salt-api

Recorded from a real run of `tests/integration/salt_path/test_salt_api.py`: a test master with `rest_cherrypy` (plain
HTTP on a free port, `disable_ssl: true`), the `file` eauth backend with a throwaway user (SPEC section 11 uses `pam`;
a system user was not created for the test), and the live PostgreSQL test database. The request is the SPEC section 11
`curl` with `-k` and `https` replaced by plain `http`, and the minion id, profile and credentials of the test:

```bash
curl -sS http://127.0.0.1:51565/run -H 'Content-Type: application/json' -d '[{"client": "local", "tgt": "minion-A2zVLT", "fun": "sqlx.query", "kwarg": {"profile": "pg_ro", "sql": "select id,note from t10_orders where id >= CAST(:s AS INTEGER) order by id", "params": {"s": 2}, "limit": 1}, "eauth": "file", "username": "t10_alice", "password": "t10_alice_pw_5b1e"}]'
```

```json
{"return": [{"minion-A2zVLT": {"columns": ["id", "note"], "rows": [[2, "two"]], "row_count": 1, "truncated": true, "elapsed_ms": 1, "profile": "pg_ro"}}]}
```

`params` arrives as an object and `limit` as an integer. A request for a profile the user is not allowed
(`mysql_ro`) returns cherrypy's HTML error page and no job runs.

### Connection details in the call

**On by default** (SPEC decision D16, owner decision): with nothing in the minion config, a call may send `dialect`,
`driver`, `host`, `port`, `database`, `username` and `password` on every function and dialect, with or without a profile.
`psql_query` keeps Salt's names `user` and `maintenance_db`. A literal `password` works, as in Salt; an `sdb://` reference
works too and is recommended, because **a literal password in a call is stored in clear text in the Salt job cache, on the
event bus and in the minion's debug log** (that is how Salt treats every argument, `postgres.psql_query` included; the
extension itself never logs or returns it).

Real runs (`salt-call --local` on a minion whose config has no `sqlx:` section at all; PostgreSQL 16.15, MySQL 8.0.46,
MariaDB 10.11.14; output abridged to one line):

```bash
salt-call --local sqlx.psql_query 'select 1' host=127.0.0.1 port=54329 user=ro maintenance_db=sqlx_test password=ro_pw
#   [{"?column?": "1"}]
salt-call --local sqlx.query sql='select current_user() as who, version() as v' dialect=mysql host=127.0.0.1 port=33061 username=ro database=scratch password=ro_pw
#   {"columns": ["who", "v"], "rows": [["ro@%", "8.0.46-0ubuntu0.24.04.4"]], "row_count": 1, "truncated": false, "elapsed_ms": 0, "profile": "(adhoc)"}
salt-call --local sqlx.query sql='select current_user() as who, version() as v' dialect=mariadb host=127.0.0.1 port=33062 username=ro database=scratch password=ro_pw
#   {"columns": ["who", "v"], "rows": [["ro@%", "10.11.14-MariaDB-0ubuntu0.24.04.1"]], "row_count": 1, "truncated": false, "elapsed_ms": 0, "profile": "(adhoc)"}
salt-call --local sqlx.check_row_count sql='select 1' operator='"=="' expected=1 dialect=postgresql host=127.0.0.1 port=54329 username=ro database=sqlx_test password=ro_pw
#   {"ok": true, "actual": 1, "expected": 1, "operator": "==", "attempts": 1, "profile": "(adhoc)"}
salt-call --local sqlx.table_list dialect=mariadb host=127.0.0.1 port=33062 username=ro database=scratch password=ro_pw
#   {"schema": "scratch", "tables": ["orders", "t_probe", "uniq"], "views": [], "profile": "(adhoc)"}

# refused, with the same default
salt-call --local sqlx.execute sql='insert into scratch.t_probe values (1)' dialect=postgresql host=127.0.0.1 port=54329 username=rw database=sqlx_test password=rw_pw
#   Error running 'sqlx.execute': [E_EXECUTE_DISABLED] profile '(adhoc)' does not set allow_execute: true
salt-call --local sqlx.ping dialect=postgresql host=169.254.169.254 database=d username=u password=x
#   Passed invalid arguments: [E_POLICY] 'host' is a link-local or cloud-metadata address; the operator can allow it by listing it in sqlx:_policy:allowed_hosts.
```

What the default does not open: `ssl_mode`, `ssl_certs_dir`, `ssl_files`, `query`, `connect_args`, `read_only` and the
timeouts are not accepted from a call (`[E_POLICY]`); `allow_execute`, `execute_verbs`, `error_detail`, `row_limit` and
`_policy` are never call keywords; caps and timeouts can only be lowered; a call without a profile can never run `execute`
(nor `psql_query write=True`), and neither can a call that carries connection fields on a writing profile; a retargeted
connection never receives the profile's password; link-local and cloud-metadata addresses (`169.254.0.0/16`, `fe80::/10`,
`fd00:ec2::254`, `100.100.100.200`) are refused on the resolved address. Loopback and private addresses are allowed.

**Tightening it** (minion config or pillar; a call can never send or change the policy):

```yaml
sqlx:
  _policy:
    allow_call_params: []      # strict: profiles only, every connection keyword is [E_OPTION_UNKNOWN] (the behaviour before D16)
```

```yaml
sqlx:
  _policy:                     # keep the default fields, restrict where and what
    allowed_hosts: [db1.internal, db2.internal, 10.20.0.0/16]   # names, globs, IPs, CIDRs; only these are allowed
    allowed_ports: [5432, 3306, 1521, 1433]                       # optional
    allowed_dialects: [postgresql, mysql]                         # optional
    allow_adhoc_connection: false                                 # a call must name a profile
    allow_call_literal_password: false                            # sdb:// references only
```

```yaml
sqlx:
  _policy:                     # an explicit list replaces the default completely (and turns ad-hoc and literals off unless set)
    allow_call_params: [dialect, driver, host, port, database, username, password, ssl_mode, ssl_certs_dir,
                        ssl_files, query, connect_args, connect_timeout_s, statement_timeout_ms, read_only]
    allow_adhoc_connection: true
    allowed_hosts: [db1.internal, db2.internal, 10.20.0.0/16]   # required when host is in the list
```

Verified earlier on all five databases through `salt`, `salt-call` and salt-api on a minion with an explicit list and no
profiles (`tests-e2e/test_e2e_adhoc.py`); outputs below are from that run (Sakila data):

```bash
salt minion-adhoc sqlx.ping dialect=postgresql host=pg port=5432 database=sakila username=ro password=sdb://e2e_sdb/ro_pw
#   {"ok": true, "dialect": "postgresql", "server_version": "18.4", "elapsed_ms": 185, "profile": "(adhoc)"}
salt-call sqlx.query sql="select count(*) as n from film" connection='{"dialect":"postgresql","host":"pg","port":5432,"database":"sakila","username":"ro","password":"sdb://e2e_sdb/ro_pw"}'
#   {"columns": ["n"], "rows": [[1000]], "row_count": 1, "truncated": false, "elapsed_ms": 15, "profile": "(adhoc)"}
```

A call can also **overlay** a profile: it names `profile=` (or the minion sets `sqlx:default_profile`) and sends only the
fields to change. A profile can narrow what a call may change with its own `allow_call_params: [...]`.

More real invocations (PostgreSQL test server; this minion had an explicit `allow_call_params` list, so a literal password
needed `allow_call_literal_password`; under the default a literal is accepted). Output lines are abridged JSON from real runs:

```bash
# flat keywords; port is YAML-typed, so port=5432 is an integer
salt-call --local sqlx.ping host=127.0.0.1 port=54329 username=ro database=sqlx_test password=sdb://cli_sdb/cli_pw
#   {"ok": true, "dialect": "postgresql", "server_version": "16.15 ...", "profile": "pg_dead"}

# one nested object (YAML/JSON parsed by Salt); give sql= by keyword when you also pass connection=
salt-call --local sqlx.query sql="select current_user as who" connection='{"host":"127.0.0.1","port":54329,"username":"ro","database":"sqlx_test","password":"sdb://cli_sdb/cli_pw"}'
#   {"columns": ["who"], "rows": [["ro"]], "row_count": 1, "truncated": false, "profile": "pg_dead"}

# Salt-shaped psql_query: list of dicts, values as text, NULL as null
salt-call --local sqlx.psql_query "select 1 as one, null as nothing" user=ro host=127.0.0.1 port=54329 maintenance_db=sqlx_test password=sdb://cli_sdb/cli_pw
#   [{"one": "1", "nothing": null}]

# refused (explicit list with allowed_hosts, no literals)
salt-call --local sqlx.ping host=192.0.2.9
#   Passed invalid arguments: [E_POLICY] 'host' is not in sqlx:_policy:allowed_hosts.
salt-call --local sqlx.ping host=127.0.0.1 port=54329 username=ro database=sqlx_test password=literalpw
#   Passed invalid arguments: [E_POLICY] 'password' from a call must be an sdb:// reference (a literal would be stored in the job cache and on the event bus).
salt-call --local sqlx.psql_query "select 1" runas=postgres
#   Passed invalid arguments: [E_OPTION_INVALID] runas is not supported; sqlx does not use psql or an OS user.
# strict (allow_call_params: [])
salt-call --local sqlx.psql_query 'select 1' host=127.0.0.1 port=54329 user=ro maintenance_db=sqlx_test password=ro_pw
#   Passed invalid arguments: [E_POLICY] call-time connection parameters are disabled (host, maintenance_db, password, port, user); the operator enables fields with sqlx:_policy:allow_call_params.
```

Through `salt-api` (`tests/integration/params_path`, `file` eauth, kwargs are JSON-typed, `port` is a number):

```bash
curl -sS http://127.0.0.1:36005/run -H 'Content-Type: application/json' -d '[{"client": "local", "tgt": "minion-qkq5Ke", "fun": "sqlx.query", "kwarg": {"host": "127.0.0.1", "port": 54329, "username": "ro", "database": "sqlx_test", "password": "sdb://t10_sdb/t10_pg_ro_password", "profile": "pg_dead", "sql": "select current_user"}, "eauth": "file", "username": "t10_alice", "password": "t10_alice_pw_5b1e"}]'
```
```json
{"return": [{"minion-qkq5Ke": {"columns": ["current_user"], "rows": [["ro"]], "row_count": 1, "truncated": false, "elapsed_ms": 1, "profile": "pg_dead"}}]}
```

Pitfalls: `salt-call` shows the function docstring for a `SaltInvocationError` and logs the message (run with `--log-level=error`
to see it); a quoted `port='5432'` is a string and is refused, never coerced; when a `connection=` object is given, name `sql=`
(a bare first word is taken as `profile`); `salt ... host=db2` is subject to `external_auth` regex limits on `host`,
`connection`, `username` and the other fields (SECURITY section 4), and kwargs a rule does not name are not restricted by eauth,
so the minion policy is the real control. For ad-hoc calls pin `host` (and `dialect`) in the eauth rule, since there is
no `profile` to match; such a rule refuses a call that hides its details in `connection=` (observed, HTTP 401), so callers
use the flat keywords. The user guide, section 6, has the rule. Hard rules no policy or default relaxes, the per-field table and the `psql_query` differences
from `postgres.psql_query` are in `SPEC.md` sections 3.2 and 4.7; the threat table is in `SECURITY.md` section 3.

### Authorization and what Salt stores

`external_auth` regex limits on `profile` work as in `SECURITY.md` section 4 (verified with `file` eauth): the permitted
user can call `sqlx.query` with `pg_ro`, is refused for any other profile (including a positional profile, a missing
profile and a longer name), and is refused for `sqlx.execute`. A function listed **without** `kwargs` (for example a bare
`- sqlx.ping`) has no profile limit at all, so give every entry its own `kwargs: {profile: ...}`.

Salt stores the job load: the **SQL text and the bound `params` of every call are in the master job cache** (`.load.p`),
on the event bus (`salt/job/<jid>/new`) and, at debug level, in the master and minion logs. The account password never
appeared in any of them, and the stored job *return* holds the rows only. Do not put secrets in `sql` or `params`.

## Security

If you discover a security vulnerability, please refer
to [Salt's security guide][security].

## User Documentation

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for install and usage, and `SPEC.md` for the full contract.

## Contributing

The saltext-sqlx project welcomes contributions from anyone!

The [Salt Extensions guide][salt-extensions-guide] provides comprehensive instructions on all aspects
of Salt extension development, including [writing tests][writing-tests], [running tests][running-tests],
[writing documentation][writing-docs] and [rendering the docs][rendering-docs].

### Quickstart

To get started contributing, first clone this repository (or your fork):

```bash
# Clone the repo
git clone --origin upstream git@github.com:yogzblr/saltext-sqlx.git

# Change to the repo dir
cd saltext-sqlx
```

#### Automatic
If you have installed [direnv][direnv], copying the included `.envrc.example` to `.envrc` and
allowing it to run ensures a proper development environment is present and the virtual environment is active.

Without `direnv`, you can still run the automation explicitly:

```bash
make dev  # or python3 tools/initialize.py
source .venv/bin/activate
```

#### Manual
Please follow the [first steps][first-steps], skipping the repository initialization and first commit.

### Pull request

Always make changes in a feature branch:

```bash
git switch -c my-feature-branch
```

Please ensure you include a [news fragment](https://salt-extensions.github.io/salt-extension-copier/topics/documenting/changelog.html#procedure)
describing your changes. This is a requirement for all user-facing changes (bug fixes, new features),
with the exception of documentation changes.

To [submit a Pull Request][submitting-pr], you'll need a fork of this repository in
your own GitHub account. If you followed the instructions above,
set your fork as the `origin` remote now:

```bash
git remote add origin git@github.com:<your_fork>.git
```

Ensure you followed the [first steps][first-steps] and commit your changes, fixing any
failing `pre-commit` hooks. Then push the feature branch to your fork and submit a PR.

### Ways to contribute

Contributions come in many forms, and they’re all valuable! Here are some ways you can help
without writing code:

* **Documentation**: Especially examples showing how to use this project
  to solve specific problems.
* **Triaging issues**: Help manage [issues][issues] and participate in [discussions][discussions].
* **Reviewing [Pull Requests][PRs]**: We especially appreciate reviews using [Conventional Comments][comments].

You can also contribute by:

* Writing blog posts
* Sharing your experiences using Salt + SQL databases
  on social media
* Giving talks at conferences
* Publishing videos
* Engaging in IRC, Discord or email groups

Any of these things are super valuable to our community, and we sincerely
appreciate every contribution!

[security]: https://github.com/saltstack/salt/blob/master/SECURITY.md
[salt-extensions-guide]: https://salt-extensions.github.io/salt-extension-copier/
[writing-tests]: https://salt-extensions.github.io/salt-extension-copier/topics/testing/writing.html
[running-tests]: https://salt-extensions.github.io/salt-extension-copier/topics/testing/running.html
[writing-docs]: https://salt-extensions.github.io/salt-extension-copier/topics/documenting/writing.html
[rendering-docs]: https://salt-extensions.github.io/salt-extension-copier/topics/documenting/building.html
[first-steps]: https://salt-extensions.github.io/salt-extension-copier/topics/creation.html#initialize-the-python-virtual-environment
[submitting-pr]: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork
[direnv]: https://direnv.net
[issues]: https://github.com/yogzblr/saltext-sqlx/issues
[PRs]: https://github.com/yogzblr/saltext-sqlx/pulls
[discussions]: https://github.com/yogzblr/saltext-sqlx/discussions
[comments]: https://conventionalcomments.org/
