#!/usr/bin/env bash
# Copy the server certificate and key from the read-only mount into /certs owned by the database user with mode 600
# (PostgreSQL refuses a key that is group/world readable or owned by another user), then run the image's entrypoint.
# Usage (compose entrypoint): tls-entry.sh <db-user> <command...>
set -euo pipefail
user="$1"; shift
mkdir -p /certs
cp /certs-src/server.pem /certs-src/server.key /certs-src/ca.pem /certs/
chown -R "$user" /certs
chmod 600 /certs/server.key
chmod 644 /certs/server.pem /certs/ca.pem
exec docker-entrypoint.sh "$@"
