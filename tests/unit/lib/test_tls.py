"""Unit tests for saltext.sqlx.lib.tls (SPEC section 7, decision D12).

Everything here runs on throw-away directories under ``tmp_path``: the files only need to exist, because the module
never reads certificate or key contents (it checks names, location, type, permissions and openability). Real TLS
handshakes are in ``tests/functional/test_tls_functional.py``.
"""

import copy
import os
import stat
import types

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import profiles
from saltext.sqlx.lib import tls

MARKER = "-----BEGIN PRIVATE KEY-----SECRET-KEY-MATERIAL-----"

BASE = {
    "dialect": "postgresql",
    "host": "db1.internal",
    "port": 5432,
    "database": "orders",
    "username": "salt_ro",
}


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "certs"
    path.mkdir()
    return path


@pytest.fixture
def policy(root):
    return {"ssl_certs_dir_root": str(root)}


def make_dir(root, name="db1", ca="ca.pem", cert="client.pem", key="client.key", key_mode=0o600):
    """Create ``root/name`` with the given files (None = absent). Contents are a marker, never parsed."""
    path = root / name
    path.mkdir(exist_ok=True)
    for fname in (ca, cert):
        if fname:
            (path / fname).write_text("certificate")
    if key:
        (path / key).write_text(MARKER)
        (path / key).chmod(key_mode)
    return path


def prof(path, dialect="postgresql", **extra):
    raw = dict(BASE, dialect=dialect, ssl_certs_dir=str(path))
    raw.update(extra)
    return raw


def load(raw, policy):
    return profiles.validate_profile("p", copy.deepcopy(raw), policy)


def fails(code, raw, policy, *, absent=()):
    with pytest.raises(SaltInvocationError) as exc:
        load(raw, policy)
    text = str(exc.value)
    assert text.startswith(f"[{code}]"), text
    for needle in (MARKER, "SECRET-KEY-MATERIAL", *absent):
        assert needle not in text
    return text


# -- profiles without TLS are untouched -------------------------------------------------------------------------
def test_profile_without_ssl_keys_has_no_tls():
    out = profiles.validate_profile("p", dict(BASE))
    assert "tls" not in out


@pytest.mark.parametrize("extra", [{"ssl_mode": "verify-full"}, {"ssl_files": {"ca": "x.pem"}}])
def test_ssl_mode_and_files_need_a_directory(extra):
    text = fails("E_PROFILE_INVALID", dict(BASE, **extra), None)
    assert "ssl_certs_dir" in text


# -- the happy path and the default names -------------------------------------------------------------------------
def test_defaults_mtls(root, policy):
    path = make_dir(root)
    out = load(prof(path), policy)["tls"]
    real = os.path.realpath(path)
    assert out == {
        "mode": "verify-full",
        "dir": real,
        "ca": f"{real}/ca.pem",
        "cert": f"{real}/client.pem",
        "key": f"{real}/client.key",
    }


def test_ca_only_directory_has_no_client_identity(root, policy):
    out = load(prof(make_dir(root, cert=None, key=None)), policy)["tls"]
    assert out["cert"] is None and out["key"] is None and out["ca"].endswith("/ca.pem")


def test_fallback_names_crt(root, policy):
    out = load(prof(make_dir(root, ca="ca.crt", cert="client.crt")), policy)["tls"]
    assert out["ca"].endswith("/ca.crt") and out["cert"].endswith("/client.crt")


def test_ambiguous_ca_names_are_refused(root, policy):
    path = make_dir(root)
    (path / "ca.crt").write_text("x")
    text = fails("E_PROFILE_INVALID", prof(path), policy)
    assert "ca.pem" in text and "ca.crt" in text and "ssl_files" in text


def test_ambiguous_client_cert_names_are_refused(root, policy):
    path = make_dir(root)
    (path / "client.crt").write_text("x")
    text = fails("E_PROFILE_INVALID", prof(path), policy)
    assert "client.pem" in text and "client.crt" in text


