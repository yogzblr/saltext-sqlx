# End-to-end suite (Docker Salt master, minions, salt-api; five Sakila databases)

Drives `saltext.sqlx` the way an operator does: `salt <minion> sqlx.*` on the master, `salt-call` on the minion and HTTP
calls to salt-api, against PostgreSQL, MySQL, MariaDB, Oracle and SQL Server preloaded with the same Sakila data.

```bash
tests-e2e/up.sh                      # PKI, databases one by one (waited for), accounts, Salt master + 2 minions
python -m pytest tests-e2e -m e2e    # from the repository root, in the dev virtualenv (needs only pytest)
tests-e2e/down.sh                    # stop everything and remove the volumes
```

`up.sh pg mysql` starts a subset; `up.sh salt` only the Salt part. Logs go to `$SQLX_E2E_LOGS` (default `~/e2e-logs`),
the throw-away PKI to `$SQLX_E2E_DATA` (default `~/e2e-data`; it must be on the Linux filesystem, never `/mnt/c`).
After a code change: `docker compose -f tests-e2e/docker-compose.yml restart minion minion-policy` (the entrypoint
reinstalls the extension from the read-only bind mount of the repository).

## What runs

| Service | Image | Notes |
|---|---|---|
| `pg` | `ghcr.io/sakiladb/postgres:18` | PostgreSQL 18.4, database `sakila` (UTF8), schema `public`, published 127.0.0.1:25432 |
| `mysql` | `ghcr.io/sakiladb/mysql:9` | MySQL 9.7.1, database `sakila`, 127.0.0.1:23306 |
| `mariadb` | `ghcr.io/sakiladb/mariadb:12` | MariaDB 12.3.2, database `sakila`, 127.0.0.1:23307 |
| `oracle` | `ghcr.io/sakiladb/oracle:23` | Oracle AI Database 26ai Free 23.26.2, PDB/service `SAKILA`, 127.0.0.1:21521; healthy after about 3 minutes |
| `mssql` | `ghcr.io/sakiladb/sqlserver:2022` | SQL Server 2022 CU25 (16.0.4255.1), amd64 only, database `sakila`, schema `dbo`, 127.0.0.1:21433. The image sets `ACCEPT_EULA=Y` itself (Developer edition) |
| `pg-tls`, `mysql-tls`, `mariadb-tls` | `postgres:16`, `mysql:8.0`, `mariadb:10.11` | TLS-only servers (the Sakila images have no TLS); certificate SANs are the service names, the `*-tls-alias` network names are not in them |
| `master` | `sqlx-e2e-salt:3008.5` (built from `salt/Dockerfile`) | salt-master + salt-api (rest_cherrypy, plain HTTP, 127.0.0.1:8000), `file` eauth |
| `minion` | same | profiles of `salt/minion.d/10-sqlx.conf`, no call-time policy |
| `minion-policy` | same | the same profiles plus `salt/minion-policy.d/20-policy.conf` (call-time parameters for a host list) |

There is no official `saltproject/salt` image (the Docker Hub repository does not exist, checked 2026-10-31), so the image
is built from the Salt Project onedir tarball 3008.5. **Its Python is 3.14.7**, not 3.11; every driver wheel
(psycopg-binary 3.3.6, PyMySQL 1.2.3, oracledb 26.1.0, pymssql 2.4.6, SQLAlchemy 2.0.55) installs from PyPI for it.

## Accounts (created by `db/setup.sh` with the image's own admin account, which is used for nothing else)

| Account | Rights |
|---|---|
| `ro` | SELECT on the Sakila schema only (Oracle: plus private synonyms so that `film` resolves) |
| `rw` | SELECT/INSERT/UPDATE/DELETE on `scratch.e2e_notes` and on the Sakila `rental` table |
| `tls_pw`, `tls_cert` | TLS servers only: password over TLS, client certificate (`CN=tls_cert`) |

Passwords are `sdb://e2e_sdb/<key>` references into `salt/e2e-secrets.yaml` (the `yaml` sdb driver). The eauth users of
`salt/e2e-eauth.txt`: `sre_ops` (`sqlx.query` and `sqlx.ping` on `^(pg|mysql|mariadb|oracle|mssql)_ro$`), `dba_writers`
(`sqlx.execute` on `..._rw$`) and `e2e_full` (every `sqlx.*` function; used by the harness to reach all functions through
salt-api, not an example). All of these are throw-away values.

`type_probe` (one populated row with non-ASCII text, one all-NULL row), closest type per database:

| Column | PostgreSQL | MySQL / MariaDB | Oracle | SQL Server |
|---|---|---|---|---|
| `t_text` | `text` | `varchar(100)` utf8mb4 | `NVARCHAR2(100)` | `nvarchar(100)` |
| `t_num` | `numeric(10,2)` | `decimal(10,2)` | `NUMBER(10,2)` | `decimal(10,2)` |
| `t_ts` | `timestamp(6)` | `datetime(6)` | `TIMESTAMP(6)` | `datetime2(6)` |
| `t_uuid` | `uuid` | `char(36)` | `RAW(16)` | `uniqueidentifier` |
| `t_bin` | `bytea` | `varbinary(16)` | `BLOB` | `varbinary(16)` |
| `t_json` | `jsonb` | `json` (MariaDB: `LONGTEXT` + check) | `JSON` (23ai native) | `nvarchar(max)` (no JSON type before 2025) |
| `t_null` | `text` | `varchar(10)` | `VARCHAR2(10)` | `nvarchar(10)` |

## Files

- `probe_wire.py`: records raw driver error shapes for Oracle and SQL Server (evidence in `docs/wire_shapes/t1b/`).
- `conftest.py`: the three call paths and the event-bus capture; `dbs.py`: per-database SQL and expected texts.
- `test_e2e_*.py`: the matrix; `test_zz_leaks.py` runs last and greps the job cache, logs, event bus and every output.
