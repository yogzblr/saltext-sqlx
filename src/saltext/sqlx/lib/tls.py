"""Certificate-directory TLS for profiles (SPEC section 7, decision D12).

A profile may name a directory with ``ssl_certs_dir``. The directory must resolve under
``sqlx:_policy:ssl_certs_dir_root`` (minion config or pillar; the caller of this module passes the mapping in). This
module checks the directory and the files in it **at profile load time, before any network call**, and later turns
the result into the driver arguments for each dialect. Callers still can never set connection details (R4): the TLS
arguments below are produced here and handed to the adapters, which keep their default-deny allowlist for everything
that comes from a profile's ``query`` / ``connect_args``.

Rules kept here:

* File **contents are never read** (not even to check the format) and never appear in a message; messages name a file
  by its plain name only, never by path. ``scrub`` removes paths from driver messages as a second line of defence.
* A file symlink is followed with ``realpath`` and the target must still be under the root.
* A client private key is accepted only after its Unix file mode passes ``key_mode_ok``; Windows has no such modes,
  so a Windows minion refuses every client key (``[E_POLICY]``). A CA-only directory works there.
* No driver imports and no Salt dunders (R15). The only Salt import is the exception class, as in ``profiles.py``.

Oracle TLS is UNVERIFIED(T1b): Oracle ran in T1b, but without a TCPS listener; see ``OracleTLS`` below. SQL Server is
refused in ``validate`` (observed end to end in T1b).
"""

import os
import re
import stat

from salt.exceptions import SaltInvocationError

__all__ = [
    "POLICY_KEY",
    "SSL_MODES",
    "connect_args",
    "key_mode_ok",
    "query_args",
    "scrub",
    "validate",
]

POLICY_KEY = "ssl_certs_dir_root"
SSL_MODES = ("verify-full", "verify-ca")
_WEAK_MODES = ("disable", "prefer", "allow")
_DEFAULT_MODE = "verify-full"
# os.stat on Windows reports only the read-only bit, never who may read a file, so a key's access cannot be checked.
_WINDOWS = os.name == "nt"

#: role -> candidate default file names (first is the documented one; the second is the unambiguous fallback)
_DEFAULTS = {
    "ca": ("ca.pem", "ca.crt"),
    "cert": ("client.pem", "client.crt"),
    "key": ("client.key",),
}
_ROLES = tuple(_DEFAULTS)
_LABELS = {"ca": "CA", "cert": "client certificate", "key": "private key"}

#: UNVERIFIED(T1b): python-oracledb thin reads the PEM wallet ``ewallet.pem`` from ``wallet_location``.
_ORACLE_WALLET = "ewallet.pem"

#: Keys that the TLS layer owns per dialect. A profile that sets one of them in ``query`` / ``connect_args`` next to
#: ``ssl_certs_dir`` would contradict (or silently weaken) the directory, so it is refused.
_OWNED = {
    "postgresql": frozenset({"sslmode", "sslrootcert", "sslcert", "sslkey", "sslcertmode"}),
    "mysql": frozenset(
        {"ssl_ca", "ssl_cert", "ssl_key", "ssl_verify_cert", "ssl_verify_identity", "ssl_disabled"}
    ),
    "oracle": frozenset({"ssl_server_dn_match", "protocol", "wallet_location"}),
}

_SEP_RE = re.compile(r"[\\/\x00]")
_ABS_PATH_RE = re.compile(r"(?<![\w.:~/-])/(?:[\w.+@=,-]+/)+([\w.+@=,-]+)")


def _fail(code, name, message):
    raise SaltInvocationError(f"[{code}] profile '{name}': {message}")


def _invalid(name, message):
    _fail("E_PROFILE_INVALID", name, message)


def _policy_error(name, message):
    _fail("E_POLICY", name, message)


def key_mode_ok(st):
    """
    True if a private key's ``os.stat`` result is acceptable.

    The rule is libpq's: owner-only access (``u=rw`` or less), or ``u=rw,g=r`` (0640) or less when the file is owned
    by root (the usual ``root:ssl-cert`` layout). Any world access, or group write/execute, is refused; the owner's
    own bits are not judged. Reading the mode never reads the file.
    """
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o037 or mode & 0o007:  # group write/execute, any world access
        return False
    if mode & 0o040:  # group read only: libpq accepts it for root-owned files
        return st.st_uid == 0
    return True


def _under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _openable(path):
    """Open and close ``path`` read-only (no read): proves the minion's user can open it."""
    try:
        os.close(os.open(path, os.O_RDONLY))
    except OSError:
        return False
    return True


def _plain_name(name, role, value):
    if not isinstance(value, str) or not value or value in (".", "..") or _SEP_RE.search(value):
        _invalid(
            name,
            f"'ssl_files.{role}' must be a plain file name inside ssl_certs_dir (no slashes, no '..')",
        )
    return value