def test_ssl_files_override_default_names(root, policy):
    path = make_dir(root, ca="root.pem", cert="me.pem", key="me.key")
    raw = prof(path, ssl_files={"ca": "root.pem", "cert": "me.pem", "key": "me.key"})
    out = load(raw, policy)["tls"]
    assert out["ca"].endswith("/root.pem") and out["key"].endswith("/me.key")


def test_ssl_files_explicit_name_disables_fallback(root, policy):
    path = make_dir(root, ca="ca.crt", cert=None, key=None)
    text = fails("E_PROFILE_INVALID", prof(path, ssl_files={"ca": "ca.pem"}), policy)
    assert "'ca.pem'" in text


@pytest.mark.parametrize("mode", ["verify-full", "verify-ca"])
def test_modes(root, policy, mode):
    assert load(prof(make_dir(root), ssl_mode=mode), policy)["tls"]["mode"] == mode


@pytest.mark.parametrize("mode", ["disable", "prefer", "allow"])
def test_weak_modes_refused_with_directory(root, policy, mode):
    text = fails("E_PROFILE_INVALID", prof(make_dir(root), ssl_mode=mode), policy)
    assert "not allowed" in text


def test_require_refused_and_explained(root, policy):
    text = fails("E_PROFILE_INVALID", prof(make_dir(root), ssl_mode="require"), policy)
    assert "verify-ca" in text


@pytest.mark.parametrize("mode", ["bogus", 1, None, ["verify-full"]])
def test_unknown_mode(root, policy, mode):
    fails("E_PROFILE_INVALID", prof(make_dir(root), ssl_mode=mode), policy)


# -- the path policy ------------------------------------------------------------------------------------------------
def test_no_root_configured_is_policy_error(root):
    path = make_dir(root)
    for pol in (None, {}, {"ssl_certs_dir_root": None}, {"other": 1}):
        text = fails("E_POLICY", prof(path), pol, absent=(str(root),))
        assert "ssl_certs_dir_root" in text


@pytest.mark.parametrize("pol", ["text", 5, ["x"]])
def test_policy_must_be_a_mapping(root, pol):
    fails("E_PROFILE_INVALID", prof(make_dir(root)), pol)


@pytest.mark.parametrize("bad", ["", "relative/dir", "certs", 5, None, ["/x"], "/a\x00b"])
def test_directory_must_be_a_nonempty_absolute_string(policy, bad):
    fails("E_PROFILE_INVALID", dict(BASE, ssl_certs_dir=bad), policy)


def test_root_must_be_absolute_and_exist(tmp_path):
    path = tmp_path / "d"
    path.mkdir()
    for bad in ("relative", str(tmp_path / "missing"), 5, ""):
        fails("E_POLICY", prof(path), {"ssl_certs_dir_root": bad})
    (tmp_path / "file").write_text("x")
    fails("E_POLICY", prof(path), {"ssl_certs_dir_root": str(tmp_path / "file")})


def test_dotdot_in_the_given_path_is_refused(root, policy):
    make_dir(root, "a")
    make_dir(root, "b")
    fails("E_PROFILE_INVALID", prof(os.path.join(str(root), "a", "..", "b")), policy)


def test_dotdot_behind_the_alternative_separator_is_refused(root, policy, monkeypatch):
    # Windows takes "/" as well as os.sep (os.altsep), so C:\certs\a/../b must not slip past the ".." rule. Here
    # the other separator is an ordinary character, so the directory exists and only the ".." rule can refuse it.
    other = "\\" if os.sep == "/" else "/"
    monkeypatch.setattr(os, "altsep", other)
    make_dir(root, f"a{other}..{other}b", cert=None, key=None)
    text = fails("E_PROFILE_INVALID", prof(f"{root}{os.sep}a{other}..{other}b"), policy)
    assert "'..'" in text


def test_directory_outside_root_is_policy_error(policy, tmp_path):
    outside = make_dir(tmp_path, "elsewhere")
    text = fails("E_POLICY", prof(outside), policy, absent=(str(tmp_path),))
    assert "ssl_certs_dir_root" in text


