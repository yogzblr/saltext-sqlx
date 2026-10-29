#!/usr/bin/env bash
# Start THREE separate TLS-enabled test servers (PostgreSQL 54339, MySQL 33071, MariaDB 33072), data under
# $SQLX_TLS_ROOT (default /var/tmp/sqlx-tls-dbs). They never touch the standard servers of tests-support/native.
# Needs root (runuser sqlxdb) and the binaries installed by tests-support/native/install.sh.
# Accounts (database "tlsdb"):  tls_pw / tls_pw_pw  password over TLS only;  tls_cert  certificate only (CN=tls_cert).
set -euo pipefail
. "$(dirname "$0")/../env.sh"
here="$(cd "$(dirname "$0")" && pwd)"
export SQLX_TLS_ROOT="${SQLX_TLS_ROOT:-/var/tmp/sqlx-tls-dbs}"
export SQLX_TLS_PG_PORT=54339 SQLX_TLS_MYSQL_PORT=33071 SQLX_TLS_MARIADB_PORT=33072
R="$SQLX_TLS_ROOT"; PKI="$R/pki"
mkdir -p "$R"; chown sqlxdb "$R"; chmod 755 "$R"
[ -f "$PKI/server/server.pem" ] || bash "$here/gen_certs.sh" "$PKI"
as_db() { runuser -u sqlxdb -- "$@"; }
PGBIN=/usr/lib/postgresql/16/bin
MYB="$SQLX_OPT/mysql8/usr"; MAB="$SQLX_OPT/mariadb/usr"
PW="$SQLX_ADMIN_PW"
wait_for() { for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done; echo "timeout waiting: $*" >&2; return 1; }

up_pg() {
  local d="$R/pg/data"
  if [ ! -d "$d" ]; then
    mkdir -p "$R/pg"; chown sqlxdb "$R/pg"; echo "$PW" > "$R/pg.pw"; chown sqlxdb "$R/pg.pw"
    as_db "$PGBIN/initdb" -D "$d" -E UTF8 --locale=C.UTF-8 -U postgres --auth-local=trust --auth-host=scram-sha-256 --pwfile="$R/pg.pw" >/dev/null
    cat >> "$d/postgresql.conf" <<CONF
ssl = on
ssl_cert_file = '$PKI/server/server.pem'
ssl_key_file = '$PKI/server/server.key'
ssl_ca_file = '$PKI/server/ca.pem'
CONF
    # first match wins: plain TCP is rejected; tls_cert must present a client certificate; tls_pw uses a password
    cat > "$d/pg_hba.conf" <<CONF
local   all  all                         trust
hostnossl all all 0.0.0.0/0              reject
hostssl tlsdb tls_cert 0.0.0.0/0         cert clientcert=verify-full
hostssl tlsdb tls_mismatch 0.0.0.0/0     cert clientcert=verify-full
hostssl tlsdb tls_pw   0.0.0.0/0         scram-sha-256
hostssl all   postgres 0.0.0.0/0         scram-sha-256
CONF
  fi
  as_db "$PGBIN/pg_ctl" -D "$d" status >/dev/null 2>&1 || \
    as_db "$PGBIN/pg_ctl" -D "$d" -l "$R/pg/server.log" -w -o "-p $SQLX_TLS_PG_PORT -c timezone=UTC -c log_timezone=UTC -c listen_addresses=0.0.0.0 -c unix_socket_directories=$R/pg" start >/dev/null
  local psql=("$PGBIN/psql" -h "$R/pg" -p "$SQLX_TLS_PG_PORT" -U postgres -v ON_ERROR_STOP=1 -X -q)
  if ! "${psql[@]}" -tAc "select 1 from pg_database where datname='tlsdb'" | grep -q 1; then
    "${psql[@]}" -c "create database tlsdb encoding 'UTF8' lc_collate 'C.UTF-8' lc_ctype 'C.UTF-8' template template0"
    "${psql[@]}" -c "create role tls_pw login password 'tls_pw_pw'" -c "create role tls_cert login" -c "create role tls_mismatch login"
    "${psql[@]}" -d tlsdb -c "grant connect on database tlsdb to tls_pw, tls_cert, tls_mismatch"
  fi
  echo "postgresql (tls) up on $SQLX_TLS_PG_PORT"
}

up_my() {  # name basedir daemon client port
  local name=$1 base=$2 daemon=$3 client=$4 port=$5 d="$R/$1" extra=()
  local cl=("$client" --no-defaults -uroot -S "$d/sock")
  if [ ! -d "$d/data" ]; then
    mkdir -p "$d"; chown sqlxdb "$d"
    if [ "$name" = mysql_tls ]; then
      as_db "$base/sbin/mysqld" --no-defaults --initialize-insecure --datadir="$d/data" --basedir="$base" --lc-messages-dir="$base/share/mysql" >/dev/null 2>&1
    else
      as_db "$base/bin/mariadb-install-db" --no-defaults --datadir="$d/data" --basedir="$base" --auth-root-authentication-method=normal >/dev/null 2>&1
    fi
  fi
  [ "$name" = mysql_tls ] && extra=(--mysqlx=OFF --secure-file-priv=NULL --require-secure-transport=ON)
  if ! "${cl[@]}" -e 'select 1' >/dev/null 2>&1; then
    cd /
    as_db "$base/sbin/$daemon" --no-defaults --datadir="$d/data" --basedir="$base" --lc-messages-dir="$base/share/mysql" \
      --port="$port" --bind-address=0.0.0.0 --socket="$d/sock" --pid-file="$d/pid" --character-set-server=utf8mb4 \
      --ssl-ca="$PKI/server/ca.pem" --ssl-cert="$PKI/server/server.pem" --ssl-key="$PKI/server/server.key" "${extra[@]}" \
      >"$d/server.log" 2>&1 </dev/null &
    wait_for "${cl[@]}" -e 'select 1'
  fi
  if ! "${cl[@]}" -N -e "show databases like 'tlsdb'" | grep -q tlsdb; then
    "${cl[@]}" -e "CREATE DATABASE tlsdb;
      CREATE USER 'tls_pw'@'%' IDENTIFIED BY 'tls_pw_pw' REQUIRE SSL;
      CREATE USER 'tls_cert'@'%' IDENTIFIED BY 'tls_cert_pw' REQUIRE X509;
      GRANT ALL ON tlsdb.* TO 'tls_pw'@'%', 'tls_cert'@'%';
      DROP USER IF EXISTS ''@'localhost'; DROP USER IF EXISTS ''@'$(hostname)';"
  fi
  echo "$name (tls) up on $port"
}

up_pg
up_my mysql_tls   "$MYB" mysqld  "$MYB/bin/mysql"   "$SQLX_TLS_MYSQL_PORT"
up_my mariadb_tls "$MAB" mariadbd "$MAB/bin/mariadb" "$SQLX_TLS_MARIADB_PORT"
