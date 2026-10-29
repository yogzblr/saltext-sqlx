"""``sqlx.psql_query``: argument translation and result shaping for a Salt ``postgres.psql_query`` look-alike.

``postgres.psql_query(query, user, host, port, maintenance_db, password, runas, write)`` runs ``psql``. The compat
function runs the same request through the sqlx path (profile, policy, error fidelity). Differences from Salt are listed
in SPEC section 4.7. No Salt dunders and no driver imports here.
"""

import json

from salt.exceptions import SaltInvocationError

__all__ = ["shape", "supplied_names", "translate"]

#: psql_query argument -> sqlx connection keyword
_MAP = {
    "user": "username",
    "host": "host",
    "port": "port",
    "maintenance_db": "database",
    "password": "password",
}


def _fail(message):
    raise SaltInvocationError(f"[E_OPTION_INVALID] {message}")


def supplied_names(**given):
    """Names of the connection arguments of ``psql_query`` that are not ``None`` (for the policy message)."""
    return sorted(name for name in _MAP if given.get(name) is not None)


def translate(query, runas, write, **given):
    """
    Return ``(function, kwargs)`` for ``_run``: ``("query"|"execute", {...})``.

    ``given`` holds ``user``, ``host``, ``port``, ``maintenance_db``, ``password`` and ``extra`` (the other keywords
    of the call). ``runas`` other than ``None`` is refused; ``write`` must be a real bool. A trailing ``;`` is
    removed like Salt does. A connection argument also present in ``extra`` under its sqlx name is a conflict.
    """
    if runas is not None:
        _fail("runas is not supported; sqlx does not use psql or an OS user")
    if not isinstance(write, bool):
        _fail("option 'write' must be a boolean")
    if not isinstance(query, str):
        _fail("option 'query' must be a non-empty string")
    kwargs = dict(given.get("extra") or {})
    for arg, key in _MAP.items():
        value = given.get(arg)
        if value is None:
            continue
        if kwargs.get(key) is not None:
            _fail(f"'{arg}' and '{key}' both given")
        kwargs[key] = value
    kwargs["sql"] = query.strip().rstrip(";").rstrip()
    return ("execute" if write else "query"), kwargs


def _text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def shape(result, write):
    """
    Salt's return shape: a list of dicts, column name to text (``None`` for NULL, ``t``/``f`` for booleans).

    ``write=True`` (the ``execute`` path) has no result set: one row ``{"rows_affected": "<n>"}``.
    """
    if write:
        return [{"rows_affected": str(result["rows_affected"])}]
    columns = result["columns"]
    return [dict(zip(columns, (_text(v) for v in row))) for row in result["rows"]]
