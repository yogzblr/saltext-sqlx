-- SQL Server (sakiladb/sqlserver): logins, users and probe tables. Run once as `sakila` (sysadmin in this image).
-- ro: SELECT on schema dbo (the Sakila schema) only. rw: DML on scratch.e2e_notes and on dbo.rental.
CREATE LOGIN ro WITH PASSWORD = 'RoE2e_Secret_7731', CHECK_POLICY = OFF, DEFAULT_DATABASE = sakila;
CREATE LOGIN rw WITH PASSWORD = 'RwE2e_Secret_4412', CHECK_POLICY = OFF, DEFAULT_DATABASE = sakila;
GO
USE sakila;
GO
CREATE USER ro FOR LOGIN ro WITH DEFAULT_SCHEMA = dbo;
CREATE USER rw FOR LOGIN rw WITH DEFAULT_SCHEMA = dbo;
GO
-- type_probe: closest SQL Server type (JSON is NVARCHAR(MAX): the json type exists only from SQL Server 2025)
CREATE TABLE dbo.type_probe (
  id      int PRIMARY KEY,
  t_text  nvarchar(100),
  t_num   decimal(10,2),
  t_ts    datetime2(6),
  t_uuid  uniqueidentifier,
  t_bin   varbinary(16),
  t_json  nvarchar(max),
  t_null  nvarchar(10)
);
INSERT INTO dbo.type_probe VALUES
  (1, N'Grüße ✓ naïve', 12.50, '2026-10-31 12:34:56.789012', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 0xdeadbeef, N'{"a": 1, "b": [true, null]}', NULL),
  (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
GRANT SELECT ON SCHEMA::dbo TO ro;
GO
CREATE SCHEMA scratch;
GO
CREATE TABLE scratch.e2e_notes (id int PRIMARY KEY, note nvarchar(100) NOT NULL);
INSERT INTO scratch.e2e_notes VALUES (1, N'seed');
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::scratch TO rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.rental TO rw;
GO
