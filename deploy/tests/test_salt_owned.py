"""Respect what Salt ships: vendored Salt locks, pin check, stubs, stripped wheelhouse, and the minion helper.

Nothing here needs the network except the tests marked ``needs_wheelhouse`` (they read a wheelhouse built by build_wheelhouse.sh).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import DEPLOY
from conftest import TOOL
from conftest import install_salt_stubs
from conftest import make_wheel
from conftest import result_line
from conftest import write_sums

REQ = DEPLOY / "wheelhouse" / "requirements"
LOCKS = sorted((REQ / "salt-locks").glob("*/py*.lock"))
TARGETS = ("linux-x86_64", "linux-arm64", "windows-x86_64", "windows-arm64")
BUILD = DEPLOY / "wheelhouse" / "build_wheelhouse.sh"


def tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def lock_for(tag, pyver, osname):
    return REQ / "salt-locks" / tag / f"py{pyver}.{osname}.lock"


def lock_names(path):
    return {
        re.sub(r"[-_.]+", "-", m.group(1)).lower(): m.group(2)
        for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", Path(path).read_text(), re.M)
    }


# ---- the vendored locks ----------------------------------------------------------------------------------------------------


def test_vendored_locks_have_a_header_with_tag_and_date():
    assert len(LOCKS) == 6  # v3008.5 py3.14, v3007.15 py3.11, v3006.27 py3.11 x linux/windows
    for path in LOCKS:
        head = path.read_text().splitlines()[:6]
        assert head[0].startswith("# VENDORED COPY")
        assert head[1] == f"# salt-tag: {path.parent.name}"
        assert re.fullmatch(r"# fetched: \d{4}-\d\d-\d\d", head[4]), head
        assert head[5].startswith("# source: https://raw.githubusercontent.com/saltstack/salt/")
        assert re.search(r"^cryptography==", path.read_text(), re.M)


def test_the_facts_this_work_is_based_on():
    """Salt 3008.5 / Python 3.14: cryptography and pyopenssl everywhere, pymssql on Windows only, no database drivers."""
    linux = lock_names(lock_for("v3008.5", "3.14", "linux"))
    windows = lock_names(lock_for("v3008.5", "3.14", "windows"))
    assert linux["cryptography"] == windows["cryptography"] == "50.0.1"
    assert linux["pyopenssl"] == windows["pyopenssl"] == "26.4.0"
    assert "pymssql" not in linux and windows["pymssql"] == "2.3.11"
    for absent in (
        "psycopg",
        "psycopg-binary",
        "pymysql",
        "oracledb",
        "sqlalchemy",
        "redis",
        "pymongo",
        "pg8000",
        "scramp",
    ):
        assert absent not in linux and absent not in windows, absent


# ---- pins -----------------------------------------------------------------------------------------------------------------


def _target_requirements(target):
    return [REQ / "requirements.common.txt", REQ / f"requirements.{target}.txt"]


PAIRS = [
    (lock, t) for lock in LOCKS for t in TARGETS if lock.name.split(".")[-2] == t.split("-")[0]
]


@pytest.mark.parametrize(
    "lock,target", PAIRS, ids=lambda v: v if isinstance(v, str) else f"{v.parent.name}-{v.name}"
)
def test_the_pins_of_the_repo_agree_with_every_vendored_lock(lock, target):
    """Every requirement file of every target passes the pin check against every lock of the same OS (no build needed)."""
    r = tool("check-pins", "--lock", str(lock), *map(str, _target_requirements(target)))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "cryptography==50.0.2",
        "cryptography>=51",
        "urllib3==2.8.0",
        "PyOpenSSL==27.0.0",
        "typing_extensions==4.16.0",
    ],
)
def test_a_version_bump_of_a_salt_owned_package_is_rejected_with_a_clear_message(tmp_path, bad):
    req = tmp_path / "req.txt"
    req.write_text(f"SQLAlchemy==2.0.55\n{bad}   # somebody wanted a newer one\n")
    r = tool("check-pins", "--lock", str(lock_for("v3008.5", "3.14", "linux")), str(req))
    name = re.split(r"[=<>!~]", bad)[0].lower().replace("_", "-")
    assert r.returncode == 1
    assert f"req.txt:2: '{bad}'" in r.stderr
    assert f"already ships {name}==" in r.stderr
    assert "v3008.5" in r.stderr and "python 3.14" in r.stderr
    assert "Remove the pin" in r.stderr and "refresh_salt_locks.sh" in r.stderr


def test_repeating_salts_own_version_or_naming_the_package_without_a_version_is_harmless(tmp_path):
    req = tmp_path / "req.txt"
    req.write_text("cryptography==50.0.1\nurllib3\nrequests[socks]\n")
    assert (
        tool("check-pins", "--lock", str(lock_for("v3008.5", "3.14", "linux")), str(req)).returncode
        == 0
    )


def test_salt_wins_marker_drops_the_pin_where_salt_ships_the_package(tmp_path):
    req, out = tmp_path / "req.txt", tmp_path / "out.txt"
    req.write_text(
        "PyMySQL==1.2.3   # salt-wins: only where Salt does not ship it\nsqlalchemy==2.0.55\n"
    )
    win = tool(
        "check-pins",
        "--lock",
        str(lock_for("v3007.15", "3.11", "windows")),
        "--out",
        str(out),
        str(req),
    )
    assert win.returncode == 0 and "dropped" in win.stderr and "pymysql==1.2.0" in win.stderr
    assert "PyMySQL" not in out.read_text() and "sqlalchemy" in out.read_text()
    lin = tool(
        "check-pins",
        "--lock",
        str(lock_for("v3007.15", "3.11", "linux")),
        "--out",
        str(out),
        str(req),
    )
    assert (
        lin.returncode == 0 and "PyMySQL==1.2.3" in out.read_text()
    )  # Salt on Linux does not ship it: kept


@pytest.mark.skipif(os.name == "nt", reason="bash script")
def test_build_script_fails_on_a_bumped_pin_before_downloading_anything(tmp_path):
    reqdir = tmp_path / "req"
    shutil.copytree(REQ, reqdir)
    (reqdir / "requirements.common.txt").write_text(
        (REQ / "requirements.common.txt").read_text() + "cryptography==50.0.2\n"
    )
    own = tmp_path / "own"
    make_wheel(own, "saltext-sqlx", "0.1.0")
    out = tmp_path / "out"
    env = dict(os.environ, OWN=str(own), OUT=str(out), REQ=str(reqdir), PYVER="3.14", PIP="false")
    r = subprocess.run(["bash", str(BUILD)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "already ships cryptography==50.0.1" in r.stderr
    for target in TARGETS:
        assert f"{target}: a pin conflicts with a package Salt ships" in r.stdout
    assert not list(
        out.glob("*/*.whl")
    )  # `PIP=false` would have failed any download: nothing was attempted


# ---- stubs and stripping --------------------------------------------------------------------------------------------------


def pip_download(tmp_path, requirement, dest, index, stubs):
    """pip download against two local folders only (a stand-in "index" and the stubs) with the Salt constraints."""
    cmd = [sys.executable, "-m", "pip", "download", requirement, "-d", str(dest), "--no-index"]
    cmd += ["--find-links", str(index), "--find-links", str(stubs), "-c", str(tmp_path / "c.txt")]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_stub_wheels_win_over_the_real_wheel_of_the_same_version_and_are_stripped(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text(
        "# salt-tag: v9.9\n# python: 3.14\n# platform: linux\nownedlib==1.5.0\n    # via -r requirements/base.txt\n"
    )
    stubs, index = tmp_path / "stubs", tmp_path / "index"
    assert tool("stub", str(stubs), "--lock", str(lock)).returncode == 0
    assert (stubs / "ownedlib-1.5.0+salt9.9-py3-none-any.whl").exists()
    make_wheel(index, "ownedlib", "1.5.0")  # the "real" one a package index would offer
    make_wheel(index, "mine", "1.0.0", requires=["ownedlib>=1.2"])
    dest = tmp_path / "dest"
    (tmp_path / "c.txt").write_text(tool("constraints", str(lock)).stdout)
    p = pip_download(tmp_path, "mine", dest, index, stubs)
    assert p.returncode == 0, p.stderr
    assert sorted(x.name for x in dest.glob("*.whl")) == [
        "mine-1.0.0-py3-none-any.whl",
        "ownedlib-1.5.0+salt9.9-py3-none-any.whl",
    ]
    assert (
        tool("strip", str(dest), "--lock", str(lock), "--check").returncode == 1
    )  # the stub is there
    assert tool("strip", str(dest), "--lock", str(lock)).returncode == 0
    assert [x.name for x in dest.glob("*.whl")] == ["mine-1.0.0-py3-none-any.whl"]
    assert tool("strip", str(dest), "--lock", str(lock), "--check").returncode == 0


def test_a_package_that_needs_a_newer_salt_owned_package_cannot_be_resolved(tmp_path):
    """The resolver itself refuses (this is what pushed anyio 4.15.1 back to 4.14.2: it wants typing_extensions>=4.16, Salt has 4.15)."""
    lock = tmp_path / "x.lock"
    lock.write_text("# salt-tag: v9.9\nownedlib==1.5.0\n")
    stubs, index = tmp_path / "stubs", tmp_path / "index"
    tool("stub", str(stubs), "--lock", str(lock))
    make_wheel(index, "mine", "1.0.0", requires=["ownedlib>=2"])
    (tmp_path / "c.txt").write_text(tool("constraints", str(lock)).stdout)
    p = pip_download(tmp_path, "mine", tmp_path / "d", index, stubs)
    assert p.returncode != 0 and "ResolutionImpossible" in p.stderr


# ---- the real wheelhouse ----------------------------------------------------------------------------------------------------


def test_real_wheelhouse_contains_nothing_that_salt_ships(real_wheelhouse):
    versions = json.loads((real_wheelhouse / "VERSIONS.json").read_text())
    tag, pyver = versions["salt_tag"], versions["python"]
    assert tag
    for target, packages in versions["targets"].items():
        owned = lock_names(lock_for(tag, pyver, target.split("-")[0]))
        if target == "windows-arm64":
            owned.pop("pymssql")  # 2.3.11 has no win_arm64 wheel; documented in deploy/README.md
        clash = sorted(set(packages) & set(owned))
        assert clash == [], f"{target}: wheels that Salt already ships: {clash}"
        assert "cryptography" not in packages and "pyopenssl" not in packages, target
        sums = (real_wheelhouse / target / "SHA256SUMS").read_text()
        assert "SALT_OWNED.txt" in sums
        listed = lock_names(real_wheelhouse / target / "SALT_OWNED.txt")
        assert listed == owned or "pymssql" not in listed
    assert (
        "pymssql" not in versions["targets"]["windows-x86_64"]
    )  # Salt's 2.3.11 is used, the 2.4.6 wheel is not shipped
    assert versions["targets"]["linux-x86_64"]["pymssql"] == "2.4.6"  # Linux Salt has none


def _host_target():
    import platform

    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    return f"{platform.system().lower()}-{arch}" if arch else None


def test_real_wheelhouse_installs_without_touching_what_salt_ships(env, real_wheelhouse, tmp_path):
    """The Salt-owned set is installed first (metadata stubs at Salt's versions); the helper must leave every one of them alone."""
    versions = json.loads((real_wheelhouse / "VERSIONS.json").read_text())
    target = _host_target()
    if versions["python"] != f"{sys.version_info[0]}.{sys.version_info[1]}" or target is None:
        pytest.skip("wheelhouse is for another Python or host")
    wh = real_wheelhouse / target
    names = install_salt_stubs(env, wh, versions, tmp_path)
    before = {n: env.version(n) for n in names}
    pkgs = versions["targets"][target]
    pins = {"saltext-sqlx[postgres,mysql,oracle,mssql]": pkgs["saltext-sqlx"]}
    for dep in (
        "saltext-kafka",
        "saltext-elasticsearch",
        "saltext-mongodb",
        "saltext-redis",
        "elasticsearch",
        "pymongo",
        "redis",
        "redis-py-cluster",
    ):
        pins[dep] = pkgs[dep]
    for extra in (("--target", str(tmp_path / "extras-3.14")), ()):
        r = env.apply(pins, wh, tmp_path / f"applied{len(extra)}.json", *extra)
        assert r.returncode == 0, r.stderr
        assert result_line(r)["changed"] == "yes"
        assert env.pip("check").returncode == 0, env.pip("check").stdout
    assert {n: env.version(n) for n in names} == before  # not one Salt-owned package changed
    extras = tmp_path / "extras-3.14"
    for n in names:
        assert not list(
            extras.glob(f"{n.replace('-', '_')}-*")
        ), n  # and none was copied into the extras directory


# ---- the minion helper ------------------------------------------------------------------------------------------------------


def _owned(wheels, *lines):
    (wheels / "SALT_OWNED.txt").write_text("# test\n" + "\n".join(lines) + "\n")
    write_sums(wheels)


def test_helper_never_installs_or_upgrades_a_package_salt_ships(env, make_wheelhouse, tmp_path):
    base = make_wheelhouse({"ownedlib": ["1.5.0"]}, sub="salt-has")
    assert env.pip("install", "--no-index", "--find-links", str(base), "ownedlib").returncode == 0
    # a decoy: even if somebody drops a newer ownedlib into the wheelhouse, the helper must not use it
    wh = make_wheelhouse(
        {"saltext-demo": ["1.0.0"], "ownedlib": ["2.0.0"], "plain-dep": ["3.0.0"]},
        **{"saltext-demo": {"requires": ["ownedlib>=1.2", "plain-dep"]}},
    )
    _owned(wh, "ownedlib==1.5.0")
    marker = tmp_path / "applied.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert r.returncode == 0, r.stderr
    assert env.version("ownedlib") == "1.5.0"
    assert env.version("plain-dep") == "3.0.0" and env.version("saltext-demo") == "1.0.0"
    assert env.pip("check").returncode == 0


def test_helper_in_target_mode_does_not_copy_a_salt_package_into_the_extras_dir(
    env, make_wheelhouse, tmp_path
):
    base = make_wheelhouse({"ownedlib": ["1.5.0"]}, sub="salt-has")
    env.pip("install", "--no-index", "--find-links", str(base), "ownedlib")
    wh = make_wheelhouse(
        {"saltext-demo": ["1.0.0"], "ownedlib": ["1.5.0"]},
        **{"saltext-demo": {"requires": ["ownedlib>=1.2"]}},
    )
    _owned(wh, "ownedlib==1.5.0")
    extras = tmp_path / "extras-3.14"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "applied.json", "--target", str(extras))
    assert r.returncode == 0, r.stderr
    assert (extras / "saltext_demo").is_dir() and not list(extras.glob("ownedlib*"))
    assert env.pip("check").returncode == 0


