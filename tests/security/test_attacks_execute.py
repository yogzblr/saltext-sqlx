# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""T11 part 2, attack class (5): bypass the ``execute`` policy (``allow_execute``, verb filter, ``dry_run``).

Tables ``scratch.t11_x`` (victim) and ``scratch.t11_y`` (target) hold known rows; every case resets them, runs
``sqlx.execute`` through the public function and reads the state back with the admin account on a fresh connection.
"""

import socket

import pytest
from salt.exceptions import SaltInvocationError

from tests.security import harness
from tests.security.harness import observe

X, Y = "scratch.t11_x", "scratch.t11_y"


@pytest.fixture(scope="module")
def tables(server):
    for t in (X, Y):
        server.admin(f"DROP TABLE IF EXISTS {t}")
        server.admin(f"CREATE TABLE {t} (id integer PRIMARY KEY, v integer)")
        if server.key == "postgresql":
            server.admin(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO rw")
            server.admin(f"GRANT SELECT ON {t} TO ro")
    try:
        yield server
    finally:
        for t in (X, Y):
            server.admin(f"DROP TABLE IF EXISTS {t}")


@pytest.fixture
def ex(tables):
    """``ex(profile, sql, **kw)`` -> ``(outcome, (x_rows, y_rows))`` starting from x=[(1,1)], y=[(1,1)]."""
    srv = tables
    profiles = {
        "rwv": srv.profile("rw", allow_execute=True, read_only=False),
        "rwi": srv.profile("rw", allow_execute=True, read_only=False, execute_verbs=["INSERT"]),
        "rwu": srv.profile("rw", allow_execute=True, read_only=False, execute_verbs=["UPDATE"]),
        "rwnone": srv.profile("rw", allow_execute=True, read_only=False, execute_verbs=[]),
        "rw_noexec": srv.profile("rw", read_only=False),
        "ro": srv.profile("ro"),
    }
    with harness.injected(profiles) as module:

        def run(profile, sql, label=None, **kw):
            for t in (X, Y):
                srv.admin(f"DELETE FROM {t}")
                srv.admin(f"INSERT INTO {t} VALUES (1, 1)")
            outcome = harness.attempt(lambda: module.execute(profile, sql, **kw))
            state = (
                srv.admin(f"SELECT id, v FROM {X} ORDER BY id"),
                srv.admin(f"SELECT id, v FROM {Y} ORDER BY id"),
            )
            observe(
                "execute",
                f"{srv.key} {profile}{kw or ''}: {label or sql[:80]!r}",
                f"{harness.describe(outcome)} | x={state[0]} y={state[1]}",
            )
            return outcome, state

        run.module = module
        yield run


UNCHANGED = ([(1, 1)], [(1, 1)])


def test_positive_control_an_allowed_statement_commits(ex):
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (2, 2)")
    assert outcome[0] == "OK" and outcome[1]["committed"] is True and state[1] == [(1, 1), (2, 2)]


@pytest.mark.parametrize("profile", ["rw_noexec", "ro"])
def test_allow_execute_false_refuses_before_any_connection(ex, profile, monkeypatch):
    attempts = []
    real = socket.socket.connect
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, a: (attempts.append(a), real(self, a))[1]
    )
    outcome = harness.attempt(lambda: ex.module.execute(profile, f"INSERT INTO {Y} VALUES (5, 5)"))
    monkeypatch.undo()
    observe(
        "execute",
        f"{profile} allow_execute false",
        f"{harness.describe(outcome)}; sockets opened: {len(attempts)}",
    )
    assert outcome[0] == "ERR" and str(outcome[1]).startswith("[E_EXECUTE_DISABLED]")
    assert attempts == []


@pytest.mark.parametrize(
    "kw",
    [
        {"allow_execute": True},
        {"execute_verbs": ["DELETE"]},
        {"read_only": False},
        {"dialect": "x"},
    ],
)
def test_policy_flags_are_not_call_arguments(ex, kw):
    outcome, state = ex("rw_noexec", f"INSERT INTO {Y} VALUES (5, 5)", **kw)
    assert (
        outcome[0] == "ERR" and isinstance(outcome[1], SaltInvocationError) and state == UNCHANGED
    )


def test_verb_not_in_the_list_is_refused(ex):
    outcome, state = ex("rwi", f"DELETE FROM {X}")
    assert str(outcome[1]).startswith("[E_POLICY]") and state == UNCHANGED
    outcome, state = ex("rwnone", f"INSERT INTO {Y} VALUES (5, 5)")
    assert str(outcome[1]).startswith("[E_POLICY]") and state == UNCHANGED


REFUSED_SHAPES = [
    (
        "cte-delete-then-insert",
        "WITH d AS (DELETE FROM {x} RETURNING id) INSERT INTO {y} SELECT id, 0 FROM d",
        "E_POLICY",
    ),
    ("with-first", "WITH d AS (SELECT 1) DELETE FROM {x}", "E_POLICY"),
    ("fullwidth-INSERT", "ＩＮＳＥＲＴ INTO {y} VALUES (7, 7)", "E_POLICY"),
    ("null-byte-prefix", "\x00INSERT INTO {y} VALUES (7, 7)", "E_OPTION_INVALID"),
    ("INSERTX", "INSERTX INTO {y} VALUES (7, 7)", "E_POLICY"),
    ("INSERT_", "INSERT_ INTO {y} VALUES (7, 7)", "E_POLICY"),
    ("parenthesised", "(INSERT INTO {y} VALUES (7, 7))", "E_POLICY"),
    ("quoted-verb", "'INSERT' INTO {y} VALUES (7, 7)", "E_POLICY"),
    ("backtick-verb", "`INSERT` INTO {y} VALUES (7, 7)", "E_POLICY"),
    ("unterminated-comment", "/* INSERT INTO {y} VALUES (7, 7)", "E_OPTION_INVALID"),
    ("comment-only", "-- INSERT INTO {y} VALUES (7, 7)", "E_OPTION_INVALID"),
    ("whitespace-only", "   \n\t  ", "E_OPTION_INVALID"),
    ("COMMIT", "COMMIT", "E_POLICY"),
    ("BEGIN", "BEGIN", "E_POLICY"),
    ("SET", "SET autocommit = 1", "E_POLICY"),
    ("CALL", "CALL scratch.t11_nothing()", "E_POLICY"),
    ("DROP", "DROP TABLE {x}", "E_POLICY"),
    ("TRUNCATE", "TRUNCATE {x}", "E_POLICY"),
    ("SELECT", "SELECT 1", "E_POLICY"),
    ("hash-comment-first", "# c\nINSERT INTO {y} VALUES (7, 7)", "E_POLICY"),
]


@pytest.mark.parametrize("label,template,code", REFUSED_SHAPES, ids=[c[0] for c in REFUSED_SHAPES])
def test_verb_filter_refuses_these_shapes(ex, label, template, code):
    if label == "hash-comment-first":
        pass  # '#' is a MySQL comment, but the filter does not skip it: over-strict, safe
    outcome, state = ex("rwi", template.format(x=X, y=Y), label=label)
    assert outcome[0] == "ERR" and str(outcome[1]).startswith(f"[{code}]"), harness.describe(
        outcome
    )
    assert state == UNCHANGED


ALLOWED_SHAPES = [
    ("lowercase", "insert into {y} values (7, 7)"),
    ("block-comment-first", "/* c */ INSERT INTO {y} VALUES (7, 7)"),
    ("line-comment-first", "-- c\nINSERT INTO {y} VALUES (7, 7)"),
    ("plus-hint-comment", "/*+ hint */ INSERT INTO {y} VALUES (7, 7)"),
    ("tabs-newlines", "\n\t INSERT INTO {y} VALUES (7, 7)"),
]


@pytest.mark.parametrize("label,template", ALLOWED_SHAPES, ids=[c[0] for c in ALLOWED_SHAPES])
def test_verb_filter_accepts_these_shapes(ex, label, template):
    outcome, state = ex("rwi", template.format(x=X, y=Y), label=label)
    assert outcome[0] == "OK" and state[1] == [(1, 1), (7, 7)]


# ---- verb filter bypasses (best effort, documented as such; recorded as defects D5 and D1) ------------------------------------
def test_pg_nested_block_comment_cannot_hide_a_verb(request, ex, server):
    """D5: PostgreSQL nests ``/* /* */ */``; the filter ends the comment at the first ``*/`` and sees INSERT, the server runs DELETE."""
    if server.key != "postgresql":
        pytest.skip("PostgreSQL comment syntax")
    outcome, state = ex(
        "rwi",
        f"/* /* */ INSERT INTO {Y} VALUES (6, 6) */ DELETE FROM {X}",
        label="nested comment hides DELETE",
    )
    assert state == UNCHANGED, f"DELETE ran although execute_verbs is [INSERT]: x={state[0]}"


@pytest.mark.parametrize(
    "label,sql",
    [
        (
            "executable-comment",
            "/*! INSERT INTO {y} VALUES (8, 8) ON DUPLICATE KEY */ UPDATE v = 5",
        ),
        (
            "versioned-executable-comment",
            "/*!50000 INSERT INTO {y} VALUES (8, 8) ON DUPLICATE KEY */ UPDATE v = 5",
        ),
        (
            "mariadb-executable-comment",
            "/*M! INSERT INTO {y} VALUES (8, 8) ON DUPLICATE KEY */ UPDATE v = 5",
        ),
    ],
)
def test_mysql_executable_comment_cannot_hide_a_verb(request, ex, server, label, sql):
    """D5: ``/*! ... */`` is SQL for MySQL/MariaDB (``/*M! */`` for MariaDB); the filter skips it as a comment."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    if label == "mariadb-executable-comment" and server.key != "mariadb":
        pytest.skip("MariaDB-only comment form (MySQL sees a plain comment and fails with 1064)")
    outcome, state = ex("rwu", sql.format(y=Y), label=label)
    assert state == UNCHANGED, f"an INSERT ran although execute_verbs is [UPDATE]: y={state[1]}"


