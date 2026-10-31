-- MySQL / MariaDB (sakiladb/mysql, sakiladb/mariadb): accounts and probe tables. Run once as `sakila`.
-- ro: SELECT on the sakila database only. rw: DML on scratch.e2e_notes and on sakila.rental.
CREATE USER 'ro'@'%' IDENTIFIED BY 'RoE2e_Secret_7731';
CREATE USER 'rw'@'%' IDENTIFIED BY 'RwE2e_Secret_4412';

-- type_probe: closest MySQL/MariaDB type (no uuid type: CHAR(36); JSON is LONGTEXT + check on MariaDB)
CREATE TABLE sakila.type_probe (
  id      int PRIMARY KEY,
  t_text  varchar(100) CHARACTER SET utf8mb4,
  t_num   decimal(10,2),
  t_ts    datetime(6),
  t_uuid  char(36),
  t_bin   varbinary(16),
  t_json  json,
  t_null  varchar(10)
);
INSERT INTO sakila.type_probe VALUES
  (1, 'Grüße ✓ naïve', 12.50, '2026-10-31 12:34:56.789012', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 0xdeadbeef, '{"a": 1, "b": [true, null]}', NULL),
  (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
GRANT SELECT ON sakila.* TO 'ro'@'%';

CREATE DATABASE scratch;
CREATE TABLE scratch.e2e_notes (id int PRIMARY KEY, note varchar(100) NOT NULL);
INSERT INTO scratch.e2e_notes VALUES (1, 'seed');
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.* TO 'rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON sakila.rental TO 'rw'@'%';
