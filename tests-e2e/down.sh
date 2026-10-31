#!/usr/bin/env bash
# Stop and remove every container, the network and the named volumes of the end-to-end stack.
# The PKI in $SQLX_E2E_DATA stays (delete it by hand: it is throw-away).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export SQLX_E2E_DATA="${SQLX_E2E_DATA:-$HOME/e2e-data}"
docker compose -f "$here/docker-compose.yml" down -v --remove-orphans
