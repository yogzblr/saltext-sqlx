"""Call-time connection parameters: a policy-gated layer over profiles (SPEC section 3.2, decisions D1 and D16).

**Default (D16, owner decision 2026-11-01):** with no ``sqlx:_policy:allow_call_params`` in the minion config a caller
may send ``dialect``, ``driver``, ``host``, ``port``, ``database``, ``username`` and ``password`` in the call, with or
without a profile, on every function and dialect, exactly as Salt's own ``postgres.psql_query`` takes its connection in
the call. That default is the explicit object :data:`DEFAULT_POLICY`. The operator tightens it from minion config or
pillar: ``allow_call_params: []`` is strict (a connection keyword is ``[E_OPTION_UNKNOWN]``, as before D16), a shorter
list narrows it, and ``allowed_hosts`` / ``allowed_ports`` / ``allowed_dialects`` /
``allow_adhoc_connection`` / ``allow_call_literal_password`` restrict it further. An explicit list, including the empty
one, replaces the default completely. Policy lives only in minion config or pillar, is passed in by the module, and is
never read from the call. This module has no Salt dunders and imports no driver.

Hard rules that no policy and no default relaxes (SPEC section 3.2): a call can only lower ``statement_timeout_ms`` and
``connect_timeout_s``; it can never set ``allow_execute``, ``execute_verbs``, ``error_detail``, ``row_limit``,
``max_bytes`` or ``_policy`` (they are not in the schema: ``[E_OPTION_UNKNOWN]``); it can turn ``read_only`` on but never
off; ``ssl_mode`` may only tighten; TLS-owned ``query``/``connect_args`` keys cannot come from a call; an ad-hoc
connection (no profile) can never ``execute``; and the base profile's password is not sent to a retargeted connection
(``host``, ``port``, ``username`` or ``dialect`` changed) unless ``inherit_password``. Under the default (no explicit
list) a literal ``password`` is accepted; an explicit list refuses it unless ``allow_call_literal_password``.

Messages name keys only, never a value, a host, a password or an ``sdb://`` reference (R3).
"""

import collections
import copy
import fnmatch
import ipaddress
import re
import socket

from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import options
from saltext.sqlx.lib import profiles
from saltext.sqlx.lib import secrets
from saltext.sqlx.lib import tls
from saltext.sqlx.lib.dialects import DIALECT_DRIVERS

__all__ = [
    "ADHOC_NAME",
    "DEFAULT_ALLOWED_FIELDS",
    "DEFAULT_POLICY",
    "FIELDS",
    "Call",
    "build_profile",
    "overridden",
    "parse_policy",
    "prepare",
    "require_enabled",
    "resolve_secrets",
    "target",
]

#: Connection keywords a caller may supply, flat or inside ``connection`` (SPEC section 3.2).
FIELDS = (
    "dialect",
    "driver",
    "host",
    "port",
    "database",
    "username",
    "password",
    "ssl_mode",
    "ssl_certs_dir",
    "ssl_files",
    "query",
    "connect_args",
    "connect_timeout_s",
    "statement_timeout_ms",
    "read_only",
)

#: The profile name of an ad-hoc connection. Not a valid configured name for the caller: it is only set by ``prepare``.
ADHOC_NAME = "(adhoc)"

_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "oracle": 1521, "mssql": 1433}
_ADHOC_REQUIRED = ("dialect", "host", "database", "username")
_RETARGET = ("host", "port", "username", "dialect")
_SSL_RANK = {"verify-ca": 1, "verify-full": 2}
_HOST_RE = re.compile(r"[A-Za-z0-9._:-]{1,253}")
_SDB_PREFIX = secrets.SDB_PREFIX

