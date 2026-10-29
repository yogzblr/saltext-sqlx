#!/usr/bin/env bash
# Start PostgreSQL (UTF8), MySQL 8 and MariaDB 10.11 natively, seed them, create accounts ro/rw.
# Usage: native/up.sh [postgresql] [mysql] [mariadb]   (default: all three). Idempotent. Needs root (uses runuser sqlxdb).
set -euo pipefail
. "$(dirname "$0")/../env.sh"
here="$(cd "$(dirname "$0")/.." && pwd)"
targets=("$@"); [ ${#targets[@]} -eq 0 ] && targets=(postgresql mysql mariadb)
R="$SQLX_DB_ROOT"; mkdir -p "$R"; chown sqlxdb "$R"
as_db() { runuser -u sqlxdb -- "$@"; }
PGBIN=/usr/lib/postgresql/16/bin
MYB="$SQLX_OPT/mysql8/usr"; MAB="$SQLX_OPT/mariadb/usr"

wait_for() { for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done; echo "timeout waiting: $*" >&2; return 1; }

up_postgresql() {
  local d="$R/pg/data"
  if [ ! -d "$d" ]; then
    mkdir -p "$R/pg"; chown sqlxdb "$R/pg"
    echo "$SQLX_ADMIN_PW" > "$R/pg.pw"; chown sqlxdb "$R/pg.pw"
    # -E UTF8 and --locale=C.UTF-8: a SQL_ASCII/C-locale database makes psycopg 3 return bytes (see BUILD_PROMPT mistakes)
    as_db "$PGBIN/initdb" -D "$d" -E UTF8 --locale=C.UTF-8 -U postgres --auth-local=trust --auth-host=scram-sha-256 --pwfile="$R/pg.pw" >/dev/null
  fi
  as_db "$PGBIN/pg_ctl" -D "$d" status >/dev/null 2>&1 || \
    as_db "$PGBIN/pg_ctl" -D "$d" -l "$R/pg/server.log" -w -o "-p $SQLX_PG_PORT -c timezone=UTC -c log_timezone=UTC -c listen_addresses=127.0.0.1 -c unix_socket_directories=$R/pg" start >/dev/null
  local psql=("$PGBIN/psql" -h 127.0.0.1 -p "$SQLX_PG_PORT" -U postgres -v ON_ERROR_STOP=1 -X -q)
  if ! PGPASSWORD="$SQLX_ADMIN_PW" "${psql[@]}" -tAc "select 1 from pg_database where datname='sqlx_test'" | grep -q 1; then
    PGPASSWORD="$SQLX_ADMIN_PW" "${psql[@]}" -c "create database sqlx_test encoding 'UTF8' lc_collate 'C.UTF-8' lc_ctype 'C.UTF-8' template template0"
    PGPASSWORD="$SQLX_ADMIN_PW" "${psql[@]}" -d sqlx_test -f "$here/seed/postgresql.sql"
  fi
  echo "postgresql up on $SQLX_PG_PORT"
}

up_mysql_like() {  # name basedir daemon client port seed
  local name=$1 base=$2 daemon=$3 client=$4 port=$5 d="$R/$1" extra=()
  if [ ! -d "$d/data" ]; then
    mkdir -p "$d"; chown sqlxdb "$d"
    if [ "$name" = mysql8 ]; then
      as_db "$base/sbin/mysqld" --no-defaults --initialize-insecure --datadir="$d/data" --basedir="$base" --lc-messages-dir="$base/share/mysql" >/dev/null 2>&1
    else
      as_db "$base/bin/mariadb-install-db" --no-defaults --datadir="$d/data" --basedir="$base" --auth-root-authentication-method=normal >/dev/null 2>&1
    fi
  fi
  [ "$name" = mysql8 ] && extra=(--mysqlx=OFF --secure-file-priv=NULL)
  if ! "$client" --no-defaults -uroot -h127.0.0.1 -P"$port" -e 'select 1' >/dev/null 2>&1 && \
     ! "$client" --no-defaults -uroot -p"$SQLX_ADMIN_PW" -h127.0.0.1 -P"$port" -e 'select 1' >/dev/null 2>&1; then
    cd /
    as_db "$base/sbin/$daemon" --no-defaults --datadir="$d/data" --basedir="$base" --lc-messages-dir="$base/share/mysql" \
      --port="$port" --bind-address=127.0.0.1 --socket="$d/sock" --pid-file="$d/pid" --character-set-server=utf8mb4 "${extra[@]}" \
      >"$d/server.log" 2>&1 </dev/null &
    wait_for bash -c "'$client' --no-defaults -uroot -h127.0.0.1 -P$port -e 'select 1' || '$client' --no-defaults -uroot -p'$SQLX_ADMIN_PW' -h127.0.0.1 -P$port -e 'select 1'"
  fi
  # first start: root has no password; set one, then seed. Later starts: root already has it.
  if "$client" --no-defaults -uroot -h127.0.0.1 -P"$port" -e 'select 1' >/dev/null 2>&1; then
    if [ "$name" = mysql8 ]; then
      "$client" --no-defaults -uroot -h127.0.0.1 -P"$port" -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$SQLX_ADMIN_PW'; CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY '$SQLX_ADMIN_PW'; GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION;"
    else
      "$client" --no-defaults -uroot -h127.0.0.1 -P"$port" -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$SQLX_ADMIN_PW'; CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY '$SQLX_ADMIN_PW'; GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION;"
    fi
  fi
  # mariadb-install-db creates anonymous ''@localhost users that shadow 'ro'@'%' for TCP connections from 127.0.0.1
  "$client" --no-defaults -uroot -p"$SQLX_ADMIN_PW" -h127.0.0.1 -P"$port" -e "DROP USER IF EXISTS ''@'localhost'; DROP USER IF EXISTS ''@'$(hostname)'" 2>/dev/null || true
  if ! "$client" --no-defaults -uroot -p"$SQLX_ADMIN_PW" -h127.0.0.1 -P"$port" -N -e "show databases like 'scratch'" | grep -q scratch; then
    "$client" --no-defaults -uroot -p"$SQLX_ADMIN_PW" -h127.0.0.1 -P"$port" --default-character-set=utf8mb4 < "$here/seed/mysql.sql"
  fi
  echo "$name up on $port"
}

for t in "${targets[@]}"; do
  case "$t" in
    postgresql) up_postgresql;;
    mysql)   up_mysql_like mysql8  "$MYB" mysqld  "$MYB/bin/mysql"   "$SQLX_MYSQL_PORT";;
    mariadb) up_mysql_like mariadb "$MAB" mariadbd "$MAB/bin/mariadb" "$SQLX_MARIADB_PORT";;
    oracle|mssql) echo "$t: no native package; use docker compose (tests-support/up.sh)" >&2; exit 2;;
    *) echo "unknown target $t" >&2; exit 2;;
  esac
done
