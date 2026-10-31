-- run by the official MySQL / MariaDB entrypoint on first start (over the local socket)
CREATE DATABASE IF NOT EXISTS tlsdb;
CREATE USER 'tls_pw'@'%' IDENTIFIED BY 'TlsE2e_Secret_5523' REQUIRE SSL;
CREATE USER 'tls_cert'@'%' IDENTIFIED BY 'TlsCertE2e_Secret_6634' REQUIRE X509;
GRANT SELECT ON tlsdb.* TO 'tls_pw'@'%', 'tls_cert'@'%';