#: Addresses that are refused even when a name or a glob allows the host. A CIDR/IP entry in ``allowed_hosts`` lifts
#: this only if it lies inside the range (``127.0.0.0/8``, ``169.254.169.254``), so ``0.0.0.0/0`` never does.
_DENIED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, includes the cloud metadata address 169.254.169.254
        "224.0.0.0/4",
        "::/128",
        "::1/128",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::254/128",  # AWS IPv6 metadata address
    )
)

#: Denied when ``allowed_hosts`` is NOT configured (the D16 default): only link-local and cloud-metadata targets.
#: Loopback and private addresses stay allowed (Salt's own ``psql_query`` allows them). Resolved addresses are checked,
#: so a name, a decimal form (``2852039166``) or an IPv4-mapped IPv6 address cannot slip through. An operator lifts
#: this by listing the address in ``allowed_hosts`` (which then restricts callers to that list, see ``_check_host``).
_COMPAT_DENIED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",  # link-local, includes the AWS/Azure/GCP/OCI metadata address 169.254.169.254
        "fe80::/10",  # IPv6 link-local
        "fd00:ec2::254/128",  # AWS IPv6 metadata address
        "100.100.100.200/32",  # Alibaba Cloud metadata address
    )
)

#: The connection fields a caller may send when the operator configured nothing (D16). ``ssl_mode``,
#: ``ssl_certs_dir``, ``ssl_files``, ``query``, ``connect_args``, ``read_only`` and the timeouts are NOT in it.
DEFAULT_ALLOWED_FIELDS = (
    "dialect",
    "driver",
    "host",
    "port",
    "database",
    "username",
    "password",
)

Call = collections.namedtuple("Call", "kwargs supplied adhoc")
Call.__doc__ = (
    "Result of :func:`prepare`: kwargs for ``options.validate``, the override fields, ad-hoc flag."
)

Policy = collections.namedtuple(
    "Policy", "allow hosts ports dialects adhoc literal_password inherit_password default"
)
Policy.__doc__ = (
    "Parsed ``sqlx:_policy``. ``hosts`` is ``None`` when no ``allowed_hosts`` is configured (any host that is not "
    "link-local or cloud metadata), otherwise the parsed list. ``default`` is true when ``allow_call_params`` was absent "
    "(the D16 compat default), false for an explicit list (including ``[]``)."
)

#: The documented default policy (D16): what applies when ``sqlx:_policy:allow_call_params`` is absent. Individual
#: ``allowed_*`` lists and the two ``allow_*`` flags in ``sqlx:_policy`` are still honoured on top of it.
DEFAULT_POLICY = Policy(
    allow=frozenset(DEFAULT_ALLOWED_FIELDS),
    hosts=None,
    ports=[],
    dialects=None,
    adhoc=True,
    literal_password=True,
    inherit_password=False,
    default=True,
)

#: The policy for ``allow_call_params: []`` (strict, the behaviour before D16): nothing may be sent in a call.
STRICT_POLICY = Policy(frozenset(), None, [], None, False, False, False, False)


def _fail(code, message, error=SaltInvocationError):
    raise error(f"[{code}] {message}")


def _bad_policy(key, why):
    _fail("E_POLICY", f"sqlx:_policy:{key} {why}")


def _norm_dialect(value):
    if not isinstance(value, str) or not value:
        _fail("E_OPTION_INVALID", "option 'dialect' must be a non-empty string")
    return "mysql" if value == "mariadb" else value


def _shown(names):
    """Caller-chosen key names for a message: escaped (no newlines or bidi controls) and bounded, as in ``options``."""
    width, count = options._NAME_WIDTH, options._NAMES_SHOWN  # pylint: disable=protected-access
    shown = ", ".join(ascii(n)[:width] for n in names[:count])
    if len(names) > count:
        shown += f" and {len(names) - count} more"
    return shown


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# -- policy -----------------------------------------------------------------------------------------------------------


