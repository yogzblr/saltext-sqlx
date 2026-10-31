#!/usr/bin/env bash
# Throw-away PKI for the end-to-end TLS servers. Reuses tests-support/tls/gen_certs.sh (unchanged) and adds
# e2e-server/: a server certificate signed by the same CA whose SANs are the compose service names, so verify-full
# works from the minion. The *-alias network names are deliberately NOT in the SANs (hostname-mismatch case).
# Usage: mk_certs.sh <outdir>
set -euo pipefail
out="$1"
here="$(cd "$(dirname "$0")" && pwd)"
SQLX_TLS_SERVER_USER=nobody-sqlx-e2e bash "$here/../../tests-support/tls/gen_certs.sh" "$out" >/dev/null
cd "$out"
umask 077
mkdir -p e2e-server
openssl genrsa -out e2e-server/server.key 2048 2>/dev/null
openssl req -new -key e2e-server/server.key -subj "/CN=sqlx-e2e-server" -out e2e-server/server.csr
printf '%s\n' 'basicConstraints=CA:FALSE' 'keyUsage=digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' \
  'subjectAltName=DNS:pg-tls,DNS:mysql-tls,DNS:mariadb-tls,DNS:localhost,IP:127.0.0.1' > e2e-server/server.ext
openssl x509 -req -in e2e-server/server.csr -CA ca/ca.pem -CAkey ca/ca.key -CAcreateserial -days 3650 -sha256 \
  -extfile e2e-server/server.ext -out e2e-server/server.pem 2>/dev/null
cp ca/ca.pem e2e-server/ca.pem
rm -f e2e-server/server.csr e2e-server/server.ext ca/ca.srl
chmod 600 e2e-server/server.key; chmod 644 e2e-server/server.pem e2e-server/ca.pem; chmod 755 e2e-server
echo "PKI written to $out"
