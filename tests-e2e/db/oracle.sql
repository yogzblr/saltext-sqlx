-- Oracle (sakiladb/oracle, PDB SAKILA): accounts and probe tables. Run once as SYSDBA (OS authentication inside the
-- container). ro: CREATE SESSION + SELECT on every SAKILA table and view, reached through private synonyms so that
-- `select count(*) from film` works as on the other databases. rw: DML on SCRATCH.E2E_NOTES and on SAKILA.RENTAL.
WHENEVER SQLERROR EXIT FAILURE
SET DEFINE OFF
ALTER SESSION SET CONTAINER = SAKILA;

CREATE USER ro IDENTIFIED BY "RoE2e_Secret_7731";
CREATE USER rw IDENTIFIED BY "RwE2e_Secret_4412";
GRANT CREATE SESSION TO ro, rw;
-- the scratch schema owns the write table; it cannot log in
CREATE USER scratch NO AUTHENTICATION DEFAULT TABLESPACE users QUOTA 20M ON users;

-- type_probe: closest Oracle type (no uuid type: RAW(16); binary: BLOB; JSON: the 23ai native JSON type;
-- text: NVARCHAR2; Oracle has no boolean column here and '' is NULL)
CREATE TABLE sakila.type_probe (
  id      NUMBER(10) PRIMARY KEY,
  t_text  NVARCHAR2(100),
  t_num   NUMBER(10,2),
  t_ts    TIMESTAMP(6),
  t_uuid  RAW(16),
  t_bin   BLOB,
  t_json  JSON,
  t_null  VARCHAR2(10)
);
INSERT INTO sakila.type_probe VALUES (1, UNISTR('Gr\00FC\00DFe \2713 na\00EFve'), 12.50,
  TIMESTAMP '2026-10-31 12:34:56.789012', HEXTORAW('A0EEBC999C0B4EF8BB6D6BB9BD380A11'), HEXTORAW('DEADBEEF'),
  JSON('{"a": 1, "b": [true, null]}'), NULL);
INSERT INTO sakila.type_probe VALUES (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
COMMIT;

BEGIN
  FOR o IN (SELECT object_name FROM dba_objects WHERE owner = 'SAKILA' AND object_type IN ('TABLE', 'VIEW')) LOOP
    EXECUTE IMMEDIATE 'GRANT SELECT ON sakila."' || o.object_name || '" TO ro';
    EXECUTE IMMEDIATE 'CREATE SYNONYM ro."' || o.object_name || '" FOR sakila."' || o.object_name || '"';
  END LOOP;
END;
/

CREATE TABLE scratch.e2e_notes (id NUMBER(10) PRIMARY KEY, note VARCHAR2(100) NOT NULL);
INSERT INTO scratch.e2e_notes VALUES (1, 'seed');
COMMIT;
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.e2e_notes TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON sakila.rental TO rw;
GRANT SELECT ON sakila.type_probe TO rw;
CREATE SYNONYM rw.rental FOR sakila.rental;
CREATE SYNONYM rw.type_probe FOR sakila.type_probe;
EXIT
