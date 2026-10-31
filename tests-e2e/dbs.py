"""Per-database SQL and expected database texts for the e2e matrix (the same Sakila data on all five)."""

# The Sakila schema each database exposes, as table_list reports it.
SCHEMA = {
    "pg": "public",
    "mysql": "sakila",
    "mariadb": "sakila",
    "oracle": "sakila",
    "mssql": "dbo",
}
TABLES = sorted(
    "actor address category city country customer film film_actor film_category film_text inventory language "
    "payment rental staff store type_probe".split()
)
VIEWS = sorted(
    "actor_info customer_list film_list nicer_but_slower_film_list sales_by_film_category sales_by_store staff_list".split()
)

DUAL = {"oracle": " FROM DUAL"}
#: The integer type CAST accepts: MySQL takes SIGNED/UNSIGNED, not INTEGER (1064, observed on 9.7.1); the others INTEGER.
INT = {"mysql": "SIGNED"}


def dual(db):
    return DUAL.get(db, "")


def sql(db, case):
    d = dual(db)
    i = INT.get(db, "INTEGER")
    common = {
        "count_film": "select count(*) as n from film",
        "join_actor": (
            "select f.title from film f join film_actor fa on fa.film_id = f.film_id "
            "join actor a on a.actor_id = fa.actor_id where a.actor_id = 1 order by f.title"
        ),
        "join_rental": (
            "select count(*) as n from rental r join customer c on c.customer_id = r.customer_id "
            "join inventory i on i.inventory_id = r.inventory_id where c.customer_id = 1"
        ),
        "types": "select id, t_text, t_num, t_ts, t_uuid, t_bin, t_json, t_null from type_probe order by id",
        "cast": f"select CAST(:x AS {i}) + 1 as v{d}",
        "colon": f"select 'a\\:b' as v{d}",
        "films": "select film_id from film order by film_id",
        "actors3": "select actor_id from actor where actor_id <= 3 order by actor_id",
        "actor1_name": "select first_name, last_name from actor where actor_id = 1",
        "null_value": "select t_null from type_probe where id = 1",
        "syntax": "select * fro film",
        "missing_table": "select * from nope_e2e",
        "missing_column": "select nope_e2e from film",
        "div0": f"select 1/0 as v{d}",
        "multi": f"select 1 as a{d}; select 2 as b{d}",
        "ro_write": "delete from film where film_id = -1",
        "not_null": "insert into scratch.e2e_notes (id) values (9001)",
        "unique": "insert into scratch.e2e_notes (id, note) values (1, 'dup')",
        "insert_note": f"insert into scratch.e2e_notes (id, note) values (CAST(:i AS {i}), :n)",
        "count_note": f"select count(*) as n from scratch.e2e_notes where id = CAST(:i AS {i})",
        "delete_note": f"delete from scratch.e2e_notes where id = CAST(:i AS {i})",
        "multi_dml": (
            "insert into scratch.e2e_notes (id, note) values (9101, 'a'); "
            "insert into scratch.e2e_notes (id, note) values (9102, 'b')"
        ),
        "exec_comment": "/*!40000 insert into scratch.e2e_notes (id, note) values (9201, 'x') */",
        "nul": f"select 'a\x00b' as v{d}",
        "nul_delete": "delete from scratch.e2e_notes\x00 where id = 99999",
        "select_verb": f"select 1 as v{d}",
    }
    per_db = {
        "pg": {
            "sleep": "select pg_sleep(5)",
            "fk": "insert into rental (rental_date, inventory_id, customer_id, staff_id) values (now(), 1, 32000, 1)",
            "big": "select a.payment_id, b.payment_id from payment a cross join payment b",
            "large_value": "select repeat('x', 2000000) as v",
        },
        "mysql": {
            # MySQL 9.7.1 cuts SLEEP() short silently (returns 1, no error) even when the SELECT reads tables, so the
            # timeout case is a statement that really works for longer than the limit
            "sleep": "select count(*) from payment a, payment b where a.amount + b.amount > 3.5",
            "sleep_silent": "select sleep(5) from actor limit 1",
            "sleep_hint": (
                "select /*+ MAX_EXECUTION_TIME(100000) */ count(*) from payment a, payment b "
                "where a.amount + b.amount > 3.5"
            ),
            "fk": "insert into rental (rental_date, inventory_id, customer_id, staff_id) values (now(), 1, 32000, 1)",
            "big": "select a.payment_id, b.payment_id from payment a cross join payment b",
            "large_value": "select repeat('x', 2000000) as v",
        },
        "mariadb": {
            "sleep": "select sleep(5)",
            "sleep_hint": "SET STATEMENT max_statement_time=0 FOR select sleep(5)",
            "fk": "insert into rental (rental_date, inventory_id, customer_id, staff_id) values (now(), 1, 32000, 1)",
            "big": "select a.payment_id, b.payment_id from payment a cross join payment b",
            "large_value": "select repeat('x', 2000000) as v",
        },
        "oracle": {
            "sleep": "select count(*) from payment a, payment b, payment c",
            "fk": (
                "insert into rental (rental_id, rental_date, inventory_id, customer_id, staff_id, last_update) "
                "values (999999, systimestamp, 1, 32000, 1, systimestamp)"
            ),
            "big": "select a.payment_id, b.payment_id from payment a cross join payment b",
            "large_value": "select rpad(to_clob('x'), 2000000, 'x') as v from dual",
            "syntax": "select * fro film",
        },
        "mssql": {
            "sleep": "waitfor delay '00:00:05'",
            "fk": "insert into rental (rental_date, inventory_id, customer_id, staff_id) values (getdate(), 1, 32000, 1)",
            "big": "select a.payment_id, b.payment_id from payment a cross join payment b",
            "large_value": "select replicate(cast('x' as varchar(max)), 2000000) as v",
        },
    }
    return {**common, **per_db[db]}[case]