def test_sibling_with_common_prefix_is_outside(policy, tmp_path):
    sibling = tmp_path / "certs-evil"
    sibling.mkdir()
    make_dir(sibling, "d")
    fails("E_POLICY", prof(sibling / "d"), policy)


def test_directory_symlink_escaping_root(root, policy, tmp_path):
    outside = make_dir(tmp_path, "elsewhere")
    link = root / "link"
    link.symlink_to(outside)
    fails("E_POLICY", prof(link), policy)


def test_directory_symlink_inside_root_is_followed(root, policy):
    path = make_dir(root, "real")
    (root / "alias").symlink_to(path)
    out = load(prof(root / "alias"), policy)["tls"]
    assert out["dir"] == os.path.realpath(path)


def test_root_itself_may_hold_the_files(root, policy):
    (root / "ca.pem").write_text("c")
    assert load(prof(root), policy)["tls"]["dir"] == os.path.realpath(root)


def test_root_given_through_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    make_dir(real, "d")
    (tmp_path / "rootlink").symlink_to(real)
    out = load(prof(real / "d"), {"ssl_certs_dir_root": str(tmp_path / "rootlink")})
    assert out["tls"]["ca"]


def test_missing_directory_and_not_a_directory(root, policy):
    fails("E_PROFILE_INVALID", prof(root / "nope"), policy)
    (root / "afile").write_text("x")
    text = fails("E_PROFILE_INVALID", prof(root / "afile"), policy)
    assert "not a directory" in text


def test_file_symlink_escaping_root(root, policy, tmp_path):
    path = make_dir(root, cert=None, key=None)
    secret = tmp_path / "outside.pem"
    secret.write_text(MARKER)
    (path / "ca.pem").unlink()
    (path / "ca.pem").symlink_to(secret)
    text = fails("E_POLICY", prof(path), policy)
    assert "ca.pem" in text and "outside.pem" not in text and str(tmp_path) not in text


def test_file_symlink_inside_root_is_allowed(root, policy):
    shared = make_dir(root, "shared", cert=None, key=None)
    path = make_dir(root, "db1", ca=None, cert=None, key=None)
    (path / "ca.pem").symlink_to(shared / "ca.pem")
    assert load(prof(path), policy)["tls"]["ca"] == os.path.realpath(shared / "ca.pem")


def test_dangling_symlink_counts_as_missing(root, policy):
    path = make_dir(root, ca=None, cert=None, key=None)
    (path / "ca.pem").symlink_to(root / "gone.pem")
    fails("E_PROFILE_INVALID", prof(path), policy)


def test_a_directory_named_like_a_file_is_refused(root, policy):
    path = make_dir(root, ca=None, cert=None, key=None)
    (path / "ca.pem").mkdir()
    text = fails("E_PROFILE_INVALID", prof(path), policy)
    assert "regular file" in text


# -- ssl_files --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "sub/ca.pem",
        "/etc/passwd",
        "../ca.pem",
        "..",
        ".",
        "",
        "a\\b.pem",
        "a\x00.pem",
        5,
        None,
        ["x"],
    ],
)
def test_ssl_files_values_are_plain_names(root, policy, name):
    path = make_dir(root)
    fails("E_PROFILE_INVALID", prof(path, ssl_files={"ca": name}), policy)


def test_ssl_files_unknown_key_and_shape(root, policy):
    path = make_dir(root)
    text = fails("E_PROFILE_INVALID", prof(path, ssl_files={"crl": "x.pem"}), policy)
    assert "crl" in text
    fails("E_PROFILE_INVALID", prof(path, ssl_files=["ca.pem"]), policy)


def test_ssl_files_null_means_defaults(root, policy):
    assert load(prof(make_dir(root), ssl_files=None), policy)["tls"]["ca"].endswith("ca.pem")


# -- required and paired files ----------------------------------------------------------------------------------------
def test_missing_ca_names_the_file_only(root, policy):
    path = make_dir(root, ca=None)
    text = fails("E_PROFILE_INVALID", prof(path), policy, absent=(str(root), "SECRET"))
    assert "ca.pem" in text