def _parse_hosts(raw):
    if not isinstance(raw, list) or not raw:
        _bad_policy(
            "allowed_hosts", "must be a non-empty list of names, globs, IP addresses or CIDR ranges"
        )
    out = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            _bad_policy("allowed_hosts", "has an entry that is not a non-empty string")
        text = entry.strip().lower()
        if re.fullmatch(r"[*?]+", text):
            _bad_policy("allowed_hosts", "has an entry that matches every host")
        try:
            out.append(("net", ipaddress.ip_network(text, strict=False)))
            continue
        except ValueError:
            pass
        if "/" in text:
            _bad_policy("allowed_hosts", "has an invalid CIDR entry")
        out.append(("name", text.rstrip(".")))
    return out


def _parse_ports(raw):
    if not isinstance(raw, list):
        _bad_policy("allowed_ports", "must be a list of ports or 'low-high' ranges")
    out = []
    for entry in raw:
        if _is_int(entry) and 1 <= entry <= 65535:
            out.append((entry, entry))
        elif isinstance(entry, str) and re.fullmatch(r"\d{1,5}-\d{1,5}", entry):
            low, high = (int(p) for p in entry.split("-"))
            if not 1 <= low <= high <= 65535:
                _bad_policy("allowed_ports", "has an invalid range")
            out.append((low, high))
        else:
            _bad_policy("allowed_ports", "has an invalid entry")
    return out


def _parse_dialects(raw):
    if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
        _bad_policy("allowed_dialects", "must be a list of dialect names")
    out = set()
    for entry in raw:
        if entry not in DIALECT_DRIVERS and entry != "mariadb":
            _bad_policy("allowed_dialects", "has an unknown dialect")
        out.add("mysql" if entry == "mariadb" else entry)
    return out


def _flag(section, key, default=False):
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        _bad_policy(key, "must be true or false")
    return value


def parse_policy(config):
    """
    Parse ``sqlx:_policy`` (the ``config`` mapping holds ``profiles`` and ``_policy``) into a :class:`Policy`.

    * ``allow_call_params`` **absent**: :data:`DEFAULT_POLICY` (D16). ``allowed_hosts``, ``allowed_ports``,
      ``allowed_dialects``, ``allow_adhoc_connection`` and ``allow_call_literal_password`` in the same section still
      narrow it (``allow_adhoc_connection: false`` or ``allow_call_literal_password: false`` switch those two off).
    * ``allow_call_params`` **null or ``[]``**: :data:`STRICT_POLICY`, the feature is off and nothing else is looked at.
    * ``allow_call_params`` **a list**: it replaces the default completely. Every entry must be an explicit field name
      from :data:`FIELDS` (``"*"`` is refused); ``allowed_hosts`` is required when ``host`` can be set or ad-hoc is
      enabled; the two flags default to false, as before D16.

    Any problem is ``[E_POLICY]`` naming the key (the feature fails closed).
    """
    section = config.get("_policy") if isinstance(config, dict) else None
    if section is None:
        section = {}
    if not isinstance(section, dict):
        _fail("E_PROFILE_INVALID", "sqlx:_policy must be a mapping")
    explicit = "allow_call_params" in section
    raw = section.get("allow_call_params")
    if explicit and (raw is None or raw == []):
        return STRICT_POLICY
    if explicit:
        if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
            _bad_policy("allow_call_params", "must be a list of field names")
        unknown = [n for n in raw if n not in FIELDS]
        if unknown:
            _bad_policy(
                "allow_call_params", "must list explicit field names (no '*', no unknown name)"
            )
    adhoc = _flag(
        section, "allow_adhoc_connection", DEFAULT_POLICY.adhoc if not explicit else False
    )
    allow = frozenset(raw) if explicit else DEFAULT_POLICY.allow
    hosts = DEFAULT_POLICY.hosts
    if section.get("allowed_hosts") is not None or (explicit and ("host" in raw or adhoc)):
        hosts = _parse_hosts(section.get("allowed_hosts"))
    ports = (
        _parse_ports(section["allowed_ports"]) if section.get("allowed_ports") is not None else []
    )
    dialects = (
        _parse_dialects(section["allowed_dialects"])
        if section.get("allowed_dialects") is not None
        else None
    )
    return Policy(
        allow,
        hosts,
        ports,
        dialects,
        adhoc,
        _flag(
            section,
            "allow_call_literal_password",
            DEFAULT_POLICY.literal_password if not explicit else False,
        ),
        _flag(section, "inherit_password", False),
        not explicit,
    )


