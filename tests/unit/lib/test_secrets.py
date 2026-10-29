"""Unit tests for ``saltext.sqlx.lib.secrets`` (TEST_PLAN section 3, ``test_secrets.py``).

The module has no Salt dunders: the ``sdb.get`` callable is injected, so every test here uses a fake getter.
"""

import copy
import logging
import traceback
from urllib.parse import quote

import pytest
from salt.exceptions import CommandExecutionError

from saltext.sqlx.lib import secrets

SECRET = "s3cr3t-Pa55/w0rd?@:%"
REF = "sdb://vault/orders_pg"


def _getter_returning(value):
    def getter(_ref):
        return value

    return getter


def _getter_raising(exc):
    def getter(_ref):
        raise exc

    return getter


def _chain(exc):
    """Raise ``exc`` from inside a handler so it carries ``__context__`` holding the secret."""
    try:
        raise ValueError(f"backend said password={SECRET}")
    except ValueError:
        raise exc  # pylint: disable=raise-missing-from


# Every way resolution can fail. Several deliberately put the secret into the underlying exception text.
ERROR_PATHS = {
    "getter_raises_with_secret_in_text": _getter_raising(
        RuntimeError(f"vault denied for {SECRET}")
    ),
    "getter_raises_keyerror_with_secret": _getter_raising(KeyError(SECRET)),
    "getter_raises_chained_secret": lambda _ref: _chain(RuntimeError("backend failure")),
    "getter_returns_none": _getter_returning(None),
    "getter_returns_empty": _getter_returning(""),
    "getter_returns_whitespace": _getter_returning("   "),
    "getter_returns_reference_unresolved": _getter_returning(REF),
    "getter_returns_non_string": _getter_returning(12345),
}


def _all_text(excinfo):
    exc = excinfo.value
    parts = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    return "\n".join(parts)


# --------------------------------------------------------------------------- resolve_secret


def test_non_sdb_value_passes_through_without_calling_getter():
    def getter(_ref):  # pragma: no cover - must not be called
        raise AssertionError("getter must not be called for literals")

    assert secrets.resolve_secret("literal-pw", getter) == "literal-pw"


@pytest.mark.parametrize("value", [None, "", 5, ["sdb://x"], "SDB://vault/key", " sdb://vault/key"])
def test_non_matching_values_pass_through(value):
    assert secrets.resolve_secret(value, _getter_returning("nope")) == value


def test_sdb_reference_resolved_via_injected_getter():
    seen = []

    def getter(ref):
        seen.append(ref)
        return SECRET

    assert secrets.resolve_secret(REF, getter) == SECRET
    assert seen == [REF]


@pytest.mark.parametrize("ref", ["sdb://", "sdb:///"])
def test_empty_reference_name_is_rejected_without_calling_getter(ref):
    def getter(_ref):  # pragma: no cover - must not be called
        raise AssertionError("getter must not be called")

    with pytest.raises(CommandExecutionError, match=r"^\[E_SECRET\]"):
        secrets.resolve_secret(ref, getter)


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
def test_every_error_path_raises_e_secret_naming_reference(path):
    with pytest.raises(CommandExecutionError) as excinfo:
        secrets.resolve_secret(REF, ERROR_PATHS[path])
    message = str(excinfo.value)
    assert message.startswith("[E_SECRET]")
    assert REF in message


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
def test_no_error_path_leaks_secret_in_exception_or_logs(path, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CommandExecutionError) as excinfo:
        secrets.resolve_secret(REF, ERROR_PATHS[path])
    assert SECRET not in _all_text(excinfo)
    assert SECRET not in caplog.text
    assert all(SECRET not in record.getMessage() for record in caplog.records)


def test_underlying_exception_text_is_not_chained():
    with pytest.raises(CommandExecutionError) as excinfo:
        secrets.resolve_secret(REF, _getter_raising(RuntimeError(SECRET)))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_getter_exception_class_name_is_reported():
    with pytest.raises(CommandExecutionError, match="KeyError"):
        secrets.resolve_secret(REF, _getter_raising(KeyError(SECRET)))


def test_successful_resolution_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    secrets.resolve_secret(REF, _getter_returning(SECRET))
    assert caplog.records == []


# --------------------------------------------------------------------------- resolve_profile_secrets


def test_resolve_profile_returns_copy_with_password_resolved():
    profile = {"dialect": "postgresql", "host": "db", "password": REF, "options": {"a": [1]}}
    original = copy.deepcopy(profile)
    resolved = secrets.resolve_profile_secrets(profile, _getter_returning(SECRET))
    assert resolved["password"] == SECRET
    assert resolved["host"] == "db"
    assert profile == original  # input untouched
    assert resolved is not profile
    assert resolved["options"] is not profile["options"]


def test_resolve_profile_literal_password_kept():
    resolved = secrets.resolve_profile_secrets({"password": "lit"}, _getter_returning("x"))
    assert resolved == {"password": "lit"}


def test_resolve_profile_without_password_is_plain_copy():
    profile = {"dialect": "sqlite"}
    resolved = secrets.resolve_profile_secrets(profile, _getter_returning("x"))
    assert resolved == profile
    assert resolved is not profile


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
def test_resolve_profile_error_paths_do_not_leak(path, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CommandExecutionError) as excinfo:
        secrets.resolve_profile_secrets({"password": REF}, ERROR_PATHS[path])
    assert str(excinfo.value).startswith("[E_SECRET]")
    assert SECRET not in _all_text(excinfo)
    assert SECRET not in caplog.text


# --------------------------------------------------------------------------- mask()


def test_mask_replaces_known_secrets():
    assert secrets.mask(f"connect failed with {SECRET} twice {SECRET}", [SECRET]) == (
        "connect failed with *** twice ***"
    )


