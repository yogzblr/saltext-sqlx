"""orch.saltexts.rollout against a REAL Salt master and three REAL minions (pytest-salt-factories), minions in their own tmp roots and
their own virtualenvs. The only thing replaced is `systemd-run systemctl restart salt-minion`: the injected restart command asks a
watcher thread to stop/start the minion process for real (saltfactories terminate()/start()). Scenarios a-e of docs/DEPLOYMENT_CURSOR_PROMPT.md.

Run order matters (module scoped state): initial rollout -> no-op -> bump -> batches -> a minion that does not come back."""
import json
import os
import platform
import shutil
import site
import subprocess
import sys
import threading
import time
import venv
from pathlib import Path

import pytest

from conftest import STATE_ROOT, write_sums
from demo_ext import make_demo_wheel

pytest.importorskip("saltfactories")
PYV = f"{sys.version_info[0]}.{sys.version_info[1]}"
HOST = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
TARGET = f"linux-{HOST}" if platform.system() == "Linux" and HOST else None
MINIONS = ["om1", "om2", "om3"]  # sorted: batch composition is deterministic
WAIT_TIMEOUT = 60
LOG = []


def _venv(root: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    py = root / "bin" / "python"
    sp = subprocess.run([str(py), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True).stdout.strip()
    (Path(sp) / "zz_salt.pth").write_text("\n".join(p for p in site.getsitepackages() if Path(p).exists()) + "\n")
    return py


class Fleet:
    """Owns pillar files, wheelhouse, restart watcher and the helpers every scenario uses."""

    def __init__(self, base: Path, master, minions, versions):
        self.base, self.master, self.minions, self.versions = base, master, minions, versions
        self.pillar_dir = base / "pillar"
        self.req_dir = base / "restart-requests"
        self.req_dir.mkdir(exist_ok=True)
        self.behaviour = {m: "restart" for m in MINIONS}   # restart | down | ignore
        self.restart_log = []                              # (time, id, what)
        self.seen = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self.demo = "1.0.0"
        self.extra = {}

    # -- pillar -------------------------------------------------------------------------------------------------------------
    def write_pillar(self, **over):
        v = self.versions
        pins = {
            "saltext-sqlx[postgres,mysql,oracle,mssql]": v["own"]["saltext-sqlx"],
            "saltext-kafka": v["own"]["saltext-kafka"],
            "saltext.demo": self.demo,
        }
        cmd = f"sh -c 'date +%s.%N >> {self.req_dir}/{{{{ grains[\"id\"] }}}}.req'"
        body = {
            "python": PYV,
            "packages": pins,
            "modules": ["sqlx", "kafka", "demo"],
            "restart_linux_cmd": cmd,
        }
        body.update(self.extra)
        body.update(over)
        import yaml

        (self.pillar_dir / "saltexts.sls").write_text(yaml.safe_dump({"saltexts": body}).replace("'sh -c", "'sh -c"))

    # -- restart watcher: turns the injected "restart" command into a REAL stop/start of the minion process ---------------------
    def _do_restart(self, mid, fac, mode):
        time.sleep(3)  # like `systemd-run --on-active=10`: the job returned before the restart
        fac.terminate()
        self.restart_log.append((time.time(), mid, "stopped"))
        if mode == "restart":
            fac.start(max_start_attempts=3)
            self.restart_log.append((time.time(), mid, "started"))

    def _watch(self):
        while not self._stop.is_set():
            for mid, fac in self.minions.items():
                req = self.req_dir / f"{mid}.req"
                if req.exists():
                    n = len(req.read_text().split())
                    if n > self.seen.get(mid, 0):
                        self.seen[mid] = n
                        mode = self.behaviour[mid]
                        self.restart_log.append((time.time(), mid, f"requested ({mode})"))
                        if mode != "ignore":   # one thread per minion: they restart in parallel, like real machines
                            threading.Thread(target=self._do_restart, args=(mid, fac, mode), daemon=True).start()
            time.sleep(0.2)

    def start_watcher(self):
        self._thread.start()

    def stop_watcher(self):
        self._stop.set()
        self._thread.join(timeout=30)

    # -- helpers -------------------------------------------------------------------------------------------------------------
    def pids(self):
        return {m: f.pid for m, f in self.minions.items()}

    def rollout(self, timeout=600, **pillar):
        pillar.setdefault("tgt", "G@saltexts:managed")
        pillar.setdefault("wait_timeout", WAIT_TIMEOUT)
        pillar.setdefault("verify_interval", 2)
        pillar.setdefault("verify_attempts", 8)
        cli = self.master.salt_run_cli()
        t0 = time.time()
        ret = cli.run("state.orchestrate", "orch.saltexts.rollout", f"pillar={json.dumps(pillar)}", "--out=json", "--out-indent=-1", _timeout=timeout)
        data = ret.data
        steps = {}
        if isinstance(data, dict):
            body = data.get("data", data)
            for host, states in body.items():
                if isinstance(states, dict):
                    for key, v in states.items():
                        if isinstance(v, dict) and "__id__" in v:
                            steps[v["__id__"]] = v
        return type("Run", (), {"ret": ret, "steps": steps, "data": data, "seconds": time.time() - t0})

    def minion_state(self, mid, fun, *args):
        ret = self.master.salt_cli().run(fun, *args, minion_tgt=mid, _timeout=120)
        return ret.data

    def demo_version(self, mid):
        return self.minion_state(mid, "demo.version")


def changed(step):
    return bool(step.get("changes"))


def skipped_by_onchanges(step):
    return step["result"] is True and "onchanges" in step["comment"].lower() and not step.get("changes")


@pytest.fixture(scope="module")
def fleet(tmp_path_factory, salt_factories, real_wheelhouse):
    versions = json.loads((real_wheelhouse / "VERSIONS.json").read_text())
    if TARGET is None or versions["python"] != PYV or not (real_wheelhouse / TARGET).is_dir():
        pytest.skip(f"wheelhouse python {versions['python']} / target {TARGET} does not match this interpreter ({PYV})")
    base = tmp_path_factory.mktemp("orch")
    fr2 = base / "fr2"
    wh = fr2 / "saltexts" / "wheels" / TARGET
    shutil.copytree(real_wheelhouse / TARGET, wh)
    make_demo_wheel(wh, "1.0.0")
    make_demo_wheel(wh, "2.0.0")
    write_sums(wh)
    pillar_dir = base / "pillar"
    pillar_dir.mkdir()
    (pillar_dir / "top.sls").write_text("base:\n  '*':\n    - saltexts\n")
    master = salt_factories.salt_master_daemon(
        "orch-master",
        overrides={
            "file_roots": {"base": [str(STATE_ROOT), str(fr2)]},
            "pillar_roots": {"base": [str(pillar_dir)]},
            "fileserver_list_cache_time": 1,
            "worker_threads": 4,
        },
    )
    minions = {}
    for mid in MINIONS:
        py = _venv(base / f"venv-{mid}")
        minions[mid] = master.salt_minion_daemon(
            mid, overrides={"grains": {"saltexts": "managed", "role": "canary" if mid == "om1" else "fleet"}},
        )
        minions[mid].python_executable = str(py)   # each minion runs in its own virtualenv: installs must not leak between them
    f = Fleet(base, master, minions, versions)
    f.write_pillar()
    with master.started():
        for fac in minions.values():
            fac.start()
        for fac in minions.values():
            master.salt_key_cli().run("-a", fac.id, "-y")
        f.start_watcher()
        try:
            yield f
        finally:
            f.stop_watcher()
            for fac in minions.values():
                fac.terminate()


def _installed(fleet, mid, name="saltext.demo"):
    py = fleet.base / f"venv-{mid}" / "bin" / "python"
    r = subprocess.run([str(py), "-c", f"import importlib.metadata as m;print(m.version({name!r}))"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def test_minions_connected_and_use_their_own_python(fleet):
    for mid in MINIONS:
        assert fleet.minion_state(mid, "test.ping") is True
        exe = fleet.minion_state(mid, "grains.item", "pythonexecutable")["pythonexecutable"]
        assert exe == str(fleet.base / f"venv-{mid}" / "bin" / "python")


def test_initial_rollout_installs_restarts_waits_and_verifies(fleet):
    pids = fleet.pids()
    run_t0 = time.time()
    run = fleet.rollout(batch_size=10)
    s = run.steps
    print(json.dumps({k: (v["result"], v["comment"][:90]) for k, v in s.items()}, indent=1))
    print("RESTART LOG", [(round(t - run_t0, 1), m, w) for t, m, w in fleet.restart_log])
    assert run.ret.returncode == 0, run.ret
    assert set(s) == {"stage_1", "apply_1", "restart_1", "wait_1", "verify_1"}
    assert all(v["result"] is True for v in s.values())
    assert changed(s["apply_1"]) and not skipped_by_onchanges(s["restart_1"]) and not skipped_by_onchanges(s["wait_1"])
    assert sorted(s["wait_1"]["changes"]["minions_seen"]) == MINIONS
    for mid in MINIONS:
        assert _installed(fleet, mid) == "1.0.0"
        assert fleet.demo_version(mid)["module_file"] == "1.0.0"
    new = fleet.pids()
    assert all(new[m] != pids[m] for m in MINIONS), (pids, new)


def test_a_nothing_changed_skips_restart_and_wait_and_verify_passes(fleet):
    pids = fleet.pids()
    log_len = len(fleet.restart_log)
    run = fleet.rollout(batch_size=10)
    s = run.steps
    print(json.dumps({k: (v["result"], v["comment"][:110]) for k, v in s.items()}, indent=1))
    assert run.ret.returncode == 0
    assert all(v["result"] is True for v in s.values())
    assert not changed(s["stage_1"]) and not changed(s["apply_1"])
    assert skipped_by_onchanges(s["restart_1"]) and skipped_by_onchanges(s["wait_1"])
    assert s["verify_1"]["result"] is True
    assert fleet.pids() == pids and len(fleet.restart_log) == log_len, "a no-op run must not restart anybody"
    assert run.seconds < 60


def test_b_pin_change_applies_restarts_the_batch_waits_for_start_events_and_verifies(fleet):
    fleet.demo = "2.0.0"
    fleet.write_pillar()
    pids = fleet.pids()
    run = fleet.rollout(batch_size=10)
    s = run.steps
    print(json.dumps({k: (v["result"], v["comment"][:110]) for k, v in s.items()}, indent=1))
    assert run.ret.returncode == 0
    assert all(v["result"] is True for v in s.values())
    ap = s["apply_1"]["changes"]["ret"]
    for mid in MINIONS:
        installed = next(iter(ap[mid].values()))["changes"]["installed"]
        assert installed == {"saltext.demo": "2.0.0"}   # exactly that package
        assert _installed(fleet, mid) == "2.0.0"
        assert fleet.demo_version(mid)["module_file"] == "2.0.0"
    assert sorted(s["wait_1"]["changes"]["minions_seen"]) == MINIONS
    new = fleet.pids()
    assert all(new[m] != pids[m] for m in MINIONS)
    # the restart command ran on every minion of the batch, and only after apply
    assert {m for _, m, w in fleet.restart_log if w.startswith("requested")} == set(MINIONS)


def test_d_batch_size_splits_and_next_batch_waits_for_the_previous_verify(fleet):
    fleet.demo = "1.0.0"   # rollback = an ordinary rollout
    fleet.write_pillar()
    run = fleet.rollout(batch_size=2)
    s = run.steps
    print(json.dumps({k: (v["result"], v["__run_num__"]) for k, v in s.items()}, indent=1))
    assert run.ret.returncode == 0
    assert {f"{k}_{n}" for n in (1, 2) for k in ("stage", "apply", "restart", "wait", "verify")} == set(s)
    assert all(v["result"] is True for v in s.values())
    assert set(s["apply_1"]["changes"]["ret"]) == {"om1", "om2"} and set(s["apply_2"]["changes"]["ret"]) == {"om3"}
    assert s["verify_1"]["__run_num__"] < s["stage_2"]["__run_num__"]
    for mid in MINIONS:
        assert _installed(fleet, mid) == "1.0.0"


def test_c_minion_that_does_not_come_back_fails_the_run_and_the_next_batch_never_runs(fleet):
    fleet.demo = "2.0.0"
    fleet.write_pillar()
    fleet.behaviour["om1"] = "down"          # the first batch contains om1 only (batch_size=1, sorted ids)
    try:
        run = fleet.rollout(batch_size=1, timeout=300)
        s = run.steps
        print(json.dumps({k: (v["result"], v["comment"][:100]) for k, v in s.items()}, indent=1))
        assert run.ret.returncode != 0
        assert s["wait_1"]["result"] is False and "Timeout" in s["wait_1"]["comment"]
        assert "stage_2" not in s or s["stage_2"]["result"] is not True
        # om2 and om3 were never touched
        assert _installed(fleet, "om2") == "1.0.0" and _installed(fleet, "om3") == "1.0.0"
        assert not any(v["result"] is True and k.endswith("_2") and changed(v) for k, v in s.items())
    finally:
        fleet.behaviour["om1"] = "restart"
        fac = fleet.minions["om1"]
        if not fac.is_running():
            fac.start()
    # a later rollout converges the whole fleet
    run = fleet.rollout(batch_size=10)
    assert run.ret.returncode == 0, run.ret
    for mid in MINIONS:
        assert _installed(fleet, mid) == "2.0.0"


def test_empty_target_is_a_failure_not_a_silent_success(fleet):
    run = fleet.rollout(tgt="G@role:does-not-exist")
    assert run.ret.returncode != 0
    assert run.steps["saltexts_no_minions_up"]["result"] is False