def test_cert_without_key(root, policy):
    path = make_dir(root, key=None)
    text = fails("E_PROFILE_INVALID", prof(path), policy)
    assert "client.pem" in text and "client.key" in text


def test_key_without_cert(root, policy):
    path = make_dir(root, cert=None)
    text = fails("E_PROFILE_INVALID", prof(path), policy)
    assert "client.key" in text and "client.pem" in text


def test_explicit_cert_without_key_name(root, policy):
    path = make_dir(root)
    fails("E_PROFILE_INVALID", prof(path, ssl_files={"cert": "client.pem"}), policy)
    fails("E_PROFILE_INVALID", prof(path, ssl_files={"key": "client.key"}), policy)


def test_explicit_names_that_do_not_exist(root, policy):
    path = make_dir(root)
    text = fails(
        "E_PROFILE_INVALID", prof(path, ssl_files={"cert": "a.pem", "key": "a.key"}), policy
    )
    assert "a.pem" in text


# -- key permissions ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [0o644, 0o664, 0o604, 0o666, 0o640 | 0o010, 0o620])
def test_key_with_group_or_world_access_is_policy_error(root, policy, mode):
    path = make_dir(root, key_mode=mode)
    text = fails("E_POLICY", prof(path), policy)
    assert "client.key" in text and "chmod" in text and "600" in text


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_owner_only_key_is_accepted(root, policy, mode):
    assert load(prof(make_dir(root, key_mode=mode)), policy)["tls"]["key"]


def stat_of(mode, uid):
    return types.SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid)


@pytest.mark.parametrize(
    "mode,uid,ok",
    [
        (0o600, 1000, True),
        (0o400, 0, True),
        (0o640, 0, True),  # libpq rule: u=rw,g=r allowed when owned by root
        (0o640, 1000, False),
        (0o660, 0, False),
        (0o604, 0, False),
        (0o644, 0, False),
        (0o700, 1000, True),
        (0o610, 0, False),
    ],
)
def test_key_mode_rule(mode, uid, ok):
    assert tls.key_mode_ok(stat_of(mode, uid)) is ok


def test_unreadable_files_are_refused(root, policy, monkeypatch):
    path = make_dir(root)
    real_open = os.open

    def deny(file, flags, *args, **kwargs):
        if str(file).endswith("client.key"):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(tls.os, "open", deny)
    text = fails("E_PROFILE_INVALID", prof(path), policy, absent=(str(root),))
    assert "client.key" in text and "not readable" in text


def test_file_contents_are_never_read(root, policy, monkeypatch):
    path = make_dir(root)

    def no_read(*args, **kwargs):  # pragma: no cover - fails the test if reached
        raise AssertionError("tls must not read file contents")

    monkeypatch.setattr(tls.os, "read", no_read, raising=False)
    monkeypatch.setattr("builtins.open", no_read)
    assert load(prof(path), policy)["tls"]["key"]


# -- conflicts with query / connect_args (the allowlist stays default-deny) ---------------------------------------------
@pytest.mark.parametrize(
    "dialect,where,key",
    [
        ("postgresql", "query", "sslmode"),
        ("postgresql", "connect_args", "sslmode"),
        ("mysql", "query", "ssl_verify_cert"),
        ("mysql", "connect_args", "ssl_verify_identity"),
        ("mysql", "connect_args", "ssl_disabled"),
        ("oracle", "query", "ssl_server_dn_match"),
        ("oracle", "connect_args", "ssl_server_dn_match"),
    ],
)
def test_tls_keys_in_query_or_connect_args_conflict(root, policy, dialect, where, key):
    path = make_dir(root, **({"cert": None, "key": None} if dialect == "oracle" else {}))
    if dialect == "oracle":
        (path / "ewallet.pem").write_text("w")
    text = fails("E_PROFILE_INVALID", prof(path, dialect, **{where: {key: "x"}}), policy)
    assert key in text and "ssl_mode" in text


