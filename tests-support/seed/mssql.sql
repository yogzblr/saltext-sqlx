-- UNTESTED (SQL Server could not be run in the T1 environment). Run with sqlcmd as sa.
IF DB_ID('sqlx_test') IS NULL CREATE DATABASE sqlx_test;
GO
USE sqlx_test;
GO
IF SUSER_ID('ro') IS NULL CREATE LOGIN ro WITH PASSWORD = 'Ro_pw_12345!', CHECK_POLICY = OFF;
IF SUSER_ID('rw') IS NULL CREATE LOGIN rw WITH PASSWORD = 'Rw_pw_12345!', CHECK_POLICY = OFF;
CREATE USER ro FOR LOGIN ro;
CREATE USER rw FOR LOGIN rw;
GO
CREATE SCHEMA scratch;
GO
CREATE SCHEMA private;
GO
CREATE TABLE scratch.orders (
  id         int PRIMARY KEY,
  note       nvarchar(200),
  amount     decimal(10,2),
  created_at datetime2(3),
  ref        uniqueidentifier,
  payload    varbinary(64),
  doc        nvarchar(max) CONSTRAINT orders_doc_json CHECK (ISJSON(doc) = 1),
  nothing    nvarchar(50)
);
INSERT INTO scratch.orders VALUES
  (1, N'caf' + NCHAR(233) + N' ' + NCHAR(8364) + N' ' + NCHAR(26085), 12.50, '2026-10-29 12:34:56.789',
   '123e4567-e89b-12d3-a456-426614174000', 0xDEADBEEF, N'{"a": 1, "b": [1, 2]}', NULL),
  (2, N'plain', 0.00, NULL, NULL, NULL, NULL, NULL);
CREATE TABLE scratch.uniq (email nvarchar(100) PRIMARY KEY);
INSERT INTO scratch.uniq VALUES (N'a@example.com');
CREATE TABLE scratch.t_probe (id int);
CREATE TABLE private.secrets (v nvarchar(50));
INSERT INTO private.secrets VALUES (N'hidden');
GO
GRANT SELECT ON SCHEMA::scratch TO ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::scratch TO rw;
GO
