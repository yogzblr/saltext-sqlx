-- PostgreSQL (sakiladb/postgres): accounts and probe tables for the end-to-end suite. Run once as `sakila` (superuser).
-- ro: SELECT on the Sakila schema (public) only. rw: DML on scratch.e2e_notes and on public.rental.
\set ON_ERROR_STOP on
CREATE ROLE ro LOGIN PASSWORD 'RoE2e_Secret_7731';
CREATE ROLE rw LOGIN PASSWORD 'RwE2e_Secret_4412';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE sakila TO ro, rw;
GRANT USAGE ON SCHEMA public TO ro, rw;

-- type_probe: closest PostgreSQL type for each probe column
CREATE TABLE public.type_probe (
  id      integer PRIMARY KEY,
  t_text  text,
  t_num   numeric(10,2),
  t_ts    timestamp(6),
  t_uuid  uuid,
  t_bin   bytea,
  t_json  jsonb,
  t_null  text
);
INSERT INTO public.type_probe VALUES
  (1, 'Grüße ✓ naïve', 12.50, '2026-10-31 12:34:56.789012', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '\xdeadbeef', '{"a": 1, "b": [true, null]}', NULL),
  (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ro;

CREATE SCHEMA scratch;
CREATE TABLE scratch.e2e_notes (id integer PRIMARY KEY, note varchar(100) NOT NULL);
INSERT INTO scratch.e2e_notes VALUES (1, 'seed');
GRANT USAGE ON SCHEMA scratch TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.e2e_notes TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.rental TO rw;
GRANT USAGE ON SEQUENCE public.rental_rental_id_seq TO rw;
