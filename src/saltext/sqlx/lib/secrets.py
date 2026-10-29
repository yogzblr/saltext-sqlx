"""Resolve ``sdb://`` references and mask secrets.

No Salt dunders here: the ``sdb.get`` callable is injected by the caller (in production
``__salt__["sdb.get"]``). Resolved values must live in local variables only (R9, SECURITY.md) and
must never reach an exception message, a log record or a return value.
"""

import copy
import logging
import re
from urllib.parse import quote
from urllib.parse import quote_plus

from salt.exceptions import CommandExecutionError

__all__ = ["MaskingFilter", "mask", "resolve_profile_secrets", "resolve_secret"]

SDB_PREFIX = "sdb://"
MASK = "***"

# ``<key><sep><value>`` where the key contains password, secret or token. Covers ``password=x``,
# ``password: x``, ``'password': 'x'``, ``"password": "x"``, ``api_token=x`` and ``?token=x&a=b``.
_SENSITIVE_KEY = r"[\w.\-]*(?:password|secret|token)[\w.\-]*"
_SENSITIVE_PAIR = re.compile(
    r"(?P<key>(?P<kq>[\"']?)" + _SENSITIVE_KEY + r"(?P=kq)\s*(?:=>|[:=])\s*)"
    r"(?P<val>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,;&})\]\"']+)",
    re.IGNORECASE,
)
_SENSITIVE_NAME = re.compile(_SENSITIVE_KEY, re.IGNORECASE)


def _fail(reference, reason):
    """Build the one ``E_SECRET`` error. ``reference`` is the sdb URI, never a resolved value."""
    return CommandExecutionError(f"[E_SECRET] cannot resolve {reference}: {reason}")


def resolve_secret(value, getter):
    """
    Resolve ``value`` if it is an ``sdb://`` reference, otherwise return it unchanged.

    ``getter`` is a callable taking the full ``sdb://`` URI and returning the secret
    (``__salt__["sdb.get"]``). Any exception, an empty or ``None`` result, a non-string result, or a
    result equal to the reference itself (what ``sdb.get`` returns for an unknown profile) raises
    ``CommandExecutionError("[E_SECRET] ...")`` naming the reference. The message carries only the
    reference and the exception class name, never the underlying exception text or the value, and
    the error is raised outside the ``except`` block so no ``__context__`` keeps that text alive.
    """
    if not isinstance(value, str) or not value.startswith(SDB_PREFIX):
        return value
    if not value[len(SDB_PREFIX) :].strip("/"):
        raise _fail(value, "empty reference name")

    failure = None
    resolved = None
    try:
        resolved = getter(value)
    except Exception as exc:  # pylint: disable=broad-except
        failure = f"sdb getter raised {type(exc).__name__}"
    if failure is None:
        if resolved is None or (isinstance(resolved, str) and not resolved.strip()):
            failure = "resolved to an empty value"
        elif not isinstance(resolved, str):
            failure = "resolved to a non-string value"
        elif resolved == value:
            failure = "reference was not resolved"
    if failure is not None:
        raise _fail(value, failure)
    return resolved


def resolve_profile_secrets(profile, getter):
    """Return a deep copy of ``profile`` with ``password`` resolved via :func:`resolve_secret`."""
    resolved = copy.deepcopy(profile)
    if "password" in resolved:
        resolved["password"] = resolve_secret(resolved["password"], getter)
    return resolved


def _variants(secret):
    """The forms in which a secret can show up in text: raw, URL-quoted, repr-escaped."""
    forms = {secret, quote(secret, safe=""), quote_plus(secret), repr(secret)[1:-1]}
    return [form for form in forms if form]


def _secret_list(secrets):
    if secrets is None:
        return []
    if isinstance(secrets, str):
        secrets = [secrets]
    return [s for s in secrets if isinstance(s, str) and s]


def mask(text, secrets):
    """Replace every known secret value in ``text`` with ``***`` (longest first)."""
    text = str(text)
    forms = set()
    for secret in _secret_list(secrets):
        forms.update(_variants(secret))
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, MASK)
    return text


def _mask_pairs(text):
    def _sub(match):
        val = match.group("val")
        if val[0] in "\"'" and len(val) >= 2:
            return f"{match.group('key')}{val[0]}{MASK}{val[0]}"
        return f"{match.group('key')}{MASK}"

    return _SENSITIVE_PAIR.sub(_sub, text)


class MaskingFilter(logging.Filter):
    """
    Logging filter that hides secrets from log records, including exception and stack text.

    It masks the value of any ``key=value`` / ``key: value`` pair whose key contains ``password``,
    ``secret`` or ``token``, any record attribute with such a name, and every value registered with
    :meth:`add_secret`. It never drops a record.
    """

    def __init__(self, secrets=None, name=""):
        super().__init__(name)
        self._secrets = set(_secret_list(secrets))

    def add_secret(self, secret):
        """Register a resolved secret value to be masked wherever it appears. Empty values are ignored."""
        if isinstance(secret, str) and secret:
            self._secrets.add(secret)

    def mask_text(self, text):
        """Apply key-based and value-based masking to ``text``."""
        return mask(_mask_pairs(str(text)), self._secrets)

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            message = str(record.msg)
        record.msg = self.mask_text(message)
        record.args = ()

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.mask_text(record.exc_text)
        # The text is cached in exc_text; dropping exc_info stops other handlers re-rendering it unmasked.
        record.exc_info = None
        if record.stack_info:
            record.stack_info = self.mask_text(record.stack_info)

        for attr in list(vars(record)):
            if _SENSITIVE_NAME.fullmatch(attr):
                setattr(record, attr, MASK)
        return True
