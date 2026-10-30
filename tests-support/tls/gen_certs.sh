#!/usr/bin/env bash
# Throwaway PKI for the TLS functional tests. NEVER use these keys for anything real.
# Usage: gen_certs.sh [outdir]      (default /var/tmp/sqlx-tls-dbs/pki; recreated from scratch)
#
# outdir/
#   ca/ other-ca/        CA certificate + key (other-ca is unrelated to ca)
#   server/              server.pem server.key ca.pem  (SAN DNS:localhost, IP:127.0.0.1, signed by ca)
#   expired-server/      same names, server certificate already expired
#   server-good/         copy of server/ (swap_server_cert.sh puts it back)
#   good/                profile directories (ca.pem, client.pem, client.key), client CN=tls_cert, signed by ca
#   ca-only/             ca.pem only (TLS without a client certificate)
#   wrong-ca/            ca.pem of other-ca (the server's CA is unknown to the client) + a valid client pair
#   other-client/        ca.pem of ca, client pair signed by other-ca (the server must refuse it)
#   expired-client/      ca.pem of ca, client pair signed by ca but expired
#   alt-names/           ca.crt client.crt client.key (fallback file names)
# Every private key is mode 600. server/ and expired-server/ are chowned to $SQLX_TLS_SERVER_USER (default sqlxdb)
# when that user exists, because the database servers run as it.
set -euo pipefail
out="${1:-/var/tmp/sqlx-tls-dbs/pki}"
srv_user="${SQLX_TLS_SERVER_USER:-sqlxdb}"
rm -rf "$out"; mkdir -p "$out"; chmod 755 "$out"
umask 077
cd "$out"

mkca() {  # name cn
  mkdir -p "$1"
  openssl genrsa -out "$1/ca.key" 2048 2>/dev/null
  openssl req -x509 -new -key "$1/ca.key" -sha256 -days 3650 -subj "/CN=$2" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$1/ca.pem"
}

# Minimal `openssl ca` setup, needed only to be able to issue an already-expired certificate.
mkdb() {  # caname
  local d="$1/db"; mkdir -p "$d/new"; : > "$d/index.txt"; echo 1000 > "$d/serial"
  cat > "$d/ca.cnf" <<CNF
[ca]
default_ca = c
[c]
dir = $out/$1/db
database = \$dir/index.txt
new_certs_dir = \$dir/new
serial = \$dir/serial
default_md = sha256
policy = p
unique_subject = no
copy_extensions = none
[p]
commonName = supplied
CNF
}

# issue <caname> <outprefix> <cn> <ext> <startdate> <enddate>   (key + cert written to <outprefix>.key/.pem)
issue() {
  local ca="$1" prefix="$2" cn="$3" ext="$4" start="$5" end="$6"
  openssl genrsa -out "$prefix.key" 2048 2>/dev/null
  openssl req -new -key "$prefix.key" -subj "/CN=$cn" -out "$prefix.csr"
  printf '%s\n' "$ext" > "$prefix.ext"
  openssl ca -batch -config "$out/$ca/db/ca.cnf" -cert "$out/$ca/ca.pem" -keyfile "$out/$ca/ca.key" \
    -in "$prefix.csr" -out "$prefix.pem" -notext -extfile "$prefix.ext" -startdate "$start" -enddate "$end" 2>/dev/null
  rm -f "$prefix.csr" "$prefix.ext"
}

now=$(date -u -d '-1 day' +%Y%m%d%H%M%SZ); later=$(date -u -d '+10 years' +%Y%m%d%H%M%SZ)
old_start=20200101000000Z; old_end=20200102000000Z
SRV_EXT=$'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1'
CLI_EXT=$'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth'

mkca ca "sqlx-test-ca"; mkca other-ca "sqlx-test-other-ca"; mkdb ca; mkdb other-ca

mkdir server expired-server
issue ca server/server localhost "$SRV_EXT" "$now" "$later"; cp ca/ca.pem server/ca.pem
issue ca expired-server/server localhost "$SRV_EXT" "$old_start" "$old_end"; cp ca/ca.pem expired-server/ca.pem

mkdir good ca-only wrong-ca other-client expired-client alt-names
issue ca good/client tls_cert "$CLI_EXT" "$now" "$later"; cp ca/ca.pem good/ca.pem
cp ca/ca.pem ca-only/ca.pem
cp other-ca/ca.pem wrong-ca/ca.pem; cp good/client.pem good/client.key wrong-ca/
cp ca/ca.pem other-client/ca.pem; issue other-ca other-client/client tls_cert "$CLI_EXT" "$now" "$later"
cp ca/ca.pem expired-client/ca.pem; issue ca expired-client/client tls_cert "$CLI_EXT" "$old_start" "$old_end"
cp ca/ca.pem alt-names/ca.crt; cp good/client.pem alt-names/client.crt; cp good/client.key alt-names/client.key

rm -rf ca/db other-ca/db
cp -a server server-good   # swap_server_cert.sh restores from it
find . -name '*.key' -exec chmod 600 {} +
find . -name '*.pem' -o -name '*.crt' | xargs chmod 644
find . -type d -exec chmod 755 {} +
if id "$srv_user" >/dev/null 2>&1; then
  chown -R "$srv_user" server expired-server server-good
fi
echo "PKI written to $out"
