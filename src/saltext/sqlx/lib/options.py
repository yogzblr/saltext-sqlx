"""Call keyword validation and hard caps.

``validate`` checks the keywords of one public function (SPEC sections 4 and 5) and returns a clean copy.
``apply_caps`` resolves ``limit``, ``timeout_ms``, ``max_bytes`` and ``retry_timeout_s`` against the profile and the
hard maxima of SPEC section 6: a call can only lower a cap, never raise it.

Failures raise ``SaltInvocationError`` whose message starts with ``[E_...]``. Messages name keys only; they never
contain values, SQL text or parameters. This module has no Salt dunders.
"""

import math

from salt.exceptions import SaltInvocationError

#: Hard maxima from SPEC section 6. Profiles and calls can only go lower.
HARD_MAX_ROWS = 10_000
HARD_MAX_TIMEOUT_MS = 300_000
HARD_MAX_BYTES = 8 * 1024 * 1024
HARD_MAX_CONNECT_TIMEOUT_S = 60
HARD_MAX_RETRY_TIMEOUT_S = 10

#: Defaults from SPEC section 6.
DEFAULT_ROWS = 500
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_BYTES = 1024 * 1024
DEFAULT_CONNECT_TIMEOUT_S = 10

# Value kinds used in the schema below.
_NAME = "name"  # non-empty str
_TEXT = "text"  # non-empty str
_OPT_TEXT = "opt_text"  # str or None
_PARAMS = "params"  # dict with str keys, or None
_POS_INT = "pos_int"  # int >= 1, or None
_NONNEG_INT = "nonneg_int"  # int >= 0
_NONNEG_NUM = "nonneg_num"  # int or float >= 0
_BOOL = "bool"
_ANY = "any"

# function -> {key: (kind, required, default)}
_SCHEMA = {
    "query": {
        "profile": (_NAME, True, None),
        "sql": (_TEXT, True, None),
        "params": (_PARAMS, False, None),
        "limit": (_POS_INT, False, None),
        "timeout_ms": (_POS_INT, False, None),
        "max_bytes": (_POS_INT, False, None),
    },
    "execute": {
        "profile": (_NAME, True, None),
        "sql": (_TEXT, True, None),
        "params": (_PARAMS, False, None),
        "dry_run": (_BOOL, False, False),
        "timeout_ms": (_POS_INT, False, None),
    },
    "check_query_result": {
        "profile": (_NAME, True, None),
        "sql": (_TEXT, True, None),
        "operator": (_TEXT, True, None),
        "expected": (_ANY, True, None),
        "params": (_PARAMS, False, None),
        "row": (_NONNEG_INT, False, 0),
        "column": (_NONNEG_INT, False, 0),
        "retry_timeout_s": (_NONNEG_NUM, False, 0),
        "retry_pause_s": (_NONNEG_NUM, False, 1),
    },
    "check_row_count": {
        "profile": (_NAME, True, None),
        "sql": (_TEXT, True, None),
        "operator": (_TEXT, True, None),
        "expected": (_ANY, True, None),
        "params": (_PARAMS, False, None),
        "retry_timeout_s": (_NONNEG_NUM, False, 0),
        "retry_pause_s": (_NONNEG_NUM, False, 1),
    },
    "ping": {
        "profile": (_NAME, True, None),
    },
    "table_list": {
        "profile": (_NAME, True, None),
        "schema": (_OPT_TEXT, False, None),
        "pattern": (_OPT_TEXT, False, None),
        "include_views": (_BOOL, False, False),
    },
}

_KIND_DESCRIPTION = {
    _NAME: "a non-empty string",
    _TEXT: "a non-empty string",
    _OPT_TEXT: "a string",
    _PARAMS: "an object with string keys",
    _POS_INT: "an integer >= 1",
    _NONNEG_INT: "an integer >= 0",
    _NONNEG_NUM: "a number >= 0",
    _BOOL: "a boolean",
}


def _fail(code, message):
    raise SaltInvocationError(f"[{code}] {message}")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid(kind, value):
    if kind in (_NAME, _TEXT):
        return isinstance(value, str) and bool(value)
    if kind == _OPT_TEXT:
        return isinstance(value, str)
    if kind == _PARAMS:
        return isinstance(value, dict) and all(isinstance(k, str) for k in value)
    if kind == _POS_INT:
        return _is_int(value) and value >= 1
    if kind == _NONNEG_INT:
        return _is_int(value) and value >= 0
    if kind == _NONNEG_NUM:
        return _is_num(value) and value >= 0 and not math.isnan(value)
    if kind == _BOOL:
        return isinstance(value, bool)
    return True  # _ANY


def validate(function, kwargs):
    """
    Validate the call keywords of one public function and return a cleaned copy.

    Keys starting with ``__`` (Salt's ``__pub_*`` and ``__kwarg__``) are dropped first. Any other key outside the
    function's allowed set is ``E_OPTION_UNKNOWN``; a missing required key or a value of the wrong type or range is
    ``E_OPTION_INVALID``. ``None`` means "not given" for optional keys. Defaults are filled in; ``limit``,
    ``timeout_ms`` and ``max_bytes`` stay ``None`` until :func:`apply_caps` resolves them against the profile.
    """
    try:
        schema = _SCHEMA[function]
    except KeyError:
        raise ValueError(f"unknown sqlx function: {function!r}") from None

    given = {k: v for k, v in kwargs.items() if not (isinstance(k, str) and k.startswith("__"))}

    unknown = sorted(str(k) for k in given if k not in schema)
    if unknown:
        _fail("E_OPTION_UNKNOWN", f"unknown option(s) for sqlx.{function}: {', '.join(unknown)}")

    out = {}
    for key, (kind, required, default) in schema.items():
        value = given.get(key)
        if key not in given or (value is None and kind != _ANY):
            if required:
                _fail("E_OPTION_INVALID", f"option '{key}' is required for sqlx.{function}")
            out[key] = default
            continue
        if not _valid(kind, value):
            _fail("E_OPTION_INVALID", f"option '{key}' must be {_KIND_DESCRIPTION[kind]}")
        out[key] = value
    return out


def _lowest(*values):
    return min(v for v in values if v is not None)


def apply_caps(opts, profile):
    """
    Resolve the effective limits for a validated call against ``profile`` (a validated profile dict).

    The effective value is the lowest of the call value, the profile value and the hard maximum, so a call can only
    lower a cap. A call value above the cap is clamped (SPEC section 3: "never raise them"). Functions without a
    matching option (``ping``, ``table_list``) are returned unchanged. Returns a new dict.
    """
    out = dict(opts)
    has_limit = "limit" in out
    has_timeout = "timeout_ms" in out
    is_check = "operator" in out

    if has_limit or is_check:
        out["limit"] = _lowest(out.get("limit"), profile.get("row_limit"), HARD_MAX_ROWS)
        out["max_bytes"] = _lowest(out.get("max_bytes"), profile.get("max_bytes"), HARD_MAX_BYTES)
    if has_timeout or is_check:
        out["timeout_ms"] = _lowest(
            out.get("timeout_ms"), profile.get("statement_timeout_ms"), HARD_MAX_TIMEOUT_MS
        )
    if "retry_timeout_s" in out:
        out["retry_timeout_s"] = min(out["retry_timeout_s"], HARD_MAX_RETRY_TIMEOUT_S)
    return out
