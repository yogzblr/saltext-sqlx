-- Run as superuser against database sqlx_test (created UTF8 by native/up.sh or the compose init).
CREATE SCHEMA IF NOT EXISTS scratch;
CREATE SCHEMA IF NOT EXISTS private;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ro') THEN CREATE ROLE ro LOGIN PASSWORD 'ro_pw'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rw') THEN CREATE ROLE rw LOGIN PASSWORD 'rw_pw'; END IF;
END $$;

CREATE TABLE scratch.orders (
  id         integer PRIMARY KEY,
  note       text,
  amount     numeric(10,2),
  created_at timestamp,
  ref        uuid,
  payload    bytea,
  doc        jsonb,
  nothing    text
);
INSERT INTO scratch.orders VALUES
  (1, 'caf' || chr(233) || ' ' || chr(8364) || ' ' || chr(26085), 12.50, '2026-10-29 12:34:56.789',
   '123e4567-e89b-12d3-a456-426614174000', '\xdeadbeef', '{"a": 1, "b": [1, 2]}', NULL),
  (2, 'plain', 0.00, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE scratch.uniq (email text PRIMARY KEY);
INSERT INTO scratch.uniq VALUES ('a@example.com');
CREATE TABLE scratch.t_probe (id integer);
CREATE TABLE private.secrets (v text);
INSERT INTO private.secrets VALUES ('hidden');

GRANT USAGE ON SCHEMA scratch TO ro, rw;
GRANT SELECT ON ALL TABLES IN SCHEMA scratch TO ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA scratch TO rw;
ALTER ROLE ro SET search_path = scratch;
ALTER ROLE rw SET search_path = scratch;