def require_enabled(config, names):
    """
    ``[E_POLICY]`` if ``names`` (call-time parameters, for the ``psql_query`` form) are given but the operator turned
    the feature off (``allow_call_params: []``). Returns the parsed :class:`Policy`.
    """
    policy = parse_policy(config)
    if names and not policy.allow:
        _fail(
            "E_POLICY",
            f"call-time connection parameters are disabled ({', '.join(sorted(names))}); "
            "the operator enables fields with sqlx:_policy:allow_call_params",
        )
    return policy


# -- splitting the call -----------------------------------------------------------------------------------------------


def prepare(function, kwargs, config):
    """
    Split the connection keywords off a call and choose the base profile.

    Returns ``Call(kwargs, supplied, adhoc)``. ``kwargs`` is what ``options.validate`` should see (``profile`` filled
    from ``sqlx:default_profile`` or the ad-hoc name when absent, connection keywords removed). ``supplied`` maps field
    names to the values the caller gave (``None`` means "not given"). With the feature off, a connection keyword is
    ``[E_OPTION_UNKNOWN]`` and a call without keywords is returned unchanged.
    """
    present = sorted(k for k in kwargs if k in FIELDS or k == "connection")
    out = dict(kwargs)
    supplied = {}
    adhoc = False
    policy = None
    if present:
        policy = parse_policy(config)
        if not policy.allow:
            _fail(
                "E_OPTION_UNKNOWN", f"unknown option(s) for sqlx.{function}: {', '.join(present)}"
            )
        conn = out.pop("connection", None)
        if conn is not None and not isinstance(conn, dict):
            _fail("E_OPTION_INVALID", "option 'connection' must be an object")
        for key in FIELDS:
            out.pop(key, None)
        conn = conn or {}
        bad = sorted(str(k) for k in conn if k not in FIELDS)
        if bad:
            _fail("E_OPTION_UNKNOWN", f"unknown key(s) in connection: {_shown(bad)}")
        both = sorted(k for k in conn if kwargs.get(k) is not None and conn[k] is not None)
        if both:
            _fail(
                "E_OPTION_INVALID",
                f"key(s) given both as options and in connection: {_shown(both)}",
            )
        for key in FIELDS:
            value = conn.get(key) if conn.get(key) is not None else kwargs.get(key)
            if value is not None:
                supplied[key] = value
    if not out.get("profile"):
        default = config.get("default_profile") if isinstance(config, dict) else None
        if default is not None and not (isinstance(default, str) and default):
            _fail("E_OPTION_INVALID", "sqlx:default_profile must be a non-empty string")
        if default:
            out["profile"] = default
        elif supplied and all(k in supplied for k in _ADHOC_REQUIRED):
            if not policy.adhoc:
                _fail(
                    "E_POLICY",
                    "ad-hoc connections are disabled (sqlx:_policy:allow_adhoc_connection); name a profile",
                )
            out["profile"] = ADHOC_NAME
            adhoc = True
        elif supplied:
            missing = [k for k in _ADHOC_REQUIRED if k not in supplied]
            _fail(
                "E_OPTION_INVALID",
                f"option 'profile' is required for sqlx.{function} unless the call sends a whole connection "
                f"(dialect, host, database, username; missing: {', '.join(missing)})",
            )
    return Call(out, supplied, adhoc)


# -- host / port / dialect gates --------------------------------------------------------------------------------------