# Fragments of the real database message expected for each error case (the text the database itself sends).
TEXT = {
    "syntax": {
        "pg": "42601",
        "mysql": "1064",
        "mariadb": "1064",
        "oracle": "ORA-00923",
        "mssql": "102",
    },
    "missing_table": {
        "pg": "42P01",
        "mysql": "1146",
        "mariadb": "1146",
        "oracle": "ORA-00942",
        "mssql": "208",
    },
    "missing_column": {
        "pg": "42703",
        "mysql": "1054",
        "mariadb": "1054",
        "oracle": "ORA-00904",
        "mssql": "207",
    },
    "div0": {"pg": "22012", "oracle": "ORA-01476", "mssql": "8134"},
    "unique": {
        "pg": "23505",
        "mysql": "1062",
        "mariadb": "1062",
        "oracle": "ORA-00001",
        "mssql": "2627",
    },
    "fk": {
        "pg": "23503",
        "mysql": "1452",
        "mariadb": "1452",
        "oracle": "ORA-02291",
        "mssql": "547",
    },
    "not_null": {
        "pg": "23502",
        "mysql": "1364",
        "mariadb": "1364",
        "oracle": "ORA-01400",
        "mssql": "515",
    },
    "permission": {
        "pg": "42501",
        "mysql": "1142",
        "mariadb": "1142",
        "oracle": "ORA-41900",
        "mssql": "229",
    },
    "read_only": {"pg": "25006", "mysql": "1792", "mariadb": "1792", "oracle": "ORA-01456"},
    "timeout": {
        "pg": "57014",
        "mysql": "3024",
        "mariadb": "1969",
        "oracle": "DPY-4024",
        "mssql": "20003",
    },
    "auth": {
        "pg": "password authentication failed",
        "mysql": "1045",
        "mariadb": "1045",
        "oracle": "ORA-01017",
        "mssql": "18456",
    },
    "nopw": {
        "pg": "no password supplied",
        "mysql": "1045",
        "mariadb": "1045",
        "oracle": "DPY-4001",
        "mssql": "18456",
    },
    "closed": {
        "pg": "Connection refused",
        "mysql": "2003",
        "mariadb": "2003",
        "oracle": "DPY-6005",
        "mssql": "20009",
    },
    "unreach": {
        "pg": "timeout expired",
        "mysql": "2003",
        "mariadb": "2003",
        "oracle": "DPY-6005",
        "mssql": "20009",
    },
    "nohost": {
        "pg": "resolve",
        "mysql": "2003",
        "mariadb": "2003",
        "oracle": "Name or service not known",
        "mssql": "20009",
    },
    "multi": {
        "pg": "42601",
        "mysql": "1064",
        "mariadb": "1064",
        "oracle": "ORA-03405",
        "mssql": "more than one statement",
    },
}
