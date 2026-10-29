"""Convert database values to plain, strict JSON (SPEC section 9, ARCHITECTURE section 4).

This module has no Salt dunders and never touches a database. Everything it returns survives
``json.dumps(..., allow_nan=False)``.

Conversions:

* ``None``, ``bool``, ``str``, finite ``float`` and ``int`` (any size) are unchanged.
* ``float`` NaN / +Inf / -Inf become the strings ``"NaN"``, ``"Infinity"``, ``"-Infinity"``.
* ``Decimal`` becomes its string form (no float rounding; NaN and infinities use the same strings).
* ``datetime`` / ``date`` / ``time`` become ISO-8601 strings; ``timedelta`` becomes ``str()``.
* ``UUID`` becomes a string.
* ``bytes`` / ``bytearray`` / ``memoryview`` become ``{"$bytes_b64": "<base64>"}``.
* ``dict`` / ``list`` / ``tuple`` are converted recursively (tuples become lists, non-string dict
  keys become ``str(key)``).
* Anything else becomes ``str(value)``.
"""

import base64
import datetime
import decimal
import json
import math
import uuid

BYTES_KEY = "$bytes_b64"


def _float(value):
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return float(value)


def serialize_value(value):  # pylint: disable=too-many-return-statements
    """Return a strict-JSON-safe equivalent of one database value."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value if type(value) is str else str.__str__(value)  # pylint: disable=unidiomatic-typecheck
    if isinstance(value, int):
        return value if type(value) is int else int(value)  # pylint: disable=unidiomatic-typecheck
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    # datetime is a subclass of date, so isoformat() on either gives the right result.
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else str(k)): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    return str(value)


def serialize_row(row):
    """Return one row as a positional list of serialised values."""
    return [serialize_value(v) for v in row]


def _json_size(obj):
    """UTF-8 byte length of the compact JSON encoding of ``obj``."""
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return len(text.encode("utf-8", "replace"))


def _cannot_fit(raw, max_bytes):
    """True when one value alone is longer than ``max_bytes``, so the row can never fit (T11 D3).

    A ``str`` of N characters needs at least N bytes plus quotes in compact JSON, and ``bytes`` of N need 4N/3 bytes
    of base64, so the full encoding (a second and third copy of a 100 MB value) is not needed to know that.
    """
    for value in raw:
        if isinstance(value, (str, bytes, bytearray, memoryview)) and len(value) > max_bytes:
            return True
    return False


def build_result(columns, raw_rows, limit, max_bytes):
    """Build ``{"columns", "rows", "row_count", "truncated"}`` from raw driver rows.

    At most ``limit`` rows are kept. ``truncated`` is true when more than ``limit`` rows were
    supplied (the runner fetches ``limit + 1``), or when adding the next row would make
    ``len(compact_json(columns)) + len(compact_json(rows))`` (UTF-8 bytes) exceed ``max_bytes``.
    Rows are added while they fit; the first row that would not fit, and everything after it, is
    dropped. If even the first row does not fit, ``rows`` is empty and ``truncated`` is true.
    """
    cols = [str(c) for c in columns]
    size = _json_size(cols) + 2  # the "[]" of an empty rows list
    rows = []
    truncated = False
    for raw in raw_rows:
        if len(rows) >= limit:
            truncated = True
            break
        if _cannot_fit(raw, max_bytes):
            truncated = True
            break
        row = serialize_row(raw)
        size += _json_size(row) + (1 if rows else 0)  # comma between rows
        if size > max_bytes:
            truncated = True
            break
        rows.append(row)
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}