def _lookup(resolver, host, port, listed=True):
    """
    Addresses for ``host`` from ``resolver`` (default: ``getaddrinfo``, the call the drivers make).

    None found is ``[E_CONNECT]`` for a name the policy lists and ``[E_POLICY]`` for one it does not (so an unlisted name
    is always reported as outside ``allowed_hosts``, whatever DNS says).
    """
    addresses = []
    try:
        if resolver is None:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = sorted({info[4][0] for info in infos})
        else:
            addresses = list(resolver(host, port))
    except (OSError, UnicodeError):
        addresses = []
    if not addresses:
        if not listed:
            _fail("E_POLICY", "'host' is not in sqlx:_policy:allowed_hosts")
        _fail(
            "E_CONNECT",
            "host name could not be resolved for the policy check",
            CommandExecutionError,
        )
    return addresses


def _address(text):
    """``ipaddress`` object for ``text`` (scope id removed, IPv4-mapped IPv6 unwrapped), or ``None``."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip


def _in(ip, net):
    return ip.version == net.version and ip in net


def _lifted(ip, nets):
    """True if a CIDR/IP entry lies inside the denied range that holds ``ip``."""
    for denied in _DENIED:
        if _in(ip, denied):
            return any(
                _in(ip, n) and n.version == denied.version and n.subnet_of(denied) for n in nets
            )
    return True


def _check_host_default(text, port, resolver):
    """D16 default (no ``allowed_hosts``): any host except link-local and cloud-metadata targets, by resolved address."""
    literal = _address(text)
    if literal is not None:
        addresses = [literal]
    else:
        found = _lookup(resolver, text, port if _is_int(port) else 0, True)
        addresses = [a for a in (_address(x) for x in found) if a is not None]
    if any(_in(a, net) for a in addresses for net in _COMPAT_DENIED):
        _fail(
            "E_POLICY",
            "'host' is a link-local or cloud-metadata address; the operator can allow it by "
            "listing it in sqlx:_policy:allowed_hosts",
        )


def _check_host(host, port, policy, resolver):
    if not isinstance(host, str) or not host:
        _fail("E_OPTION_INVALID", "option 'host' must be a non-empty string")
    if not _HOST_RE.fullmatch(host):
        _fail("E_POLICY", "'host' must be a plain host name or IP address")
    text = host.lower().rstrip(".")
    if policy.hosts is None:
        _check_host_default(text, port, resolver)
        return
    names = [v for k, v in policy.hosts if k == "name"]
    nets = [v for k, v in policy.hosts if k == "net"]
    name_ok = any(fnmatch.fnmatchcase(text, pattern) for pattern in names)
    literal = _address(text)
    if literal is not None:
        addresses = [literal]
        allowed = name_ok or any(_in(literal, n) for n in nets)
    else:
        if not name_ok and not nets:
            _fail(
                "E_POLICY", "'host' is not in sqlx:_policy:allowed_hosts"
            )  # an unlisted name is never looked up
        found = _lookup(resolver, text, port if _is_int(port) else 0, name_ok)
        addresses = [a for a in (_address(x) for x in found) if a is not None]
        allowed = name_ok or (
            bool(nets) and bool(addresses) and all(any(_in(a, n) for n in nets) for a in addresses)
        )
    if not allowed:
        _fail("E_POLICY", "'host' is not in sqlx:_policy:allowed_hosts")
    if not all(_lifted(a, nets) for a in addresses):
        _fail(
            "E_POLICY",
            "'host' resolves to a loopback, link-local or metadata address that allowed_hosts does not allow explicitly",
        )


def _check_port(port, policy):
    if not policy.ports or not _is_int(port):
        return
    if not any(low <= port <= high for low, high in policy.ports):
        _fail("E_POLICY", "'port' is not in sqlx:_policy:allowed_ports")


def _check_dialect(dialect, policy):
    if policy.dialects is not None and dialect not in policy.dialects:
        _fail("E_POLICY", "'dialect' is not in sqlx:_policy:allowed_dialects")


# -- merging ----------------------------------------------------------------------------------------------------------


def _text(key, value):
    if not isinstance(value, str) or not value:
        _fail("E_OPTION_INVALID", f"option '{key}' must be a non-empty string")
    return value


def _mapping(key, value):
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        _fail("E_OPTION_INVALID", f"option '{key}' must be an object with string keys")
    return value


def _lower(key, value, ceiling):
    if not _is_int(value) or value < 1:
        _fail("E_OPTION_INVALID", f"option '{key}' must be an integer >= 1")
    return min(value, ceiling)


def _driver_keys(key, value):
    owned = set().union(*tls._OWNED.values())  # pylint: disable=protected-access
    hit = sorted(k for k in value if k in owned)
    if hit:
        _fail(
            "E_POLICY",
            f"'{key}' key(s) {', '.join(hit)} cannot be set from a call; use ssl_mode and ssl_certs_dir",
        )


def _merge(raw, supplied, policy):  # pylint: disable=too-many-branches
    """Overlay ``supplied`` on the raw profile ``raw``; returns a new dict. Raises on every hard-rule violation."""
    out = copy.deepcopy(raw)
    for key in sorted(supplied):
        value = supplied[key]
        if key in ("host", "database", "username", "driver", "ssl_certs_dir"):
            out[key] = _text(key, value)
        elif key == "dialect":
            out[key] = _norm_dialect(value)
        elif key == "port":
            out[key] = value  # type and range: profiles.validate_profile
        elif key == "password":
            if not isinstance(value, str) or not value:
                _fail(
                    "E_OPTION_INVALID",
                    "option 'password' must be a non-empty string (the command line turns an all-digit "
                    "password into a number: quote it as password='\"<digits>\"')",
                )
            if not value.startswith(_SDB_PREFIX) and not policy.literal_password:
                _fail(
                    "E_POLICY",
                    "'password' from a call must be an sdb:// reference "
                    "(a literal would be stored in the job cache and on the event bus)",
                )
            out[key] = value
        elif key == "read_only":
            if not isinstance(value, bool):
                _fail("E_OPTION_INVALID", "option 'read_only' must be a boolean")
            if not value:
                _fail("E_POLICY", "'read_only' can be turned on from a call, never off")
            out[key] = True
        elif key in ("statement_timeout_ms", "connect_timeout_s"):
            default, _ = profiles._BOUNDED_INTS[key]  # pylint: disable=protected-access
            out[key] = _lower(key, value, out.get(key, default))
        elif key == "ssl_mode":
            if (
                "ssl_certs_dir" in raw
                and _SSL_RANK.get(value, 0) < _SSL_RANK[raw.get("ssl_mode", "verify-full")]
            ):
                _fail("E_POLICY", "'ssl_mode' can only be tightened from a call")
            out[key] = value
        else:  # query, connect_args, ssl_files: merged over the profile's own entries, never replacing them
            merged = dict(out.get(key) or {})
            merged.update(_mapping(key, value))
            if key != "ssl_files":
                _driver_keys(key, value)
            out[key] = merged
    return out


def _retargeted(raw, supplied):
    same = {
        k: (str(supplied[k]).lower() == str(raw.get(k)).lower()) if k in supplied else True
        for k in _RETARGET
    }
    if "dialect" in supplied:
        same["dialect"] = _norm_dialect(supplied["dialect"]) == raw.get("dialect")
    return not all(same.values())


def build_profile(call, name, config, resolver=None):
    """
    Return the validated profile for ``call``: the base profile ``name`` with the caller's fields overlaid, or an
    ad-hoc profile. Without call-time fields this is exactly ``profiles.load_profile(name, config)``.

    Every policy gate runs here, before any secret is resolved or any socket is opened. ``resolver(host, port)``
    returns address strings (tests inject it; the default is ``socket.getaddrinfo``). The result gains
    ``call_overrides`` (sorted field names, never values) and ``password_from_call``.
    """
    supplied = call.supplied
    if not supplied:
        return profiles.load_profile(name, config)
    policy = parse_policy(config)
    if call.adhoc:
        raw, allow, label = {}, policy.allow, ADHOC_NAME
    else:
        profiles.load_profile(
            name, config
        )  # unknown/invalid base profiles fail exactly as without overrides
        raw = copy.deepcopy(config["profiles"][name])
        narrowed = raw.get("allow_call_params")
        allow = policy.allow if narrowed is None else policy.allow & set(narrowed)
        label = name
    for key in sorted(supplied):
        if key not in allow:
            _fail(
                "E_POLICY", f"'{key}' may not be set from a call (sqlx:_policy:allow_call_params)"
            )

    merged = _merge(raw, supplied, policy)
    if "dialect" in supplied and "driver" not in supplied:
        merged.pop("driver", None)  # the driver follows the dialect
    if call.adhoc:
        merged.setdefault(
            "port",
            _DEFAULT_PORTS[merged["dialect"]] if merged["dialect"] in _DEFAULT_PORTS else None,
        )
    elif not policy.inherit_password and "password" not in supplied and _retargeted(raw, supplied):
        merged.pop("password", None)  # never send the profile's credentials to a different target

    if "dialect" in supplied or call.adhoc:
        _check_dialect(merged.get("dialect"), policy)
    if "host" in supplied or "port" in supplied or call.adhoc:
        if "host" in supplied or call.adhoc:
            _check_host(merged["host"], merged.get("port"), policy, resolver)
        _check_port(merged.get("port"), policy)

    profile = profiles.validate_profile(label, merged, config.get("_policy"))
    profile["call_overrides"] = sorted(supplied)
    profile["password_from_call"] = "password" in supplied
    profile["adhoc"] = call.adhoc
    # The driver must not fall back to the minion's own environment or password file for the target the caller chose
    # (libpq reads $PGPASSWORD and ~/.pgpass); ``dialects.PostgreSQL`` honours this marker.
    profile["password_dropped"] = (
        "password" not in supplied
        and not policy.inherit_password
        and (call.adhoc or _retargeted(raw, supplied))
    )
    return profile


# -- secrets and audit helpers ----------------------------------------------------------------------------------------


def resolve_secrets(profile, getter):
    """
    Return a copy of ``profile`` with ``password`` resolved. A password the caller supplied is resolved the same way, but
    a failure never names the ``sdb://`` reference the caller gave (``[E_SECRET]`` with ``<call-supplied>``).
    """
    if not profile.get("password_from_call"):
        return secrets.resolve_profile_secrets(profile, getter)
    resolved = copy.deepcopy(profile)
    reference = resolved["password"]
    if not reference.startswith(_SDB_PREFIX):
        return resolved  # a literal allowed by allow_call_literal_password
    failure = None
    try:
        resolved["password"] = secrets.resolve_secret(reference, getter)
    except CommandExecutionError as err:
        failure = str(err).replace(reference, "<call-supplied>")
    if failure is not None:
        raise CommandExecutionError(failure)
    return resolved


def target(profile):
    """``host:port`` as it is written to the audit line (control characters removed, IPv6 in brackets)."""
    host = re.sub(r"[^\w.:\-]", "_", str(profile["host"]))
    return f"[{host}]:{profile['port']}" if ":" in host else f"{host}:{profile['port']}"


def overridden(profile):
    """The override field names of ``profile`` for the audit line (``-`` when none). Names only."""
    names = profile.get("call_overrides")
    return ",".join(names) if names else "-"


# keep the public schema of options in one place: these never become schema keys
assert not set(FIELDS) & {
    "allow_execute",
    "execute_verbs",
    "error_detail",
    "row_limit",
    "max_bytes",
}
assert options.HARD_MAX_ROWS  # options is the home of the caps this layer may only lower