@pytest.mark.parametrize(
    "sql",
    [
        "/*!50000 DROP TABLE {x} */ UPDATE {y} SET v = 9 WHERE id = 1",
        "UPDATE {y} SET v = 9 WHERE id = 1 /*! ; DELETE FROM {x} */",
        "/*! DELETE FROM {x} */ INSERT INTO {y} VALUES (7, 7)",
        "/*!50000 DELETE FROM {x} WHERE id = 1 */ /* c */ ;",
        "/*! DELETE */ FROM {x} WHERE id = 1",
    ],
    ids=[
        "drop-then-update",
        "semicolon-delete",
        "delete-then-insert",
        "comments-only-after",
        "split-verb",
    ],
)
def test_mysql_literal_executable_comment_attacks_do_nothing(ex, server, sql):
    """The forms from the coordinator's request: refused by the driver (no multi-statements) or by the filter; nothing runs."""
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, state = ex("rwv", sql.format(x=X, y=Y))
    assert outcome[0] == "ERR" and state == UNCHANGED


def test_pg_semicolon_cannot_smuggle_a_second_verb(request, ex, server):
    """D1: the filter looks at the first keyword only; PostgreSQL runs the rest."""
    if server.key != "postgresql":
        pytest.skip("multi-statement strings run on PostgreSQL only")
    outcome, state = ex("rwi", f"INSERT INTO {Y} VALUES (6, 6); DELETE FROM {X}")
    assert state == UNCHANGED, f"DELETE ran although execute_verbs is [INSERT]: x={state[0]}"


