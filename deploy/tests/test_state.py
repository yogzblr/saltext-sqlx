"""The minion state, rendered and run for real with `salt-call --local` (needs salt importable; real wheelhouse for the run tests)."""
import json
import os
import platform
import shutil
import site
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from conftest import STATE_ROOT, make_wheel, write_sums

salt = pytest.importorskip("salt", reason="salt is not installed in the interpreter running pytest")

PYV = f"{sys.version_info[0]}.{sys.version_info[1]}"
HOST = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
HOST_TARGET = f"linux-{HOST}" if platform.system() == "Linux" and HOST else None


def salt_call_exe():
    exe = Path(sys.executable).parent / "salt-call"
    return [str(exe)] if exe.exists() else [sys.executable, "-c", "from salt.scripts import salt_call; salt_call()"]


class Minion:
    """A `salt-call --local` sandbox: own root_dir, file_roots (deploy/salt + a roots dir holding the wheelhouse), pillar_roots."""

    def __init__(self, base: Path, python: Path, grains=None, cachedir_extra=None):
        self.base = base
        self.python = python
        self.fr2 = base / "fr2"
        self.pillar_dir = base / "pillar"
        self.conf = base / "conf"
        self.root = base / "root"
        for d in (self.fr2 / "saltexts" / "wheels", self.pillar_dir, self.conf):
            d.mkdir(parents=True, exist_ok=True)
        (self.pillar_dir / "top.sls").write_text("base:\n  '*':\n    - saltexts\n")
        cfg = [
            "id: statetest",
            f"root_dir: {self.root}",
            f"file_roots: {{base: ['{STATE_ROOT}', '{self.fr2}']}}",
            f"pillar_roots: {{base: ['{self.pillar_dir}']}}",
            "file_client: local",
            "fileserver_list_cache_time: 0",  # default is 20 s: a wheel added to the master is invisible for 20 s (documented)
            "log_level: warning",
        ]
        if grains:
            cfg.append("grains:")
            cfg += [f"  {k}: {json.dumps(v)}" for k, v in grains.items()]
        (self.conf / "minion").write_text("\n".join(cfg) + "\n")

    def pillar(self, **saltexts):
        import yaml

        (self.pillar_dir / "saltexts.sls").write_text(yaml.safe_dump({"saltexts": saltexts}))

    def call(self, *args, python=None):
        exe = [str(self.python.parent / "salt-call")] if (self.python.parent / "salt-call").exists() else salt_call_exe()
        env = dict(os.environ)
        r = subprocess.run([*exe, "--local", "-c", str(self.conf), "--out=json", "--out-indent=-1", *args],
                           capture_output=True, text=True, env=env)
        try:
            return json.loads(r.stdout)["local"], r
        except (ValueError, KeyError):
            raise AssertionError(f"salt-call failed (rc={r.returncode})\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")

    def apply(self, sls="saltexts", *args):
        data, raw = self.call("state.apply", sls, *args)
        assert isinstance(data, dict), data
        return {v["__id__"]: v for v in data.values()}

    def show(self, sls="saltexts"):
        data, raw = self.call("state.show_sls", sls)
        assert isinstance(data, dict), data
        return data


def _kwargs(item_list):
    out = {}
    for it in item_list:
        if isinstance(it, dict):
            out.update(it)
    return out


