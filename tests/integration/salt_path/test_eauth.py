"""
``external_auth`` regex limit on ``profile`` (SECURITY.md section 4, TEST_PLAN section 5 item 5).

The master is configured with the ``file`` eauth backend (a throwaway user in a tmp file; ``pam`` would need a system user)::

    external_auth:
      file:
        ^filename: <tmp>/t10_eauth.txt
        t10_alice:
          - '*':
              - sqlx.query:
                  kwargs: {profile: '^pg_ro$'}
              - sqlx.ping
"""

import pytest
import salt.exceptions

from tests.integration.salt_path.conftest import EAUTH_PASSWORD
from tests.integration.salt_path.conftest import EAUTH_USER

pytestmark = [pytest.mark.pg, pytest.mark.mysql]


@pytest.fixture
def as_alice(client, minion):
    def _cmd(function, username=EAUTH_USER, password=EAUTH_PASSWORD, **kwargs):
        return client.cmd(
            minion.id,
            function,
            full_return=True,
            timeout=60,
            eauth="file",
            username=username,
            password=password,
            **kwargs,
        )

    return _cmd


def test_permitted_user_can_query_the_permitted_profile(as_alice, minion):
    result = as_alice("sqlx.query", kwarg={"profile": "pg_ro", "sql": "select 1 as one"})
    assert result[minion.id]["retcode"] == 0
    assert result[minion.id]["ret"]["rows"] == [[1]]
    assert result[minion.id]["ret"]["profile"] == "pg_ro"


@pytest.mark.parametrize(
    "profile",
    [
        "mysql_ro",  # another profile
        "pg_ro_x",  # the regex is anchored: a longer name does not match
        "pg_ro\nmysql_ro",  # an embedded newline does not slip past the anchors
    ],
)
def test_other_profiles_are_refused(as_alice, profile):
    with pytest.raises(salt.exceptions.AuthorizationError):
        as_alice("sqlx.query", kwarg={"profile": profile, "sql": "select 1"})


def test_profile_given_positionally_is_refused(as_alice):
    """``arg=["mysql_ro", ...]`` cannot be used to dodge the ``kwargs`` rule."""
    with pytest.raises(salt.exceptions.AuthorizationError):
        as_alice("sqlx.query", arg=["mysql_ro", "select 1"])


def test_missing_profile_is_refused(as_alice):
    with pytest.raises(salt.exceptions.AuthorizationError):
        as_alice("sqlx.query", kwarg={"sql": "select 1"})


@pytest.mark.parametrize("function", ["sqlx.execute", "sqlx.table_list", "sqlx.check_row_count"])
def test_functions_that_are_not_granted_are_refused(as_alice, function):
    """``sqlx.execute`` is refused even for the permitted profile name (and for the allow_execute profile)."""
    for profile in ("pg_ro", "pg_rw"):
        with pytest.raises(salt.exceptions.AuthorizationError):
            as_alice(function, kwarg={"profile": profile, "sql": "select 1"})


def test_wrong_password_is_refused(as_alice):
    with pytest.raises(salt.exceptions.AuthenticationError):
        as_alice(
            "sqlx.query",
            password="not-the-password",
            kwarg={"profile": "pg_ro", "sql": "select 1"},
        )


def test_a_bare_function_entry_has_no_profile_limit(as_alice, minion):
    """
    Documented consequence of SECURITY.md section 4: ``- sqlx.ping`` with no ``kwargs`` limits nothing, so the user can
    ping ANY profile (here ``mysql_ro``). Give every function entry its own ``kwargs: {profile: ...}``.
    """
    result = as_alice("sqlx.ping", kwarg={"profile": "mysql_ro"})
    assert result[minion.id]["retcode"] == 0
    assert result[minion.id]["ret"]["dialect"] == "mysql"