def test_mysql_family_semicolon_is_a_syntax_error(ex, server):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, state = ex("rwi", f"INSERT INTO {Y} VALUES (6, 6); DELETE FROM {X}")
    assert outcome[0] == "ERR" and "1064" in str(outcome[1]) and state == UNCHANGED


# ---- dry_run ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value", ["False", "false", "True", "0", 0, 1, 0.0, [], {}, ["x"], "", " "], ids=repr
)
def test_dry_run_must_be_a_real_boolean(ex, value):
    outcome, state = ex(
        "rwv", f"INSERT INTO {Y} VALUES (9, 9)", label=f"dry_run={value!r}", dry_run=value
    )
    assert (
        outcome[0] == "ERR"
        and str(outcome[1]).startswith("[E_OPTION_INVALID]")
        and state == UNCHANGED
    )


def test_dry_run_true_rolls_back_and_says_so(ex):
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (9, 9)", dry_run=True)
    assert outcome[0] == "OK" and outcome[1]["committed"] is False and outcome[1]["dry_run"] is True
    assert outcome[1]["rows_affected"] == 1 and state == UNCHANGED


def test_documented_dry_run_none_means_not_given_and_commits(ex):
    """D11 (design note): ``dry_run=None`` is "not given", the default is false (SPEC D2), so the statement is committed."""
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (9, 9)", dry_run=None)
    assert outcome[0] == "OK" and outcome[1]["committed"] is True and state[1] == [(1, 1), (9, 9)]


def test_dry_run_true_cannot_be_defeated_by_a_commit_in_the_statement(request, ex, server):
    """D1: on PostgreSQL ``INSERT ...; COMMIT`` with dry_run=True is committed while the result says committed false."""
    if server.key != "postgresql":
        pytest.skip("multi-statement strings run on PostgreSQL only")
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (8, 8); COMMIT", dry_run=True)
    # Fixed: the multi-statement string is refused (42601); either outcome is safe as long as nothing persists.
    assert outcome[0] == "ERR" or outcome[1]["committed"] is False
    assert state == UNCHANGED, f"dry_run=True persisted a row: y={state[1]}"


def test_mysql_family_dry_run_commit_in_statement_is_a_syntax_error(ex, server):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (8, 8); COMMIT", dry_run=True)
    assert outcome[0] == "ERR" and state == UNCHANGED


def test_failed_statement_is_rolled_back(ex, server):
    outcome, state = ex("rwv", f"INSERT INTO {Y} VALUES (1, 2)")  # duplicate key
    assert (
        outcome[0] == "ERR" and str(outcome[1]).startswith("[E_CONSTRAINT]") and state == UNCHANGED
    )