def test_mask_longest_secret_first():
    assert secrets.mask("a abc", ["a", "abc"]) == "*** ***"


def test_mask_ignores_empty_and_none_secrets():
    assert secrets.mask("keep", [None, "", "zzz"]) == "keep"


def test_mask_accepts_single_string_and_generators():
    assert secrets.mask("x pw y", "pw") == "x *** y"
    assert secrets.mask("x pw y", (s for s in ["pw"])) == "x *** y"


def test_mask_non_string_text_is_stringified():
    assert secrets.mask(ValueError("bad pw"), ["pw"]) == "bad ***"


def test_mask_covers_url_encoded_and_repr_escaped_forms():
    pw = 'p@ss:w/rd"\\'
    text = f"postgresql://u:{quote(pw, safe='')}@h/db and {pw!r}"
    masked = secrets.mask(text, [pw])
    assert quote(pw, safe="") not in masked
    assert repr(pw)[1:-1] not in masked


# --------------------------------------------------------------------------- MaskingFilter


@pytest.fixture
def masked_logger():
    logger = logging.getLogger("saltext.sqlx.test_masking")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    filt = secrets.MaskingFilter()
    handler.addFilter(filt)
    logger.addHandler(handler)
    yield logger, handler, filt
    logger.removeHandler(handler)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def test_filter_is_a_logging_filter_and_never_drops_records():
    filt = secrets.MaskingFilter()
    assert isinstance(filt, logging.Filter)
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "hello", None, None)
    assert filt.filter(record) is True


@pytest.mark.parametrize(
    "template",
    [
        "password=%s",
        "password = %s",
        "password: %s",
        "PASSWORD=%s",
        "db_password=%s",
        "client_secret=%s",
        "api_token=%s",
        "access-token: %s",
        "{'password': '%s'}",
        '{"password": "%s"}',
        "{'secret': '%s', 'host': 'h'}",
        "dsn host=h password=%s port=5432",
        "https://x/?token=%s&a=b",
    ],
)
def test_filter_masks_values_of_sensitive_keys(masked_logger, template):
    logger, handler, _ = masked_logger
    logger.info(template % "hunter2")
    assert "hunter2" not in handler.lines[0]
    assert "***" in handler.lines[0]


def test_filter_masks_values_inside_args(masked_logger):
    logger, handler, _ = masked_logger
    logger.info("profile %s", {"host": "h", "password": "hunter2"})
    logger.info("profile %(p)s", {"p": {"token": "hunter2"}})
    assert all("hunter2" not in line for line in handler.lines)
    assert len(handler.lines) == 2


def test_filter_keeps_non_sensitive_text(masked_logger):
    logger, handler, _ = masked_logger
    logger.info("connecting to host=db port=5432 passwords are hidden")
    assert handler.lines == ["INFO connecting to host=db port=5432 passwords are hidden"]


def test_filter_masks_extra_attributes_named_like_secrets(masked_logger):
    logger, handler, _ = masked_logger
    seen = []
    handler.addFilter(lambda r: seen.append(r.__dict__.copy()) or True)
    logger.info("msg", extra={"password": "hunter2", "db_token": "hunter2", "host": "h"})
    assert seen[0]["password"] == "***"
    assert seen[0]["db_token"] == "***"
    assert seen[0]["host"] == "h"


def test_filter_masks_registered_secret_values_anywhere(masked_logger):
    logger, handler, filt = masked_logger
    filt.add_secret(SECRET)
    filt.add_secret(None)
    filt.add_secret("")
    logger.info("failed to connect using %s", SECRET)
    logger.info(f"no key here, just {SECRET}")
    assert all(SECRET not in line for line in handler.lines)


def test_filter_constructor_accepts_initial_secrets(masked_logger):
    logger, handler, _ = masked_logger
    filt = secrets.MaskingFilter(["abc123"])
    handler.addFilter(filt)
    logger.info("value abc123")
    assert "abc123" not in handler.lines[0]


def test_filter_masks_exception_text(masked_logger):
    logger, handler, filt = masked_logger
    filt.add_secret(SECRET)
    try:
        raise RuntimeError(f"auth failed password={SECRET} for user")
    except RuntimeError:
        logger.exception("operation failed")
    text = "\n".join(handler.lines)
    assert SECRET not in text
    assert "RuntimeError" in text
    assert "Traceback" in text


def test_filter_masks_exception_text_with_sensitive_key_only(masked_logger):
    logger, handler, _ = masked_logger
    try:
        raise RuntimeError("bad token=hunter2")
    except RuntimeError:
        logger.error("operation failed", exc_info=True)
    assert "hunter2" not in "\n".join(handler.lines)


def test_filter_masks_stack_info(masked_logger):
    _, _, filt = masked_logger
    filt.add_secret("hunter2")
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "m", None, None)
    record.stack_info = "stack password=hunter2"
    assert filt.filter(record) is True
    assert "hunter2" not in record.stack_info


def test_filter_survives_unformattable_message():
    filt = secrets.MaskingFilter()
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "%d items", ("notanint",), None)
    assert filt.filter(record) is True
    assert "hunter2" not in record.getMessage()


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
def test_filter_on_logged_error_path_exceptions(masked_logger, path):
    """Even if a caller logs the E_SECRET failure (with traceback), nothing sensitive reaches the handler."""
    logger, handler, filt = masked_logger
    filt.add_secret(SECRET)
    try:
        secrets.resolve_secret(REF, ERROR_PATHS[path])
    except CommandExecutionError:
        logger.exception("resolve failed")
    assert all(SECRET not in line for line in handler.lines)
