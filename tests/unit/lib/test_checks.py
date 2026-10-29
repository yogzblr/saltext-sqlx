"""
Unit tests for ``saltext.sqlx.lib.checks`` (SPEC sections 4.3 and 4.4).
"""

import json

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import checks


def _result(rows, columns=None, truncated=False):
    width = len(rows[0]) if rows else 0
    return {
        "columns": columns if columns is not None else [f"c{i}" for i in range(width)],
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


class FakeTime:
    """A clock whose time moves only when ``sleep`` is called."""

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Runner:
    """Returns the queued results in order, repeating the last one."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


@pytest.fixture
def fake_time():
    return FakeTime()


def check_value(value, operator, expected=None, **kwargs):
    return checks.check_query_result(
        lambda: _result([[value]]), operator, expected, **kwargs
    )


# ---------------------------------------------------------------- operators


@pytest.mark.parametrize(
    "actual,operator,expected,outcome",
    [
        (5, "==", 5, True),
        (5, "==", 6, False),
        ("abc", "==", "abc", True),
        ("abc", "==", "abd", False),
        (5, "!=", 6, True),
        (5, "!=", 5, False),
        (5, "<", 6, True),
        (5, "<", 5, False),
        (5, "<=", 5, True),
        (5, "<=", 4, False),
        (5, ">", 4, True),
        (5, ">", 5, False),
        (5, ">=", 5, True),
        (5, ">=", 6, False),
        ("apple", "<", "banana", True),
        ("banana", "<", "apple", False),
        ("hello world", "contains", "lo wo", True),
        ("hello world", "contains", "xyz", False),
        ("hello world", "not_contains", "xyz", True),
        ("hello world", "not_contains", "hello", False),
        ([1, 2, 3], "contains", 2, True),
        ([1, 2, 3], "contains", 9, False),
        ({"a": 1}, "contains", "a", True),
        ({"a": 1}, "contains", "b", False),
        (12345, "contains", 234, True),
        (None, "contains", "x", False),
        (None, "not_contains", "x", True),
        (None, "is_null", None, True),
        (0, "is_null", None, False),
        ("", "is_null", None, False),
        (None, "is_not_null", None, False),
        (0, "is_not_null", None, True),
        ("x", "is_not_null", None, True),
    ],
)
def test_operators(actual, operator, expected, outcome):
    result = check_value(actual, operator, expected)
    assert result["ok"] is outcome
    assert result["actual"] == actual
    assert result["expected"] == expected
    assert result["operator"] == operator
    assert result["attempts"] == 1
    assert "reason" not in result


def test_is_null_does_not_need_expected():
    result = checks.check_query_result(lambda: _result([[None]]), "is_null")
    assert result == {
        "ok": True,
        "actual": None,
        "expected": None,
        "operator": "is_null",
        "attempts": 1,
    }


def test_none_with_equality_and_ordering():
    assert check_value(None, "==", None)["ok"] is True
    assert check_value(None, "==", 1)["ok"] is False
    assert check_value(None, "!=", 1)["ok"] is True
    assert check_value(None, "<", 1)["ok"] is False
    assert check_value(None, ">=", 1)["ok"] is False


def test_incomparable_types_are_a_failed_comparison_not_an_exception():
    assert check_value("abc", "<", 5)["ok"] is False
    assert check_value([1], ">", 5)["ok"] is False
    assert check_value({"a": 1}, "==", "x")["ok"] is False


def test_result_is_json_serialisable():
    result = check_value("12.50", "==", "12.5")
    json.dumps(result)


# -------------------------------------------------------- numeric strings


@pytest.mark.parametrize(
    "actual,operator,expected,outcome",
    [
        ("12.50", "==", "12.5", True),
        ("12.50", "==", 12.5, True),
        ("12.50", "==", 12, False),
        ("10", ">", "9", True),  # numeric, not lexicographic
        ("10", "<", "9", False),
        ("9", "<=", "10", True),
        (" 7 ", "==", 7, True),
        (7, "==", "7.0", True),
        ("1e3", "==", 1000, True),
        ("-3", "<", "2", True),
        ("0.1", "==", 0.1, True),  # no binary float noise
        ("12345678901234567890.1", ">", "12345678901234567890.0", True),
        ("abc", "==", "abc", True),
        ("10", "==", "ten", False),
        ("1_000", "==", 1000, False),  # underscores are not numbers
        ("nan", "==", "nan", True),  # equal as plain strings
        ("nan", ">", 1, False),
        ("inf", ">", 1, False),
        ("", "==", 0, False),
        (True, "==", 1, False),  # a bool is not a number
    ],
)
def test_numeric_string_comparison(actual, operator, expected, outcome):
    assert check_value(actual, operator, expected)["ok"] is outcome


def test_numeric_ordering_falls_back_to_strings_when_one_side_is_not_numeric():
    assert check_value("10", "<", "9a")["ok"] is True  # "10" < "9a" as text


# --------------------------------------------------------- cell selection


def test_row_and_column_selection():
    rows = [[1, 2, 3], [4, 5, 6]]
    result = checks.check_query_result(lambda: _result(rows), "==", 6, row=1, column=2)
    assert result["ok"] is True
    assert result["actual"] == 6


@pytest.mark.parametrize("row,column", [(5, 0), (0, 9), (1, 0)])
def test_missing_cell(row, column):
    result = checks.check_query_result(
        lambda: _result([[1, 2]]), "==", 1, row=row, column=column
    )
    assert result == {
        "ok": False,
        "actual": None,
        "expected": 1,
        "operator": "==",
        "attempts": 1,
        "reason": "no_such_cell",
    }


def test_empty_result_is_no_such_cell():
    result = checks.check_query_result(lambda: _result([]), "is_null", None)
    assert result["ok"] is False
    assert result["reason"] == "no_such_cell"
    assert result["actual"] is None


def test_missing_cell_is_not_satisfied_by_is_null():
    result = checks.check_query_result(lambda: _result([]), "is_null")
    assert result["ok"] is False


# ------------------------------------------------------------- row count


@pytest.mark.parametrize(
    "operator,expected,outcome",
    [
        ("==", 3, True),
        ("==", "3", True),
        ("!=", 3, False),
        ("<", 4, True),
        ("<=", 3, True),
        (">", 3, False),
        (">=", 3, True),
    ],
)
def test_row_count_operators(operator, expected, outcome):
    result = checks.check_row_count(lambda: _result([[1], [2], [3]]), operator, expected)
    assert result == {
        "ok": outcome,
        "actual": 3,
        "expected": expected,
        "operator": operator,
        "attempts": 1,
    }


def test_row_count_zero_rows():
    result = checks.check_row_count(lambda: _result([]), "==", 0)
    assert result["ok"] is True
    assert result["actual"] == 0


def test_row_count_truncated_is_unknown():
    result = checks.check_row_count(
        lambda: _result([[1], [2]], truncated=True), ">=", 1
    )
    assert result == {
        "ok": None,
        "actual": None,
        "expected": 1,
        "operator": ">=",
        "attempts": 1,
        "reason": "truncated",
    }


def test_row_count_truncated_is_not_retried(fake_time):
    runner = Runner(_result([[1]], truncated=True))
    result = checks.check_row_count(
        runner,
        "==",
        5,
        retry_timeout_s=5,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is None
    assert runner.calls == 1
    assert fake_time.sleeps == []


@pytest.mark.parametrize("operator", ["contains", "not_contains", "is_null", "is_not_null"])
def test_row_count_rejects_non_comparison_operators(operator):
    runner = Runner(_result([[1]]))
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        checks.check_row_count(runner, operator, 1)
    assert runner.calls == 0


# ----------------------------------------------------------- retry loop


def test_no_retry_by_default(fake_time):
    runner = Runner(_result([[1]]))
    result = checks.check_query_result(
        runner, "==", 2, clock=fake_time.clock, sleep=fake_time.sleep
    )
    assert result["ok"] is False
    assert result["attempts"] == 1
    assert runner.calls == 1
    assert fake_time.sleeps == []


def test_retry_until_pass(fake_time):
    runner = Runner(_result([[0]]), _result([[1]]), _result([[2]]))
    result = checks.check_query_result(
        runner,
        "==",
        2,
        retry_timeout_s=10,
        retry_pause_s=1,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is True
    assert result["actual"] == 2
    assert result["attempts"] == 3
    assert fake_time.sleeps == [1, 1]


def test_pass_on_first_attempt_does_not_sleep(fake_time):
    runner = Runner(_result([[2]]))
    result = checks.check_query_result(
        runner,
        "==",
        2,
        retry_timeout_s=10,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["attempts"] == 1
    assert fake_time.sleeps == []


def test_retry_gives_up_at_the_deadline_with_the_last_result(fake_time):
    runner = Runner(_result([[0]]), _result([[1]]))
    result = checks.check_query_result(
        runner,
        "==",
        99,
        retry_timeout_s=3,
        retry_pause_s=1,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is False
    assert result["actual"] == 1  # the last observed value
    assert result["attempts"] == 4  # t=0,1,2,3
    assert fake_time.sleeps == [1, 1, 1]
    assert fake_time.now == 103.0


def test_last_pause_is_shortened_to_the_deadline(fake_time):
    runner = Runner(_result([[0]]))
    result = checks.check_query_result(
        runner,
        "==",
        1,
        retry_timeout_s=2.5,
        retry_pause_s=1,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert fake_time.sleeps == [1, 1, 0.5]
    assert result["attempts"] == 4


def test_retry_timeout_is_capped_at_ten_seconds(fake_time):
    runner = Runner(_result([[0]]))
    result = checks.check_query_result(
        runner,
        "==",
        1,
        retry_timeout_s=3600,
        retry_pause_s=1,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert sum(fake_time.sleeps) == 10
    assert fake_time.now == 110.0
    assert result["attempts"] == 11
    assert checks.MAX_RETRY_TIMEOUT_S == 10


def test_slow_runs_count_against_the_deadline(fake_time):
    def slow():
        fake_time.now += 4  # each query takes four seconds
        return _result([[0]])

    result = checks.check_query_result(
        slow,
        "==",
        1,
        retry_timeout_s=10,
        retry_pause_s=1,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    # t=4 (attempt 1), sleep 1, t=9 (attempt 2), sleep 1, t=14 (attempt 3), past deadline
    assert result["attempts"] == 3


def test_missing_cell_is_retried_until_it_appears(fake_time):
    runner = Runner(_result([]), _result([]), _result([[7]]))
    result = checks.check_query_result(
        runner,
        "==",
        7,
        retry_timeout_s=5,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is True
    assert result["attempts"] == 3


def test_missing_cell_at_the_deadline_keeps_the_reason(fake_time):
    runner = Runner(_result([]))
    result = checks.check_query_result(
        runner,
        "==",
        7,
        retry_timeout_s=2,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is False
    assert result["reason"] == "no_such_cell"
    assert result["attempts"] == 3


def test_row_count_retry_until_pass(fake_time):
    runner = Runner(_result([]), _result([[1]]), _result([[1], [2]]))
    result = checks.check_row_count(
        runner,
        "==",
        2,
        retry_timeout_s=10,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is True
    assert result["attempts"] == 3


def test_row_count_reason_is_dropped_once_a_later_attempt_passes(fake_time):
    runner = Runner(_result([]), _result([[1]]))
    result = checks.check_query_result(
        runner,
        "==",
        1,
        retry_timeout_s=5,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    assert result["ok"] is True
    assert "reason" not in result


def test_default_clock_and_sleep_are_used_when_not_injected():
    # timeout 0: a single attempt, no waiting, real clock never consulted for sleeping
    runner = Runner(_result([[1]]))
    assert checks.check_query_result(runner, "==", 1)["ok"] is True


def test_default_clock_and_sleep_drive_a_real_retry(monkeypatch):
    sleeps = []
    ticks = iter([0.0, 0.0, 0.5, 0.5, 5.0])
    monkeypatch.setattr(checks.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(checks.time, "sleep", sleeps.append)
    runner = Runner(_result([[0]]), _result([[1]]))
    result = checks.check_query_result(runner, "==", 1, retry_timeout_s=1, retry_pause_s=0.25)
    assert result["ok"] is True
    assert result["attempts"] == 2
    assert sleeps == [0.25]


# ------------------------------------------------- exceptions propagate


class Boom(Exception):
    pass


def test_run_exception_propagates_unchanged_and_is_not_retried(fake_time):
    error = Boom("db down")
    calls = []

    def run():
        calls.append(1)
        raise error

    with pytest.raises(Boom) as excinfo:
        checks.check_query_result(
            run,
            "==",
            1,
            retry_timeout_s=10,
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
    assert excinfo.value is error
    assert len(calls) == 1
    assert fake_time.sleeps == []


def test_run_exception_on_a_later_attempt_propagates(fake_time):
    outcomes = [_result([[0]]), Boom("gone")]

    def run():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(Boom):
        checks.check_row_count(
            run,
            ">",
            5,
            retry_timeout_s=10,
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )


def test_row_count_run_exception_propagates():
    def run():
        raise Boom("x")

    with pytest.raises(Boom):
        checks.check_row_count(run, "==", 1)


# ------------------------------------------------------------ validation


@pytest.mark.parametrize("operator", ["=", "~", "CONTAINS", "", None, 5, "like"])
def test_unknown_operator(operator):
    runner = Runner(_result([[1]]))
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        checks.check_query_result(runner, operator, 1)
    assert runner.calls == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"row": -1},
        {"column": -1},
        {"row": "0"},
        {"column": 1.5},
        {"row": True},
        {"retry_timeout_s": -1},
        {"retry_timeout_s": "5"},
        {"retry_timeout_s": True},
        {"retry_timeout_s": float("nan")},
        {"retry_pause_s": 0},
        {"retry_pause_s": -1},
        {"retry_pause_s": "1"},
        {"retry_pause_s": float("inf")},
    ],
)
def test_invalid_options(kwargs):
    runner = Runner(_result([[1]]))
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        checks.check_query_result(runner, "==", 1, **kwargs)
    assert runner.calls == 0


def test_row_count_invalid_options():
    runner = Runner(_result([[1]]))
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        checks.check_row_count(runner, "==", 1, retry_timeout_s=-1)
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        checks.check_row_count(runner, "nope", 1)
    assert runner.calls == 0


def test_integer_float_like_options_are_accepted():
    result = checks.check_query_result(
        lambda: _result([[1]]), "==", 1, row=0, column=0, retry_timeout_s=0.0, retry_pause_s=0.5
    )
    assert result["ok"] is True


def test_decimal_and_non_finite_values():
    from decimal import Decimal  # pylint: disable=import-outside-toplevel

    assert check_value(Decimal("12.50"), "==", "12.5")["ok"] is True
    assert check_value(Decimal("NaN"), "==", 1)["ok"] is False
    assert check_value(float("inf"), ">", 1)["ok"] is True  # real floats order natively


def test_bool_is_not_ordered_against_numbers():
    assert check_value(True, ">", 0)["ok"] is False
    assert check_value(5, "<", True)["ok"] is False
    assert check_value(True, "==", True)["ok"] is True
