"""Load and validate sqlx profiles.

A profile is one entry of ``sqlx:profiles`` (SPEC section 3). Callers pick a profile by name; they can never set
host, port, driver, URL or ``connect_args``, because those only exist here, in operator-controlled configuration.

``password`` may be an ``sdb://`` reference. It is validated for shape but **not** resolved here (that is
``lib/secrets.py``). Messages name keys only and never contain values. This module has no Salt dunders.
"""

import copy
import re

from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import options
from saltext.sqlx.lib import tls
from saltext.sqlx.lib.dialects import DIALECT_DRIVERS  # single source of truth (SPEC section 7)

DEFAULT_EXECUTE_VERBS = ("INSERT", "UPDATE", "DELETE")

_SDB_PREFIX = "sdb://"
_VERB_RE = re.compile(r"^[A-Za-z]+$")

_REQUIRED = ("dialect", "host", "port", "database", "username")
_ALLOWED = frozenset(
    _REQUIRED
    + (
        "driver",
        "password",
        "query",
        "connect_args",
        "read_only",
        "allow_execute",
        "execute_verbs",
        "statement_timeout_ms",
        "connect_timeout_s",
        "row_limit",
        "max_bytes",
        "error_detail",
        "ssl_certs_dir",
        "ssl_mode",
        "ssl_files",
    )
)

# key -> (default, hard maximum)
_BOUNDED_INTS = {
    "statement_timeout_ms": (options.DEFAULT_TIMEOUT_MS, options.HARD_MAX_TIMEOUT_MS),
    "connect_timeout_s": (options.DEFAULT_CONNECT_TIMEOUT_S, options.HARD_MAX_CONNECT_TIMEOUT_S),
    "row_limit": (options.DEFAULT_ROWS, options.HARD_MAX_ROWS),
    "max_bytes": (options.DEFAULT_BYTES, options.HARD_MAX_BYTES),
}

_BOOLS = {"read_only": True, "allow_execute": False, "error_detail": False}


def _fail(code, message):
    raise SaltInvocationError(f"[{code}] {message}")


def _invalid(name, message):
    _fail("E_PROFILE_INVALID", f"profile '{name}': {message}")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _text(name, raw, key):
    value = raw[key]
    if not isinstance(value, str) or not value:
        _invalid(name, f"'{key}' must be a non-empty string")
    return value


def validate_profile(name, raw, policy=None):
    """
    Validate one profile dict and return a new dict with defaults filled in and ``name`` added.

    Raises ``SaltInvocationError`` ``[E_PROFILE_INVALID]`` for a missing or unknown key, a wrong type, a value above
    a hard maximum, or a dialect/driver pair that SPEC section 7 does not allow. The input is not modified.
    ``policy`` is the ``sqlx:_policy`` mapping; it is only consulted when the profile sets ``ssl_certs_dir``
    (``lib/tls.py``).
    """
    if not isinstance(raw, dict):
        _invalid(name, "must be a mapping")

    unknown = sorted(str(k) for k in raw if k not in _ALLOWED)
    if unknown:
        _invalid(name, f"unknown key(s): {', '.join(unknown)}")
    missing = [k for k in _REQUIRED if raw.get(k) is None]
    if missing:
        _invalid(name, f"missing required key(s): {', '.join(missing)}")

    out = {"name": name}

    dialect = _text(name, raw, "dialect")
    if dialect not in DIALECT_DRIVERS:
        _invalid(name, f"'dialect' must be one of: {', '.join(sorted(DIALECT_DRIVERS))}")
    out["dialect"] = dialect

    driver = raw.get("driver", DIALECT_DRIVERS[dialect])
    if driver != DIALECT_DRIVERS[dialect]:
        _invalid(
            name,
            f"'driver' does not match dialect '{dialect}' (expected '{DIALECT_DRIVERS[dialect]}')",
        )
    out["driver"] = driver

    out["host"] = _text(name, raw, "host")
    out["database"] = _text(name, raw, "database")
    out["username"] = _text(name, raw, "username")

    port = raw["port"]
    if not _is_int(port) or not 1 <= port <= 65535:
        _invalid(name, "'port' must be an integer between 1 and 65535")
    out["port"] = port

    password = raw.get("password")
    if "password" in raw and password is None:
        # Salt's config.get resolves sdb:// itself and turns an unresolvable reference into None. Treating that as
        # "no password" would connect without one and hide the real cause (found by the T10 integration suite).
        _fail(
            "E_SECRET",
            f"profile '{name}': 'password' is present but null (an sdb:// reference that did not resolve?); "
            "omit the key for no password",
        )
    if password is not None:
        if not isinstance(password, str) or not password or password == _SDB_PREFIX:
            _invalid(name, "'password' must be a non-empty string or a complete sdb:// reference")
        out["password"] = password

    query = raw.get("query", {})
    if query is None:
        query = {}
    if not isinstance(query, dict) or not all(
        isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in query.items()
    ):
        _invalid(name, "'query' must be a mapping of string keys to scalar values")
    out["query"] = copy.deepcopy(query)

    connect_args = raw.get("connect_args", {})
    if connect_args is None:
        connect_args = {}
    if not isinstance(connect_args, dict) or not all(isinstance(k, str) for k in connect_args):
        _invalid(name, "'connect_args' must be a mapping with string keys")
    out["connect_args"] = copy.deepcopy(connect_args)

    for key, default in _BOOLS.items():
        value = raw.get(key, default)
        if not isinstance(value, bool):
            _invalid(name, f"'{key}' must be a boolean")
        out[key] = value

    verbs = raw.get("execute_verbs", list(DEFAULT_EXECUTE_VERBS))
    if not isinstance(verbs, list) or not all(
        isinstance(v, str) and _VERB_RE.match(v) for v in verbs
    ):
        _invalid(name, "'execute_verbs' must be a list of single-word strings")
    out["execute_verbs"] = [v.upper() for v in verbs]

    for key, (default, maximum) in _BOUNDED_INTS.items():
        value = raw.get(key, default)
        if not _is_int(value) or value < 1:
            _invalid(name, f"'{key}' must be an integer >= 1")
        if value > maximum:
            _invalid(name, f"'{key}' exceeds the hard maximum of {maximum}")
        out[key] = value

    resolved = tls.validate(name, raw, dialect, policy)  # SPEC section 7 / D12; checks the files, no network
    if resolved is not None:
        out["ssl_certs_dir"] = raw["ssl_certs_dir"]
        out["tls"] = resolved

    return out


def load_profile(name, config):
    """
    Return the validated profile ``name`` from ``config`` (the dict from ``config.get("sqlx")``).

    ``[E_PROFILE_UNKNOWN]`` when the name is not configured (other profile names are not disclosed);
    ``[E_PROFILE_INVALID]`` when the profile or the ``profiles`` section is malformed. Only the requested profile
    is validated. The returned dict is a fresh copy.
    """
    section = config.get("profiles") if isinstance(config, dict) else None
    if section is None:
        section = {}
    if not isinstance(section, dict):
        _fail("E_PROFILE_INVALID", "sqlx:profiles must be a mapping")
    if not isinstance(name, str) or not name or name not in section:
        _fail("E_PROFILE_UNKNOWN", "profile is not configured")
    return validate_profile(name, section[name], config.get("_policy"))