def test_non_tls_query_keys_are_left_alone(root, policy):
    out = load(prof(make_dir(root), query={"application_name": "x"}), policy)
    assert out["query"] == {"application_name": "x"}


def test_path_options_stay_forbidden_in_profile_query(root, policy):
    """The module produces the path options; a profile may never carry them (allowlist is default-deny)."""
    fails(
        "E_PROFILE_INVALID", prof(make_dir(root), query={"sslrootcert": "/etc/ssl/x.pem"}), policy
    )
    without_dir = profiles.validate_profile(
        "p", dict(BASE, query={"sslrootcert": "/etc/ssl/x.pem"})
    )
    adapter = dialects.adapter("postgresql")
    with pytest.raises(SaltInvocationError) as exc:
        adapter.url(without_dir, "pw")
    assert str(exc.value).startswith("[E_POLICY]")
    for key in ("sslrootcert", "sslcert", "sslkey", "sslcertmode"):
        assert key not in adapter.allowed_args
    for key in ("ssl_ca", "ssl_cert", "ssl_key"):
        assert key not in dialects.adapter("mysql").allowed_args
    for key in ("wallet_location", "protocol", "ssl_context"):
        assert key not in dialects.adapter("oracle").allowed_args


# -- per dialect ------------------------------------------------------------------------------------------------------------
def test_mssql_is_not_supported(root, policy):
    text = fails("E_PROFILE_INVALID", prof(make_dir(root), "mssql"), policy)
    assert "not supported for mssql yet" in text


def test_oracle_needs_the_wallet_and_takes_no_ssl_files(root, policy):
    path = make_dir(root, ca=None, cert=None, key=None)
    text = fails("E_PROFILE_INVALID", prof(path, "oracle"), policy)
    assert "ewallet.pem" in text
    (path / "ewallet.pem").write_text("w")
    out = load(prof(path, "oracle"), policy)["tls"]
    assert out["ca"].endswith("/ewallet.pem") and out["cert"] is None and out["key"] is None
    text = fails("E_PROFILE_INVALID", prof(path, "oracle", ssl_files={"ca": "ewallet.pem"}), policy)
    assert "oracle" in text


# -- argument mapping (what reaches the driver) ---------------------------------------------------------------------------
def tls_profile(root, policy, dialect, mode="verify-full", **dirkw):
    path = make_dir(root, **dirkw)
    if dialect == "oracle":
        path = make_dir(root, "w", ca=None, cert=None, key=None)
        (path / "ewallet.pem").write_text("w")
    return load(prof(path, dialect, ssl_mode=mode), policy)


def test_postgresql_mapping_mtls(root, policy):
    p = tls_profile(root, policy, "postgresql")
    real = p["tls"]["dir"]
    assert tls.query_args("postgresql", p) == {
        "sslmode": "verify-full",
        "sslrootcert": f"{real}/ca.pem",
        "sslcert": f"{real}/client.pem",
        "sslkey": f"{real}/client.key",
    }
    assert not tls.connect_args("postgresql", p)


def test_postgresql_mapping_server_auth_only_disables_default_client_cert(root, policy):
    p = tls_profile(root, policy, "postgresql", mode="verify-ca", cert=None, key=None)
    args = tls.query_args("postgresql", p)
    assert args["sslmode"] == "verify-ca" and args["sslcertmode"] == "disable"
    assert "sslcert" not in args and "sslkey" not in args


@pytest.mark.parametrize("mode,identity", [("verify-full", True), ("verify-ca", False)])
def test_mysql_mapping(root, policy, mode, identity):
    p = tls_profile(root, policy, "mysql", mode=mode)
    real = p["tls"]["dir"]
    assert tls.connect_args("mysql", p) == {
        "ssl_ca": f"{real}/ca.pem",
        "ssl_cert": f"{real}/client.pem",
        "ssl_key": f"{real}/client.key",
        "ssl_verify_cert": True,
        "ssl_verify_identity": identity,
    }
    assert not tls.query_args("mysql", p)


