"""
Operator table and retry loop for the check functions (SPEC sections 4.3 and 4.4).

This module has no Salt dunders and never touches a database. The caller injects ``run``, a
zero-argument callable that returns the query-result dict of SPEC section 4.1 (``columns``,
``rows``, ``truncated``), and optionally ``clock`` and ``sleep`` so tests need no real waiting.

A failed comparison is a normal result (``ok: false``), not an exception. An exception raised
by ``run`` propagates unchanged and is never retried.
"""

import math
import re
import time
from decimal import Decimal

from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import serialize

#: Hard cap for ``retry_timeout_s`` (SPEC section 6). The caller may only lower it.
MAX_RETRY_TIMEOUT_S = 10

_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _to_number(value):
    """Return ``value`` as an exact ``Decimal``, or ``None`` if it is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        text = value.strip()
        if _NUMBER.fullmatch(text):
            return Decimal(text)
    return None


def _equal(actual, expected):
    left, right = _to_number(actual), _to_number(expected)
    if left is not None and right is not None:
        return left == right
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def _order(actual, expected):
    """Return -1, 0, 1, or ``None`` when the two values cannot be ordered."""
    left, right = _to_number(actual), _to_number(expected)
    if left is None or right is None:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return None
        left, right = actual, expected
    try:
        return (left > right) - (left < right)
    except TypeError:
        return None


def _ordered(test):
    def compare(actual, expected):
        order = _order(actual, expected)
        return order is not None and test(order)

    return compare


def _contains(actual, expected):
    if actual is None:
        return False
    if isinstance(actual, (list, tuple)):
        return any(_equal(item, expected) for item in actual)
    if isinstance(actual, dict):
        return str(expected) in actual
    return str(expected) in str(actual)


#: The operator table. Each entry is ``f(actual, expected) -> bool``.
OPERATORS = {
    "==": _equal,
    "!=": lambda actual, expected: not _equal(actual, expected),
    "<": _ordered(lambda order: order < 0),
    "<=": _ordered(lambda order: order <= 0),
    ">": _ordered(lambda order: order > 0),
    ">=": _ordered(lambda order: order >= 0),
    "contains": _contains,
    "not_contains": lambda actual, expected: not _contains(actual, expected),
    "is_null": lambda actual, expected: actual is None,
    "is_not_null": lambda actual, expected: actual is not None,
}

#: Operators that make sense when comparing a row count.
COUNT_OPERATORS = ("==", "!=", "<", "<=", ">", ">=")


def _invalid(message):
    return SaltInvocationError(f"[E_OPTION_INVALID] {message}")


def _check_operator(operator, allowed):
    if not isinstance(operator, str) or operator not in allowed:
        raise _invalid(f"operator must be one of: {', '.join(allowed)}")


def _check_index(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{name} must be a non-negative integer")


def _check_seconds(name, value, minimum_exclusive):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(f"{name} must be a number")
    if value < 0 or (minimum_exclusive and value == 0):
        raise _invalid(f"{name} must be {'greater than' if minimum_exclusive else 'at least'} 0")


def _retry(attempt, retry_timeout_s, retry_pause_s, clock, sleep):
    """
    Call ``attempt()`` until it returns a result whose ``ok`` is not ``False`` or the
    (capped) deadline passes. Returns ``(result, attempts)``.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + min(retry_timeout_s, MAX_RETRY_TIMEOUT_S)
    attempts = 0
    while True:
        attempts += 1
        result = attempt()
        if result["ok"] is not False:
            return result, attempts
        remaining = deadline - clock()
        if remaining <= 0:
            return result, attempts
        sleep(min(retry_pause_s, remaining))


def _finish(outcome, operator, expected, attempts):
    result = {
        "ok": outcome["ok"],
        "actual": outcome["actual"],
        # NaN / Infinity are not valid JSON (T11 D6)
        "expected": serialize.serialize_value(expected),
        "operator": operator,
        "attempts": attempts,
    }
    if "reason" in outcome:
        result["reason"] = outcome["reason"]
    return result


def check_query_result(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    run,
    operator,
    expected=None,
    row=0,
    column=0,
    retry_timeout_s=0,
    retry_pause_s=1,
    clock=None,
    sleep=None,
):
    """
    Compare ``rows[row][column]`` of the query result with ``expected`` (SPEC 4.3).

    ``run`` is a zero-argument callable returning the SPEC 4.1 result dict. Returns
    ``{"ok", "actual", "expected", "operator", "attempts"}`` plus ``"reason"`` (``"no_such_cell"``)
    when the cell does not exist. ``is_null`` and ``is_not_null`` ignore ``expected``.
    """
    _check_operator(operator, tuple(OPERATORS))
    _check_index("row", row)
    _check_index("column", column)
    _check_seconds("retry_timeout_s", retry_timeout_s, False)
    _check_seconds("retry_pause_s", retry_pause_s, True)
    compare = OPERATORS[operator]

    def attempt():
        rows = run()["rows"]
        if row >= len(rows) or column >= len(rows[row]):
            return {"ok": False, "actual": None, "reason": "no_such_cell"}
        actual = rows[row][column]
        return {"ok": compare(actual, expected), "actual": actual}

    outcome, attempts = _retry(attempt, retry_timeout_s, retry_pause_s, clock, sleep)
    return _finish(outcome, operator, expected, attempts)


def check_row_count(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    run,
    operator,
    expected,
    retry_timeout_s=0,
    retry_pause_s=1,
    clock=None,
    sleep=None,
):
    """
    Compare ``len(rows)`` of the query result with ``expected`` (SPEC 4.4).

    Only the comparison operators ``==  !=  <  <=  >  >=`` apply. If the result is
    ``truncated`` the count is unknown: ``ok`` is ``None`` with ``reason: "truncated"`` and
    ``actual`` is ``None``; this is final and is not retried.
    """
    _check_operator(operator, COUNT_OPERATORS)
    _check_seconds("retry_timeout_s", retry_timeout_s, False)
    _check_seconds("retry_pause_s", retry_pause_s, True)
    compare = OPERATORS[operator]

    def attempt():
        result = run()
        if result.get("truncated"):
            return {"ok": None, "actual": None, "reason": "truncated"}
        count = len(result["rows"])
        return {"ok": compare(count, expected), "actual": count}

    outcome, attempts = _retry(attempt, retry_timeout_s, retry_pause_s, clock, sleep)
    return _finish(outcome, operator, expected, attempts)