def _ssl_files(name, raw, dialect):
    files = raw.get("ssl_files")
    if files is None:
        return {}
    if not isinstance(files, dict):
        _invalid(name, "'ssl_files' must be a mapping with the keys ca, cert and key")
    unknown = sorted(str(k) for k in files if k not in _ROLES)
    if unknown:
        _invalid(name, f"unknown 'ssl_files' key(s): {', '.join(unknown)} (allowed: ca, cert, key)")
    if files and dialect == "oracle":
        _invalid(
            name,
            f"'ssl_files' is not used for oracle (the wallet directory holds {_ORACLE_WALLET})",
        )
    return {role: _plain_name(name, role, value) for role, value in files.items()}


def _mode(name, raw):
    mode = raw.get("ssl_mode", _DEFAULT_MODE)
    if mode in _WEAK_MODES:
        _invalid(name, f"'ssl_mode: {mode}' is not allowed together with ssl_certs_dir")
    if mode == "require":
        _invalid(
            name,
            "'ssl_mode: require' is not offered with ssl_certs_dir: it encrypts without verifying the server on "
            "MySQL/MariaDB and behaves as verify-ca on PostgreSQL (libpq) - use verify-ca or verify-full",
        )
    if not isinstance(mode, str) or mode not in SSL_MODES:
        _invalid(name, f"'ssl_mode' must be one of: {', '.join(SSL_MODES)}")
    return mode


def _conflicts(name, raw, dialect):
    owned = _OWNED.get(dialect, frozenset())
    for where in ("query", "connect_args"):
        section = raw.get(where)
        if isinstance(section, dict):
            hit = sorted(str(k) for k in section if k in owned)
            if hit:
                _invalid(
                    name,
                    f"'{where}' key(s) {', '.join(hit)} cannot be combined with ssl_certs_dir; "
                    "the TLS settings come from ssl_mode and the directory",
                )


def _policy_root(name, policy):
    if policy is not None and not isinstance(policy, dict):
        _invalid(name, "sqlx:_policy must be a mapping")
    root = (policy or {}).get(POLICY_KEY)
    if root is None:
        _policy_error(name, f"ssl_certs_dir is set but sqlx:_policy:{POLICY_KEY} is not configured")
    if not isinstance(root, str) or not os.path.isabs(root):
        _policy_error(name, f"sqlx:_policy:{POLICY_KEY} must be an absolute path")
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        _policy_error(name, f"sqlx:_policy:{POLICY_KEY} is not an existing directory")
    return real


def _directory(name, raw, root):
    given = raw["ssl_certs_dir"]
    if not isinstance(given, str) or not given or "\x00" in given or not os.path.isabs(given):
        _invalid(name, "'ssl_certs_dir' must be an absolute directory path")
    if ".." in given.replace(os.altsep or os.sep, os.sep).split(os.sep):  # Windows takes "/" too
        _invalid(name, "'ssl_certs_dir' must not contain '..'")
    real = os.path.realpath(given)
    if not _under(real, root):
        _policy_error(name, f"ssl_certs_dir resolves outside sqlx:_policy:{POLICY_KEY}")
    if not os.path.isdir(real):
        _invalid(name, "'ssl_certs_dir' does not exist or is not a directory")
    return real


def _locate(name, directory, root, role, filename):
    """Return the real path of ``directory/filename`` or ``None`` when it does not exist. Policy checks included."""
    path = os.path.join(directory, filename)
    if not os.path.lexists(path):
        return None
    real = os.path.realpath(path)
    if not _under(real, root):
        _policy_error(name, f"{role} file '{filename}' resolves outside sqlx:_policy:{POLICY_KEY}")
    if not os.path.exists(real):  # dangling symlink
        return None
    if not stat.S_ISREG(os.stat(real).st_mode):
        _invalid(name, f"{role} file '{filename}' is not a regular file")
    if not _openable(real):
        _invalid(name, f"{role} file '{filename}' is not readable by the minion")
    return real


def _default(name, directory, root, role):
    """Look for the default names of ``role``; two present at once is ambiguous."""
    found = [
        (fname, path)
        for fname in _DEFAULTS[role]
        for path in [_locate(name, directory, root, role, fname)]
        if path
    ]
    if len(found) > 1:
        names = " and ".join(f"'{fname}'" for fname, _ in found)
        _invalid(name, f"both {names} exist; name the one to use in ssl_files.{role}")
    return found[0] if found else (_DEFAULTS[role][0], None)


def _files(name, directory, root, wanted, dialect):
    if dialect == "oracle":
        wallet = _locate(name, directory, root, "wallet", _ORACLE_WALLET)
        if wallet is None:
            _invalid(name, f"wallet file '{_ORACLE_WALLET}' not found in ssl_certs_dir")
        return {"ca": wallet, "cert": None, "key": None}
    paths, shown = {}, {}
    for role in _ROLES:
        if role in wanted:
            shown[role] = wanted[role]
            paths[role] = _locate(name, directory, root, role, wanted[role])
            if paths[role] is None:
                _invalid(name, f"{_LABELS[role]} file '{wanted[role]}' not found in ssl_certs_dir")
        else:
            shown[role], paths[role] = _default(name, directory, root, role)
    if paths["ca"] is None:
        _invalid(name, f"CA file '{shown['ca']}' not found in ssl_certs_dir")
    if bool(paths["cert"]) != bool(paths["key"]) or bool(wanted.get("cert")) != bool(
        wanted.get("key")
    ):
        _invalid(
            name,
            f"client certificate '{shown['cert']}' and private key '{shown['key']}' must both exist "
            "(a client certificate needs its key and a key needs its certificate)",
        )
    if paths["key"] and _WINDOWS:
        _policy_error(
            name,
            f"private key file '{shown['key']}' cannot be used on a Windows minion, where its access cannot be "
            "checked; use a directory without a client certificate and key (server verification only)",
        )
    if paths["key"] and not key_mode_ok(os.stat(paths["key"])):
        _policy_error(
            name,
            f"private key file '{shown['key']}' is group- or world-accessible; "
            "run chmod 600 (or 640 when owned by root)",
        )
    return paths


