-- UNTESTED (Oracle could not be run in the T1 environment). Run as SYSTEM in FREEPDB1.
-- Schema SCRATCH owns the objects; RO and RW are separate accounts.
CREATE USER scratch IDENTIFIED BY scratch_pw QUOTA UNLIMITED ON users;
CREATE USER ro IDENTIFIED BY ro_pw;
CREATE USER rw IDENTIFIED BY rw_pw;
GRANT CREATE SESSION TO ro, rw;
CREATE USER private IDENTIFIED BY private_pw QUOTA UNLIMITED ON users;

CREATE TABLE scratch.orders (
  id         NUMBER(10) PRIMARY KEY,
  note       VARCHAR2(200 CHAR),
  amount     NUMBER(10,2),
  created_at TIMESTAMP(3),
  ref        RAW(16),
  payload    RAW(64),
  doc        CLOB CONSTRAINT orders_doc_json CHECK (doc IS JSON),
  nothing    VARCHAR2(50)
);
INSERT INTO scratch.orders VALUES (1, 'caf' || UNISTR('\00E9 \20AC \65E5'), 12.50, TIMESTAMP '2026-10-29 12:34:56.789',
  HEXTORAW('123E4567E89B12D3A456426614174000'), HEXTORAW('DEADBEEF'), '{"a": 1, "b": [1, 2]}', NULL);
INSERT INTO scratch.orders VALUES (2, 'plain', 0, NULL, NULL, NULL, NULL, NULL);
CREATE TABLE scratch.uniq (email VARCHAR2(100) PRIMARY KEY);
INSERT INTO scratch.uniq VALUES ('a@example.com');
CREATE TABLE scratch.t_probe (id NUMBER(10));
CREATE TABLE private.secrets (v VARCHAR2(50));
INSERT INTO private.secrets VALUES ('hidden');
COMMIT;
GRANT SELECT ON scratch.orders, scratch.uniq, scratch.t_probe TO ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.orders TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.uniq TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON scratch.t_probe TO rw;
