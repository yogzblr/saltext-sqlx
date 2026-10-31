-- run by the official image's entrypoint on first start (POSTGRES_PASSWORD is the admin)
CREATE DATABASE tlsdb ENCODING 'UTF8' TEMPLATE template0;
CREATE ROLE tls_pw LOGIN PASSWORD 'TlsE2e_Secret_5523';
CREATE ROLE tls_cert LOGIN;
GRANT CONNECT ON DATABASE tlsdb TO tls_pw, tls_cert;
