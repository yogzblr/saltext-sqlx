#!/usr/bin/env bash
# One command to start every runnable test database.
#   tests-support/up.sh            auto: Docker if the daemon answers, else native (PostgreSQL, MySQL, MariaDB only)
#   tests-support/up.sh native     force native;  tests-support/up.sh docker  force Docker (all five)
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-auto}"
if [ "$mode" = auto ]; then
  if docker info >/dev/null 2>&1; then mode=docker; else mode=native; fi
fi
case "$mode" in
  docker) exec docker compose -f "$here/docker-compose.yml" up -d --wait;;
  native)
    [ -x /opt/mysql8/usr/sbin/mysqld ] || "$here/native/install.sh"
    "$here/native/up.sh"; rc=$?
    echo "NOT RUN (no native package, Docker unavailable): oracle, mssql" >&2
    exit $rc;;
  *) echo "usage: $0 [auto|docker|native]" >&2; exit 2;;
esac