def test_helper_without_the_owned_list_in_target_mode_would_have_copied_it(
    env, make_wheelhouse, tmp_path
):
    """The reason for the design: `pip install --target` ignores what is installed and reinstalls every dependency into the extras dir."""
    base = make_wheelhouse({"ownedlib": ["1.5.0"]}, sub="salt-has")
    env.pip("install", "--no-index", "--find-links", str(base), "ownedlib")
    wh = make_wheelhouse(
        {"saltext-demo": ["1.0.0"], "ownedlib": ["1.5.0"]},
        **{"saltext-demo": {"requires": ["ownedlib>=1.2"]}},
    )
    extras = tmp_path / "extras-plain-pip"
    p = env.pip(
        "install", "--no-index", "--find-links", str(wh), "--target", str(extras), "saltext-demo"
    )
    assert p.returncode == 0 and list(extras.glob("ownedlib*"))


def test_helper_refuses_a_wheel_that_needs_another_version_of_a_salt_package(
    env, make_wheelhouse, tmp_path
):
    base = make_wheelhouse({"ownedlib": ["1.5.0"]}, sub="salt-has")
    env.pip("install", "--no-index", "--find-links", str(base), "ownedlib")
    wh = make_wheelhouse(
        {"saltext-demo": ["1.0.0"]}, **{"saltext-demo": {"requires": ["ownedlib>=2"]}}
    )
    _owned(wh, "ownedlib==1.5.0")
    marker = tmp_path / "applied.json"
    r = env.apply({"saltext-demo": "1.0.0"}, wh, marker)
    assert r.returncode == 1 and not marker.exists()
    assert (
        "requires ownedlib>=2" in r.stderr
        and "this Salt ships ownedlib==1.5.0" in r.stderr
        and "never changes" in r.stderr
    )
    assert env.version("ownedlib") == "1.5.0" and env.version("saltext-demo") is None


def test_helper_refuses_to_pin_a_salt_package(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"ownedlib": ["2.0.0"]})
    _owned(wh, "ownedlib==1.5.0")
    r = env.apply({"ownedlib": "2.0.0"}, wh, tmp_path / "applied.json")
    assert r.returncode == 1 and "shipped by the Salt onedir" in r.stderr


def test_helper_rejects_a_tampered_owned_list(env, make_wheelhouse, tmp_path):
    wh = make_wheelhouse({"saltext-demo": ["1.0.0"]})
    _owned(wh, "ownedlib==1.5.0")
    (wh / "SALT_OWNED.txt").write_text(
        "# test\nsaltext-demo==1.0.0\n"
    )  # would make the helper skip our own package
    r = env.apply({"saltext-demo": "1.0.0"}, wh, tmp_path / "applied.json")
    assert r.returncode == 1 and "SHA-256 mismatch for SALT_OWNED.txt" in r.stderr