def test_mysql_mapping_without_client_cert(root, policy):
    p = tls_profile(root, policy, "mysql", cert=None, key=None)
    args = tls.connect_args("mysql", p)
    assert set(args) == {"ssl_ca", "ssl_verify_cert", "ssl_verify_identity"}


@pytest.mark.parametrize("mode,match", [("verify-full", True), ("verify-ca", False)])
def test_oracle_mapping_is_unverified_but_exact(root, policy, mode, match):
    p = tls_profile(root, policy, "oracle", mode=mode)
    assert tls.connect_args("oracle", p) == {
        "protocol": "tcps",
        "wallet_location": p["tls"]["dir"],
        "ssl_server_dn_match": match,
    }
    assert not tls.query_args("oracle", p)


def test_mapping_is_empty_without_tls_and_mssql_never_maps():
    plain = profiles.validate_profile("p", dict(BASE))
    for dialect in ("postgresql", "mysql", "oracle", "mssql"):
        assert not tls.query_args(dialect, plain) and not tls.connect_args(dialect, plain)


def test_adapters_apply_the_mapping(root, policy):
    pg = tls_profile(root, policy, "postgresql")
    url = dialects.adapter("postgresql").url(pg, "pw")
    assert url.query["sslmode"] == "verify-full" and url.query["sslrootcert"].endswith("/ca.pem")
    my = tls_profile(root, policy, "mysql")
    args = dialects.adapter("mysql").connect_args(my)
    assert args["ssl_ca"].endswith("/ca.pem") and args["ssl_verify_cert"] is True
    assert args["connect_timeout"] == my["connect_timeout_s"]
    assert "ssl_ca" not in dialects.adapter("mysql").url(my, "pw").query
    ora = tls_profile(root, policy, "oracle")
    assert dialects.adapter("oracle").connect_args(ora)["protocol"] == "tcps"


def test_adapter_fails_closed_for_unresolved_tls_config():
    raw = profiles.validate_profile("p", dict(BASE))
    raw["ssl_certs_dir"] = "/somewhere"
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter("postgresql").url(raw, "pw")
    assert str(exc.value).startswith("[E_PROFILE_INVALID]")


# -- load_profile reads sqlx:_policy ----------------------------------------------------------------------------------------
def test_load_profile_uses_policy_from_the_sqlx_mapping(root, policy):
    path = make_dir(root)
    config = {"_policy": policy, "profiles": {"p": prof(path)}}
    assert profiles.load_profile("p", config)["tls"]["ca"].endswith("/ca.pem")
    config.pop("_policy")
    with pytest.raises(SaltInvocationError) as exc:
        profiles.load_profile("p", config)
    assert str(exc.value).startswith("[E_POLICY]")


def test_policy_section_is_not_a_profile(policy):
    config = {"_policy": policy, "profiles": {}}
    with pytest.raises(SaltInvocationError) as exc:
        profiles.load_profile("_policy", config)
    assert str(exc.value).startswith("[E_PROFILE_UNKNOWN]")


# -- no path or key material in anything we say ----------------------------------------------------------------------------------
def test_scrub_replaces_paths_with_file_names(root, policy):
    p = tls_profile(root, policy, "postgresql")
    real = p["tls"]["dir"]
    text = (
        f'certificate present, but not private key file "{real}/client.key": x; '
        f'root certificate file "{real}/ca.pem" does not exist; dir {real} and /etc/ssl/other/ca.crt'
    )
    out = tls.scrub(text, p)
    assert real not in out and "/etc/ssl" not in out
    assert '"client.key"' in out and '"ca.pem"' in out and "ca.crt" in out


def test_scrub_leaves_text_alone_without_tls():
    plain = profiles.validate_profile("p", dict(BASE))
    assert tls.scrub("see /var/log/x.log", plain) == "see /var/log/x.log"


def test_scrub_handles_each_path_separator_context(root, policy):
    p = tls_profile(root, policy, "mysql")
    real = p["tls"]["dir"]
    out = tls.scrub(f"('{real}/client.pem') <{real}/client.key>: {real}/", p)
    assert real not in out and "client.pem" in out and "client.key" in out
