-- MySQL 8 and MariaDB. Run as root. Database "scratch" is the scratch schema.
CREATE DATABASE IF NOT EXISTS scratch CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS private CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'ro'@'%' IDENTIFIED BY 'ro_pw';
CREATE USER IF NOT EXISTS 'rw'@'%' IDENTIFIED BY 'rw_pw';

CREATE TABLE scratch.orders (
  id         int PRIMARY KEY,
  note       text,
  amount     decimal(10,2),
  created_at datetime(3),
  ref        char(36),
  ref_bin    binary(16),
  payload    varbinary(64),
  doc        json,
  nothing    text
) CHARACTER SET utf8mb4;
INSERT INTO scratch.orders VALUES
  (1, CONCAT('caf', CHAR(0xC3A9 USING utf8mb4), ' ', CHAR(0xE282AC USING utf8mb4), ' ', CHAR(0xE697A5 USING utf8mb4)), 12.50, '2026-10-29 12:34:56.789',
   '123e4567-e89b-12d3-a456-426614174000', UNHEX('123E4567E89B12D3A456426614174000'), UNHEX('DEADBEEF'), '{"a": 1, "b": [1, 2]}', NULL),
  (2, 'plain', 0.00, NULL, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE scratch.uniq (email varchar(100) PRIMARY KEY);
INSERT INTO scratch.uniq VALUES ('a@example.com');
CREATE TABLE scratch.t_probe (id int);
CREATE TABLE private.secrets (v text);
INSERT INTO private.secrets VALUES ('hidden');

GRANT SELECT ON scratch.* TO 'ro'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.* TO 'rw'@'%';
FLUSH PRIVILEGES;