@pytest.fixture
def salt_env(tmp_path):
    """A venv that can import Salt (through a .pth) but has its own site-packages, so installs do not touch the test interpreter."""
    root = tmp_path / "saltenv"
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    py = root / "bin" / "python"
    sp = subprocess.run([str(py), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True).stdout.strip()
    paths = [p for p in site.getsitepackages() if Path(p).exists()]
    (Path(sp) / "zz_salt.pth").write_text("\n".join(paths) + "\n")
    # salt-call must be importable as a script from this venv
    (root / "bin" / "salt-call").write_text(f"#!{py}\nfrom salt.scripts import salt_call\nsalt_call()\n")
    (root / "bin" / "salt-call").chmod(0o755)
    return py


def _need_real(real_wheelhouse):
    versions = json.loads((real_wheelhouse / "VERSIONS.json").read_text())
    if versions["python"] != PYV or HOST_TARGET is None or not (real_wheelhouse / HOST_TARGET).is_dir():
        pytest.skip(f"wheelhouse python {versions['python']} / target {HOST_TARGET} does not match this interpreter ({PYV})")
    return versions


def _version(py, name):
    r = subprocess.run([str(py), "-c", f"import importlib.metadata as m;print(m.version({name!r}))"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


@pytest.fixture
def host_minion(tmp_path, salt_env, real_wheelhouse):
    versions = _need_real(real_wheelhouse)
    m = Minion(tmp_path / "m", salt_env)
    shutil.copytree(real_wheelhouse / HOST_TARGET, m.fr2 / "saltexts" / "wheels" / HOST_TARGET)
    wh = m.fr2 / "saltexts" / "wheels" / HOST_TARGET
    make_wheel(wh, "saltext-demo", "1.0.0")
    make_wheel(wh, "saltext-demo", "2.0.0")
    # SHA256SUMS of the shipped folder must list our extra test wheels too
    lines = (wh / "SHA256SUMS").read_text().splitlines()
    write_sums(wh)
    m.versions = versions
    m.base_pins = {
        "saltext-sqlx[postgres,mysql,oracle,mssql]": versions["own"]["saltext-sqlx"],
        "saltext-kafka": versions["own"]["saltext-kafka"],
        "saltext-demo": "1.0.0",
    }
    m.pillar(python=PYV, packages=m.base_pins, modules=[])
    return m


# ---- real runs -------------------------------------------------------------------------------------------------------------

def test_first_apply_installs_second_apply_is_zero_changes(host_minion):
    m = host_minion
    first = m.apply()
    assert all(v["result"] is True for v in first.values()), first
    assert first["saltexts_apply"]["changes"]["changed"] == "yes"
    assert _version(m.python, "saltext.sqlx") == m.base_pins["saltext-sqlx[postgres,mysql,oracle,mssql]"]
    assert _version(m.python, "psycopg") is not None
    second = m.apply()
    assert all(v["result"] is True and not v["changes"] for v in second.values()), {k: v["changes"] for k, v in second.items() if v["changes"]}
    assert second["saltexts_apply"]["comment"] == "unless condition is true"
    third = m.apply()
    assert all(v["result"] is True and not v["changes"] for v in third.values())


def test_changed_pin_changes_exactly_that_package(host_minion):
    m = host_minion
    m.apply()
    sqlx_before = _version(m.python, "saltext.sqlx")
    pins = dict(m.base_pins, **{"saltext-demo": "2.0.0"})
    m.pillar(python=PYV, packages=pins, modules=[])
    res = m.apply()
    assert res["saltexts_apply"]["changes"]["installed"] == {"saltext-demo": "2.0.0"}
    assert _version(m.python, "saltext-demo") == "2.0.0" and _version(m.python, "saltext.sqlx") == sqlx_before
    assert all(v["result"] is True for v in res.values())
    # rollback is just another pin change
    m.pillar(python=PYV, packages=m.base_pins, modules=[])
    assert m.apply()["saltexts_apply"]["changes"]["installed"] == {"saltext-demo": "1.0.0"}


def test_test_mode_changes_nothing(host_minion):
    m = host_minion
    res = m.apply("saltexts", "test=True")
    assert _version(m.python, "saltext.sqlx") is None
    assert not (m.root / "var" / "cache" / "salt" / "minion" / "saltext-state" / "applied.json").exists()
    assert not (m.root / "var" / "cache" / "salt" / "minion" / "saltext-state" / "pins.json").exists()
    assert any(v["result"] is None for v in res.values())  # "would change"
    m.apply()
    pins = dict(m.base_pins, **{"saltext-demo": "2.0.0"})
    m.pillar(python=PYV, packages=pins, modules=[])
    res = m.apply("saltexts", "test=True")
    assert _version(m.python, "saltext-demo") == "1.0.0"  # the pin change is reported, not applied
    assert res["saltexts_pins"]["result"] is None


def test_wrong_python_pillar_fails_loudly_and_installs_nothing(host_minion):
    m = host_minion
    m.pillar(python="3.9", packages=m.base_pins, modules=[])
    res = m.apply()
    assert list(res) == ["saltexts_python_mismatch"]
    assert res["saltexts_python_mismatch"]["result"] is False
    assert "built for Python 3.9" in res["saltexts_python_mismatch"]["comment"]
    assert _version(m.python, "saltext.sqlx") is None


def test_missing_wheel_fails_and_marker_is_not_written(host_minion):
    m = host_minion
    m.apply()
    marker = m.root / "var" / "cache" / "salt" / "minion" / "saltext-state" / "applied.json"
    good = marker.read_text()
    pins = dict(m.base_pins, **{"saltext-demo": "3.0.0"})  # no such wheel in the wheelhouse
    m.pillar(python=PYV, packages=pins, modules=[])
    res = m.apply()
    assert res["saltexts_apply"]["result"] is False
    assert "pip install failed" in res["saltexts_apply"]["comment"]
    assert marker.read_text() == good and _version(m.python, "saltext-demo") == "1.0.0"
    assert m.apply()["saltexts_apply"]["result"] is False  # and it stays red, it does not fix itself silently


def test_clean_true_removes_wheels_that_are_no_longer_shipped_but_keeps_state_files(host_minion):
    m = host_minion
    m.apply()
    cache = m.root / "var" / "cache" / "salt" / "minion"
    assert (cache / "saltext-wheels" / "saltext_demo-2.0.0-py3-none-any.whl").exists()
    wh = m.fr2 / "saltexts" / "wheels" / HOST_TARGET
    (wh / "saltext_demo-2.0.0-py3-none-any.whl").unlink()
    write_sums(wh)
    res = m.apply()
    assert not (cache / "saltext-wheels" / "saltext_demo-2.0.0-py3-none-any.whl").exists()
    assert (cache / "saltext-state" / "applied.json").exists() and (cache / "saltext-state" / "pins.json").exists()
    assert (cache / "saltext-state" / "saltexts_apply.py").exists()
    assert res["saltexts_pins"]["changes"] == {} and res["saltexts_helper"]["changes"] == {}
    assert res["saltexts_apply"]["result"] is True


def test_unsupported_platform_fails_loudly(tmp_path, salt_env):
    m = Minion(tmp_path / "m", salt_env, grains={"cpuarch": "riscv64"})
    m.pillar(python=PYV, packages={"x": "1"}, modules=[])
    res = m.apply()
    assert res["saltexts_unsupported_platform"]["result"] is False
    assert "no wheelhouse for linux-unknown" in res["saltexts_unsupported_platform"]["comment"]


def test_empty_pins_fail(tmp_path, salt_env):
    m = Minion(tmp_path / "m", salt_env)
    m.pillar(python=PYV, packages={}, modules=[])
    assert m.apply()["saltexts_no_pins"]["result"] is False


# ---- rendering the branches that cannot run here --------------------------------------------------------------------------

WIN_PY = r"C:\Program Files\Salt Project\Salt\bin\python.exe"
WIN_CACHE = r"C:\ProgramData\Salt Project\Salt\var\cache\salt\minion"


def _render(tmp_path, salt_env, grains, extra_cfg=None, pillar=None):
    m = Minion(tmp_path / "r", salt_env, grains=grains)
    m.pillar(**(pillar or dict(python=PYV, packages={"saltext-sqlx[postgres]": "0.1.0"}, modules=[])))
    return m.show()


@pytest.mark.parametrize("cpu,target", [("AMD64", "windows-x86_64"), ("ARM64", "windows-arm64")])
def test_render_windows(tmp_path, salt_env, cpu, target):
    grains = {"kernel": "Windows", "cpuarch": cpu, "pythonexecutable": WIN_PY, "pythonversion": [int(PYV.split(".")[0]), int(PYV.split(".")[1]), 0, "final", 0]}
    st = _render(tmp_path, salt_env, grains)
    assert _kwargs(st["saltexts_wheels"]["file"])["source"] == f"salt://saltexts/wheels/{target}"
    apply_ = _kwargs(st["saltexts_apply"]["cmd"])
    assert apply_["shell"] == "powershell" and apply_["stateful"] is True
    for cmd in (apply_["name"], apply_["unless"]):
        assert cmd.startswith(f"& '{WIN_PY}' ")
        assert '"' not in cmd                      # no double quote: nothing for list2cmdline to escape
        assert cmd.endswith("; exit $LASTEXITCODE")
        assert "saltext-state\\saltexts_apply.py'" in cmd and "saltext-wheels'" in cmd and "applied.json'" in cmd
        # joined with backslashes (the sandbox cachedir prefix is POSIX because root_dir is; a real Windows cachedir is all-backslash)
        assert "minion\\saltext-wheels'" in cmd and "minion\\saltext-state\\pins.json'" in cmd
    assert apply_["unless"].count("--check") == 1 and "--check" not in apply_["name"]
    # file modes are not requested on Windows
    wk = _kwargs(st["saltexts_wheels"]["file"])
    assert "file_mode" not in wk and "dir_mode" not in wk


def test_render_quotes_apostrophes_for_powershell_and_sh(tmp_path, salt_env):
    grains = {"kernel": "Windows", "cpuarch": "AMD64", "pythonexecutable": r"C:\Users\O'Brien\salt\python.exe",
              "pythonversion": [int(PYV.split(".")[0]), int(PYV.split(".")[1]), 0, "final", 0]}
    st = _render(tmp_path, salt_env, grains)
    assert _kwargs(st["saltexts_apply"]["cmd"])["name"].startswith(r"& 'C:\Users\O''Brien\salt\python.exe' ")
    grains = {"pythonexecutable": "/opt/o'brien/bin/python"}
    st = _render(tmp_path / "b", salt_env, grains)
    assert _kwargs(st["saltexts_apply"]["cmd"])["name"].startswith("'/opt/o'\\''brien/bin/python' ")


def test_render_linux_arm64_and_unsupported_darwin(tmp_path, salt_env):
    st = _render(tmp_path, salt_env, {"cpuarch": "aarch64"})
    assert _kwargs(st["saltexts_wheels"]["file"])["source"] == "salt://saltexts/wheels/linux-arm64"
    st = _render(tmp_path / "d", salt_env, {"kernel": "Darwin", "cpuarch": "arm64"})
    assert list(st) == ["saltexts_unsupported_platform"]


def test_render_pip_target_option(tmp_path, salt_env):
    st = _render(tmp_path, salt_env, {}, pillar=dict(python=PYV, packages={"a": "1"}, modules=[], pip_target="auto"))
    cmd = _kwargs(st["saltexts_apply"]["cmd"])
    assert cmd["name"].endswith("--target 'auto'")


@pytest.mark.parametrize("kernel,expect_shell", [("Linux", False), ("Windows", True)])
def test_restart_sls_uses_pillar_commands(tmp_path, salt_env, kernel, expect_shell):
    grains = {"kernel": kernel}
    m = Minion(tmp_path / "r", salt_env, grains=grains)
    m.pillar(python=PYV, packages={"a": "1"}, modules=[], restart_linux_cmd="touch /tmp/L", restart_windows_cmd="Write-Output W")
    st = m.show("saltexts.restart")
    kw = _kwargs(st["saltexts_restart"]["cmd"])
    assert kw["name"] == ("Write-Output W" if expect_shell else "touch /tmp/L")
    assert (kw.get("shell") == "powershell") is expect_shell


def test_restart_sls_defaults(tmp_path, salt_env):
    m = Minion(tmp_path / "r", salt_env)
    m.pillar(python=PYV, packages={"a": "1"}, modules=[])
    assert _kwargs(m.show("saltexts.restart")["saltexts_restart"]["cmd"])["name"] == \
        "systemd-run --on-active=10 --unit=saltexts-restart systemctl restart salt-minion"
    m = Minion(tmp_path / "w", salt_env, grains={"kernel": "Windows"})
    m.pillar(python=PYV, packages={"a": "1"}, modules=[])
    name = _kwargs(m.show("saltexts.restart")["saltexts_restart"]["cmd"])["name"]
    assert name == "Start-Process powershell -WindowStyle Hidden -ArgumentList '-Command','Start-Sleep 10; Restart-Service salt-minion'"
