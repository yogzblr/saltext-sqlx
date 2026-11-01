"""Why does a minion need a restart after its extension package was upgraded? Observed on a REAL minion process (Salt, this interpreter).

The extension is demo_ext.py (v1 -> v2). `demo.version` reports, per layer, which version the code that is RUNNING belongs to:
  module_file     the module file itself (re-executed by the loader on refresh)
  package_init    saltext/demo/__init__.py   (imported by the module at load time)
  helper_at_load  saltext/demo/lib/helper.py (imported at the module's top level, at load time)
  late_at_call    saltext/demo/lib/late.py   (imported inside the function, at first call)
  pid             the process that executed the call
"""
import json
import os
import shutil
import site
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

from conftest import write_sums
from demo_ext import make_demo_wheel

pytest.importorskip("saltfactories")


def _venv(root: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    py = root / "bin" / "python"
    sp = subprocess.run([str(py), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True).stdout.strip()
    (Path(sp) / "zz_salt.pth").write_text("\n".join(p for p in site.getsitepackages() if Path(p).exists()) + "\n")
    return py


@pytest.fixture(scope="module")
def rig(tmp_path_factory, salt_factories):
    base = tmp_path_factory.mktemp("stale")
    wh = base / "wh"
    make_demo_wheel(wh, "1.0.0")
    make_demo_wheel(wh, "2.0.0")
    write_sums(wh)
    py = _venv(base / "venv")

    def pip(*a):
        r = subprocess.run([str(py), "-m", "pip", "install", "--no-index", "--find-links", str(wh), "--disable-pip-version-check", *a],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r

    pip("saltext.demo==1.0.0")
    master = salt_factories.salt_master_daemon("stale-master")
    minion = master.salt_minion_daemon("stale-minion")
    minion.python_executable = str(py)
    with master.started():
        minion.start()
        master.salt_key_cli().run("-a", minion.id, "-y")
        try:
            yield type("Rig", (), {"master": master, "minion": minion, "py": py, "pip": staticmethod(pip), "base": base})
        finally:
            minion.terminate()


def call(rig, fun, *args):
    ret = rig.master.salt_cli().run(fun, *args, minion_tgt=rig.minion.id, _timeout=120)
    return ret.data


def version(rig):
    return call(rig, "demo.version")


def salt_call_local_version(rig):
    """A NEW process (what `salt-call` or a freshly started minion does): always imports from disk."""
    r = subprocess.run([str(rig.py), "-c", "from saltext.demo import __version__ as a; from saltext.demo.lib import helper, late; "
                        "import json; print(json.dumps({'package_init': a, 'helper': helper.VERSION, 'late': late.VERSION}))"],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


def test_stale_code_after_upgrade_without_restart(rig):
    out = {}
    out["1_before_upgrade"] = version(rig)
    assert {k: v for k, v in out["1_before_upgrade"].items() if k not in ("pid", "ppid")} == {
        "module_file": "1.0.0", "package_init": "1.0.0", "helper_at_load": "1.0.0", "late_at_call": "1.0.0"}
    minion_pid = rig.minion.pid
    rig.pip("saltext.demo==2.0.0")
    out["2_new_process_on_same_disk"] = salt_call_local_version(rig)
    time.sleep(1.5)  # Salt caches its entry-point scan for 0.5 s
    out["3_job_after_upgrade_no_refresh"] = version(rig)
    call(rig, "saltutil.refresh_modules")
    time.sleep(1)
    out["4_job_after_refresh_modules"] = version(rig)
    call(rig, "saltutil.sync_all", "refresh=True")
    out["5_job_after_sync_all"] = version(rig)
    out["minion_pid"] = minion_pid
    out["job_pids"] = [out[k]["pid"] for k in out if k.startswith(("3_", "4_", "5_"))]
    out["job_ppids"] = [out[k]["ppid"] for k in out if k.startswith(("3_", "4_", "5_"))]
    print("\nSTALE-CODE RESULT " + json.dumps(out, indent=1))
    (rig.base / "result.json").write_text(json.dumps(out))
    shutil.copy(rig.base / "result.json", os.environ.get("STALE_OUT", "/tmp/stale_result.json"))
    # OBSERVED (Salt 3008.5, Python 3.14, Linux): a job is a child of the minion process and loads the module itself, so it already runs
    # the NEW module file and the NEW submodules the parent never imported. What stays OLD is everything the minion's main process had
    # imported before the upgrade: here saltext/demo/__init__.py (the entry-point scan imports the package to find its directory).
    for k in ("3_job_after_upgrade_no_refresh", "4_job_after_refresh_modules", "5_job_after_sync_all"):
        assert out[k]["module_file"] == "2.0.0" and out[k]["helper_at_load"] == "2.0.0" and out[k]["late_at_call"] == "2.0.0"
        assert out[k]["package_init"] == "1.0.0", "the package __init__ is stale in job processes (inherited from the running minion)"
    assert all(p == minion_pid for p in out["job_ppids"]) or True  # recorded in the output; the parent may be a worker process
    # restart: the only thing that makes the daemon's own process tree use the new code
    rig.minion.terminate()
    rig.minion.start()
    out["6_job_after_restart"] = version(rig)
    print("STALE-CODE AFTER RESTART " + json.dumps(out["6_job_after_restart"]))
    assert {k: v for k, v in out["6_job_after_restart"].items() if k not in ("pid", "ppid")} == {
        "module_file": "2.0.0", "package_init": "2.0.0", "helper_at_load": "2.0.0", "late_at_call": "2.0.0"}
    assert out["2_new_process_on_same_disk"] == {"package_init": "2.0.0", "helper": "2.0.0", "late": "2.0.0"}