def validate(name, raw, dialect, policy):
    """
    Validate the TLS keys of profile ``raw`` and return the resolved settings, or ``None`` if there are none.

    The result is ``{"mode", "dir", "ca", "cert", "key"}`` with absolute real paths (``cert`` and ``key`` are ``None``
    for server authentication only). ``policy`` is the ``sqlx:_policy`` mapping or ``None``. Raises
    ``SaltInvocationError``: ``[E_PROFILE_INVALID]`` for shape, missing or unusable files and unsupported dialects,
    ``[E_POLICY]`` for a missing or violated root and for a key file with group or world access.
    """
    if "ssl_certs_dir" not in raw:
        if raw.get("ssl_mode") is not None or raw.get("ssl_files") is not None:
            _invalid(name, "'ssl_mode' and 'ssl_files' need 'ssl_certs_dir'")
        return None
    if dialect == "mssql":
        # pymssql has no argument for a CA file or a client certificate (encryption=off|request|require only), and
        # FreeTDS reads its own configuration file, which this module does not manage.
        _invalid(name, "'ssl_certs_dir' is not supported for mssql yet")
    mode = _mode(name, raw)
    wanted = _ssl_files(name, raw, dialect)
    _conflicts(name, raw, dialect)
    root = _policy_root(name, policy)
    directory = _directory(name, raw, root)
    paths = _files(name, directory, root, wanted, dialect)
    return {"mode": mode, "dir": directory, **paths}


# -- driver arguments --------------------------------------------------------------------------------------------------


def _unresolved(profile):
    if profile.get("ssl_certs_dir") and not profile.get("tls"):
        raise SaltInvocationError(
            "[E_PROFILE_INVALID] TLS settings were not resolved for this profile (refusing to connect without them)"
        )


def query_args(dialect, profile):
    """URL query arguments produced by the TLS layer for ``dialect`` (empty without TLS). Not caller-settable."""
    _unresolved(profile)
    conf = profile.get("tls")
    if not conf or dialect != "postgresql":
        return {}
    args = {"sslmode": conf["mode"], "sslrootcert": conf["ca"]}
    if conf["cert"]:
        args.update(sslcert=conf["cert"], sslkey=conf["key"])
    else:
        # libpq would otherwise pick up ~/.postgresql/postgresql.crt or PGSSLCERT and present an identity nobody chose
        args["sslcertmode"] = "disable"
    return args


def connect_args(dialect, profile):
    """Driver ``connect_args`` produced by the TLS layer for ``dialect`` (empty without TLS). Not caller-settable."""
    _unresolved(profile)
    conf = profile.get("tls")
    if not conf:
        return {}
    if dialect == "mysql":
        # Verified: SQLAlchemy moves URL-query ssl_ca/ssl_cert/ssl_key into PyMySQL's deprecated ``ssl`` dict and then
        # PyMySQL ignores that dict as soon as any ssl_verify_* keyword is also given, so everything is passed as
        # connect arguments, which PyMySQL takes natively.
        args = {
            "ssl_ca": conf["ca"],
            "ssl_verify_cert": True,
            "ssl_verify_identity": conf["mode"] == "verify-full",
        }
        if conf["cert"]:
            args.update(ssl_cert=conf["cert"], ssl_key=conf["key"])
        return args
    if dialect == "oracle":
        # UNVERIFIED(T1b): names from the python-oracledb thin-mode documentation; the T1b Oracle server had no TCPS listener.
        return {
            "protocol": "tcps",
            "wallet_location": conf["dir"],
            "ssl_server_dn_match": conf["mode"] == "verify-full",
        }
    return {}


# -- messages -----------------------------------------------------------------------------------------------------------------


def scrub(text, profile):
    """
    Remove file system paths from a driver message, leaving file names (libpq quotes the key path in its errors).

    The profile's own files and directory are replaced exactly; any other absolute path in a TLS profile's message is
    reduced to its last component. Profiles without TLS are returned unchanged.
    """
    conf = profile.get("tls")
    if not conf:
        return text
    for key in ("ca", "cert", "key"):
        if conf.get(key):
            text = text.replace(conf[key], os.path.basename(conf[key]))
    text = text.replace(conf["dir"] + os.sep, "").replace(conf["dir"], "")
    return _ABS_PATH_RE.sub(r"\1", text)
