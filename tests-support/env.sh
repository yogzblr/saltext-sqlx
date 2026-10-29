# Source this file. Ports/credentials shared by docker-compose.yml and native/*.sh.
export SQLX_PG_PORT=54329 SQLX_MYSQL_PORT=33061 SQLX_MARIADB_PORT=33062 SQLX_ORACLE_PORT=15211 SQLX_MSSQL_PORT=14331
export SQLX_ADMIN_PW='admin_pw'          # PostgreSQL postgres / MySQL root / MariaDB root
export SQLX_MSSQL_SA_PW='Sa_pw_12345!'   # SQL Server complexity rules apply
export SQLX_ORACLE_SYS_PW='admin_pw'
export SQLX_DB_ROOT="${SQLX_DB_ROOT:-/var/tmp/sqlx-testdbs}"   # data directories (native mode)
export SQLX_OPT="${SQLX_OPT:-/opt}"                            # extracted binaries (native mode)

# Probe URLs (SQLAlchemy). Replace <acct> by ro or rw; passwords are <acct>_pw (MSSQL: Ro_pw_12345! / Rw_pw_12345!).
sqlx_url() {  # usage: sqlx_url <dialect> <ro|rw>
  local d="$1" a="$2" pw
  case "$d:$a" in mssql:ro) pw='Ro_pw_12345!';; mssql:rw) pw='Rw_pw_12345!';; *) pw="${a}_pw";; esac
  case "$d" in
    postgresql) echo "postgresql+psycopg://$a:$pw@127.0.0.1:$SQLX_PG_PORT/sqlx_test";;
    mysql)      echo "mysql+pymysql://$a:$pw@127.0.0.1:$SQLX_MYSQL_PORT/scratch";;
    mariadb)    echo "mysql+pymysql://$a:$pw@127.0.0.1:$SQLX_MARIADB_PORT/scratch";;
    oracle)     echo "oracle+oracledb://$a:$pw@127.0.0.1:$SQLX_ORACLE_PORT/?service_name=FREEPDB1";;
    mssql)      echo "mssql+pymssql://$a:$pw@127.0.0.1:$SQLX_MSSQL_PORT/sqlx_test";;
  esac
}
