#!/usr/bin/env bash
# Create the e2e accounts (ro, rw) and probe tables in one database container, once. Called by up.sh after the
# container is healthy. Uses the image's own admin account (`sakila`, or SYSDBA on Oracle) for setup only.
# Usage: setup.sh <service>
set -euo pipefail
svc="$1"
here="$(cd "$(dirname "$0")" && pwd)"
dc() { docker compose -f "$here/../docker-compose.yml" "$@"; }
sql="$here/$svc.sql"
case "$svc" in
  pg)
    if dc exec -T pg psql -U sakila -d sakila -tAc "select 1 from pg_roles where rolname='ro'" | grep -q 1; then echo "pg: already set up"; exit 0; fi
    dc exec -T pg psql -U sakila -d sakila -v ON_ERROR_STOP=1 -q <"$sql" ;;
  mysql|mariadb)
    client=mysql; [ "$svc" = mariadb ] && client=mariadb
    if dc exec -T "$svc" "$client" -usakila -pp_ssW0rd -N -e "select count(*) from mysql.user where user='ro'" 2>/dev/null | grep -q 1; then echo "$svc: already set up"; exit 0; fi
    dc exec -T "$svc" "$client" --default-character-set=utf8mb4 -usakila -pp_ssW0rd <"$here/mysql.sql" ;;
  mssql)
    q=(/opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sakila -P p_ssW0rd)
    if dc exec -T mssql "${q[@]}" -h -1 -Q "set nocount on; select count(*) from sys.server_principals where name='ro'" | grep -q 1; then echo "mssql: already set up"; exit 0; fi
    # sqlcmd reads the file as UTF-8 with -f 65001 (the probe row has non-ASCII text)
    dc exec -T mssql bash -c "cat > /tmp/e2e-setup.sql" <"$sql"
    dc exec -T mssql "${q[@]}" -f 65001 -i /tmp/e2e-setup.sql ;;
  oracle)
    if dc exec -T oracle bash -c "printf '%s\\n' 'alter session set container=SAKILA;' \"select 'HAVE_RO=' || count(*) from dba_users where username='RO';\" | sqlplus -s / as sysdba" | grep -q 'HAVE_RO=1'; then echo "oracle: already set up"; exit 0; fi
    dc exec -T oracle bash -c "cat > /tmp/e2e-setup.sql" <"$sql"
    dc exec -T oracle bash -c "NLS_LANG=AMERICAN_AMERICA.AL32UTF8 sqlplus -s / as sysdba @/tmp/e2e-setup.sql" ;;
  pg-tls)
    : ;;  # roles come from tls/pg-init.sql on first start
  *) echo "setup.sh: nothing to do for $svc" ;;
esac
echo "$svc: set up"
