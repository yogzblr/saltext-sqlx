"""Unit tests of saltexts_apply.py with REAL virtualenvs and real pip (no mocks)."""
import json
import os
import platform
import shutil
import sys

import pytest

from conftest import APPLY, make_wheel, result_line, write_sums


def test_first_install_then_up_to_date_is_changed_no(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "state" / "applied.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert r.returncode == 0, r.stderr
    assert result_line(r)["changed"] == "yes"
    assert result_line(r)["installed"] == {"saltext-demo": "1.0.0"}
    assert env.version("saltext-demo") == "1.0.0"
    before = marker.stat().st_mtime_ns
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert r.returncode == 0 and result_line(r)["changed"] == "no"
    assert r.stderr == ""  # nothing on stderr on success (PowerShell 5.1 treats native stderr as an error record)
    assert marker.stat().st_mtime_ns == before  # a no-op does not even touch the marker


def test_version_differs_installs_from_temp_wheelhouse(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0", "2.0.0"]})
    marker = tmp_path / "applied.json"
    assert env.apply({"saltext-demo": "1.0.0"}, wh, marker).returncode == 0
    assert env.version("saltext-demo") == "1.0.0"
    r = env.apply({"saltext-demo": "2.0.0"}, wh, marker)
    assert result_line(r) == {"changed": "yes", "comment": "installed 1 package(s)", "installed": {"saltext-demo": "2.0.0"}}
    assert env.version("saltext-demo") == "2.0.0"
    assert json.loads(marker.read_text()) == {"saltext-demo": "2.0.0"}
    # a downgrade is just another pin change (rollback = ordinary rollout)
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert result_line(r)["changed"] == "yes" and env.version("saltext-demo") == "1.0.0"


def test_only_the_changed_package_is_touched(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"pkg-a": ["1.0.0", "1.1.0"], "pkg-b": ["5.0.0"]})
    marker = tmp_path / "applied.json"
    env.apply({"pkg-a": "1.0.0", "pkg-b": "5.0.0"}, wh, marker)
    r = env.apply({"pkg-a": "1.1.0", "pkg-b": "5.0.0"}, wh, marker)
    assert result_line(r)["installed"] == {"pkg-a": "1.1.0"}


def test_extras_only_change(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse(
        {"saltext-demo": ["1.0.0"], "driver-x": ["3.0.0"]},
        **{"saltext-demo": {"extras": {"x": ["driver-x"]}}},
    )
    marker = tmp_path / "applied.json"
    assert env.apply({"saltext-demo": "1.0.0"}, wh, marker).returncode == 0
    assert env.version("driver-x") is None
    # same version, different key (extras are part of the key) -> must install the extra's dependency
    r = env.apply({"saltext-demo[x]": "1.0.0"}, wh, marker)
    assert r.returncode == 0, r.stderr
    assert result_line(r)["changed"] == "yes"
    assert env.version("driver-x") == "3.0.0"
    assert result_line(env.apply({"saltext-demo[x]": "1.0.0"}, wh, marker))["changed"] == "no"


def test_failed_install_exits_nonzero_and_writes_no_marker(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "applied.json"
    env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    good = marker.read_text()
    r = env.apply({"saltext-demo": "1.0.0", "saltext-missing": "9.9.9"}, wh, marker)
    assert r.returncode == 1
    assert "pip install failed" in r.stderr and "saltext-missing" in r.stderr
    assert marker.read_text() == good  # untouched
    assert env.version("saltext-missing") is None
    # and the run is NOT silently "ok" the next time
    assert env.apply({"saltext-demo": "1.0.0", "saltext-missing": "9.9.9"}, wh, marker).returncode == 1


def test_failed_first_install_leaves_no_marker_at_all(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "applied.json"
    r = env.apply({"saltext-demo": "2.0.0"}, wh, marker)
    assert r.returncode == 1 and not marker.exists()


def test_pip_check_failure_is_fatal_and_marker_not_written(env, make_wheelhouse, tmp_path):
    # `holder` (already installed) needs dep<2; our package needs dep>=2: pip installs it with only a warning, `pip check` fails.
    pre = make_wheelhouse({"holder": ["1.0.0"], "dep": ["1.0.0"]}, sub="pre", **{"holder": {"requires": ["dep<2"]}})
    assert env.pip("install", "--no-index", "--find-links", str(pre), "holder").returncode == 0
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"], "dep": ["2.0.0"]}, **{"saltext-demo": {"requires": ["dep>=2"]}})
    marker = tmp_path / "applied.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert r.returncode == 1
    assert "pip check failed" in r.stderr and "holder" in r.stderr
    assert not marker.exists()
    # it keeps failing loudly on every run (the packages are installed, the marker is not)
    assert env.apply({"saltext-demo": "1.0.0"}, wh, marker).returncode == 1


def test_check_mode_changes_nothing(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "applied.json"
    assert env.apply({"saltext-demo": "1.0.0"}, wh, marker, "--check").returncode == 1  # work needed
    assert env.version("saltext-demo") is None and not marker.exists()
    env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert env.apply({"saltext-demo": "1.0.0"}, wh, marker, "--check").returncode == 0
    assert env.apply({"saltext-demo": "2.0.0"}, wh, marker, "--check").returncode == 1


def test_equivalent_version_spelling_is_not_a_change(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "applied.json"
    env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    r = env.apply({"saltext-demo": "1.0"}, wh, marker)  # PEP 440: 1.0 == 1.0.0
    assert result_line(r)["changed"] == "no"


def test_uninstalled_package_is_reinstalled_even_if_marker_says_applied(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    marker = tmp_path / "applied.json"
    env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    env.pip("uninstall", "-y", "saltext-demo")
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert result_line(r)["changed"] == "yes" and env.version("saltext-demo") == "1.0.0"


def test_empty_pins_is_an_error(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    r = env.apply({}, wh, tmp_path / "applied.json")
    assert r.returncode == 1 and "no pins" in r.stderr


# ---- checksums -------------------------------------------------------------------------------------------------------------

def test_tampered_wheel_is_rejected(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    w = next(wh.glob("*.whl"))
    w.write_bytes(w.read_bytes() + b"x")
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "applied.json")
    assert r.returncode == 1 and "SHA-256 mismatch" in r.stderr and env.version("saltext-demo") is None


def test_unlisted_wheel_missing_listed_wheel_and_missing_sums_are_rejected(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    make_wheel(wh, "extra-wheel", "1.0.0")
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "m.json")
    assert r.returncode == 1 and "not listed in SHA256SUMS" in r.stderr
    write_sums(wh)
    next(wh.glob("extra_wheel*")).unlink()
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "m.json")
    assert r.returncode == 1 and "missing from" in r.stderr
    (wh / "SHA256SUMS").unlink()
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "m.json")
    assert r.returncode == 1 and "SHA256SUMS" in r.stderr


@pytest.mark.parametrize("crlf,star", [(True, False), (False, True), (True, True)])
def test_sha256sums_written_on_windows_is_accepted(env, make_wheelhouse, tmp_path, crlf, star):
    """CRLF line endings and the `*name` binary marker (certutil / sha256sum -b / PowerShell-made files)."""
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    write_sums(wh, crlf=crlf, star=star)
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "m.json")
    assert r.returncode == 0, r.stderr


# ---- paths -----------------------------------------------------------------------------------------------------------------

def test_paths_with_spaces_and_non_ascii(env, make_wheelhouse, tmp_path):
    """Windows installs live in 'C:\\Program Files\\Salt Project' and 'C:\\ProgramData\\Salt Project': spaces everywhere."""
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]}, sub="Program Files x/Salt Project/saltext wheels é")
    state = tmp_path / "ProgramData" / "Salt Project" / "state dir"
    state.mkdir(parents=True)
    pins = state / "pins.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, state / "applied.json", pins_file=pins)
    assert r.returncode == 0, r.stderr
    assert (state / "applied.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="needs a Windows host (see deploy/README.md: not verified here)")
def test_native_windows_paths():  # pragma: no cover
    pytest.fail("run the whole suite on a Windows runner; this placeholder documents that nothing here executed on Windows")


def test_windows_path_strings_are_handled_without_touching_the_filesystem():
    """The helper only builds paths with os.path from what it is given; no hard-coded '/'. Backslash separators survive."""
    sys.path.insert(0, str(APPLY.parent))
    try:
        import importlib

        mod = importlib.import_module("saltexts_apply")
        assert mod.split_spec("saltext-sqlx[postgres,mysql]") == ("saltext-sqlx", "[postgres,mysql]")
        assert mod.split_spec("saltext.sqlx") == ("saltext.sqlx", "")
        r = mod.resolve_target(r"C:\Program Files\Salt Project\Salt\extras-3.14")
        assert r == r"C:\Program Files\Salt Project\Salt\extras-3.14"
        assert mod.resolve_target("") is None
    finally:
        sys.path.remove(str(APPLY.parent))


# ---- salt-pip style target directory -----------------------------------------------------------------------------------------

def test_target_directory_install_like_salt_pip(env, make_wheelhouse, tmp_path):
    """--target DIR (the onedir 'extras-X.Y' directory): installs with --upgrade, sees what is already there, upgrades in place."""
    wh = make_wheelhouse({"saltext-demo": ["1.0.0", "2.0.0"]})
    extras = tmp_path / "extras-3.14"
    marker = tmp_path / "applied.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker, "--target", str(extras))
    assert r.returncode == 0, r.stderr
    assert (extras / "saltext_demo" / "__init__.py").exists() and env.version("saltext-demo") is None  # NOT in site-packages
    assert result_line(env.apply({"saltext-demo": "1.0.0"}, wh, marker, "--target", str(extras)))["changed"] == "no"
    r = env.apply({"saltext-demo": "2.0.0"}, wh, marker, "--target", str(extras))
    assert r.returncode == 0, r.stderr
    assert "2.0.0" in (extras / "saltext_demo" / "__init__.py").read_text()
    assert not list(extras.glob("saltext_demo-1.0.0*"))


# ---- the real wheelhouse -----------------------------------------------------------------------------------------------------

def _host_target():
    m = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(m)
    return f"{platform.system().lower()}-{arch}" if arch else None


def test_real_wheelhouse_installs_offline_and_is_idempotent(env, real_wheelhouse, tmp_path):
    versions = json.loads((real_wheelhouse / "VERSIONS.json").read_text())
    want_py = versions["python"]
    have_py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    target = _host_target()
    if want_py != have_py:
        pytest.skip(f"wheelhouse is for Python {want_py}, this interpreter is {have_py}")
    if target is None or not (real_wheelhouse / target).is_dir():
        pytest.skip(f"no wheelhouse folder for host target {target}")
    pkgs = versions["targets"][target]
    pins = {"saltext-sqlx[postgres,mysql,oracle,mssql]": pkgs["saltext-sqlx"]}
    for name in ("saltext-kafka", "saltext-elasticsearch", "saltext-mongodb", "saltext-redis", "elasticsearch", "pymongo",
                 "redis", "redis-py-cluster"):
        pins[name] = pkgs[name]
    wh = real_wheelhouse / target
    r = env.apply(pins, wh, tmp_path / "applied.json")
    assert r.returncode == 0, r.stderr
    assert result_line(r)["changed"] == "yes"
    for name in ("psycopg", "PyMySQL", "oracledb", "pymssql", "SQLAlchemy"):
        assert env.version(name) is not None, name
    assert env.pip("check").returncode == 0
    assert result_line(env.apply(pins, wh, tmp_path / "applied.json"))["changed"] == "no"
