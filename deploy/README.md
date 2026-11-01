# deploy/ — pinned Salt extensions from the Salt master, no internet on the minions

Wheelhouse (built once, in CI) → served by the master as `salt://saltexts/wheels/<kernel>-<arch>` → `saltexts` state installs the
pinned wheels offline on each minion → `orch.saltexts.rollout` updates, restarts and verifies the minions in batches.

```
deploy/
  wheelhouse/            build_own_wheels.sh  build_wheelhouse.sh  wheelhouse_tool.py  refresh_salt_locks.sh
                         requirements/ (pins, frozen constraints, salt-locks/<tag>/ = what the Salt onedir already ships)
  salt/saltexts/         init.sls (= stage + apply)  stage.sls  apply.sls  restart.sls  verify.sls  map.jinja  files/saltexts_apply.py
  salt/orch/saltexts/    rollout.sls
  pillar/                saltexts.sls.example          master.d/  roots.conf  saltexts-schedule.conf (hourly, opt-in)
  Dockerfile.master      master image with everything baked in (NOT built in the sandbox)
  tests/                 see tests/README.md
```

## What is and is not verified

Everything below was run on **one Linux x86_64 host, Salt 3008.5, CPython 3.14.6**, 2026-11-01, unless stated. "Reasoned" means read
from Salt's source or worked out, not executed.

| Area | Status |
|---|---|
| Wheelhouse build for linux-x86_64, linux-arm64, windows-x86_64, windows-arm64 (Python 3.14 and 3.11), offline dry-run install per target, SHA256SUMS, VERSIONS.json, licence gate | **Verified** (it builds; the cross-built wheels were never installed on their own platform) |
| Installing the linux-x86_64 wheelhouse offline into a Salt 3008.5 / Python 3.14 environment, `pip check`, the `sqlx` and `kafka` modules load | **Verified** |
| Helper `saltexts_apply.py` (idempotency, bump, extras, failure, `pip check`, checksums, spaces in paths, target dir, never touching Salt-owned packages) | **Verified** with real venvs and pip; the Salt-owned set was simulated with metadata stubs in the test venvs, and once with a real `salt==3008.5` installation (and once in a real `salt==3008.5` environment installed with exactly the versions of Salt's own lock: `pip freeze` of the base environment identical before and after, no Salt-owned package in the extras directory, `pip check` clean, `cryptography` imported from Salt's copy) |
| Minion state through `salt-call --local`: first apply, second apply = zero changes, one pin = one package, `test=True`, wrong Python, unsupported platform, missing wheel, `clean: True` | **Verified** on Linux x86_64 |
| Windows and ARM64 branches of the state | **Rendered and reviewed only** (the exact `cmd.run` text is asserted by a test). **Not executed.** |
| Orchestration on a real master and 3 real minions (batches, restart for real by stopping/starting the minion process, start-event wait, retry, parallel, onchanges, failhard, a minion that never returns) | **Verified** on Linux x86_64 |
| `systemd-run --on-active=10 ... systemctl restart salt-minion` and the PowerShell `Restart-Service` | **Not testable here.** The tests inject a command that makes a watcher stop/start the minion process. |
| Salt onedir specifics (`salt-pip`, `extras-3.N`, `pip_target: auto`, a onedir's own pip) | **Not testable here** (no onedir; the sandbox cannot reach packages.broadcom.com). Plain venvs were used. |
| `Dockerfile.master` | **Not built** (no registry). Reviewed line by line. |
| Windows file locking of loaded `.pyd` files during an upgrade, Windows service restart | **Not tested; reasoned** (see "Windows") |
| Master `schedule:` entry (`saltexts-schedule.conf`) | **Not run** (see "Schedule") |
| Our own Kafka extension | **Not used.** The tests use the upstream `salt-extensions/saltext-kafka` as a **test stand-in** (`KAFKA_REPO`/`KAFKA_REF`). |

## Which Python? (decided from Salt's source, not from memory)

The wheels are built for ONE CPython minor version, the one inside the minions' Salt onedir. `cicd/shared-gh-workflows-context.yml`
(`python_version`) at each Salt tag:

| Salt | onedir Python |
|---|---|
| 3008.x (v3008.5 checked) | **3.14** (3.14.7; the release notes say "Update python 3.14 to 3.14.5") |
| 3007.x up to v3007.14 | 3.10 |
| 3007.x from 3007.15 (branch head 3.11.16), 3006.27+ | **3.11** |
| 3006.x up to 3006.26 | 3.10 |

`tests-e2e/` independently observed 3.14.7 in the 3008.5 onedir. **`PYVER` defaults to 3.14.** 3.11 builds too (own lock files
`constraints.py311.*`). **3.10 does not**: `pymongo==4.19.0` has no wheel for it, so a fleet with Salt 3006.<=26 / 3007.<=14 needs its own pins
or an upgrade first. Upgrading Salt across a Python minor version also moves the onedir's `extras-3.N` directory
(release notes 3006.27/3008.2/3008.3): extensions installed there must be reinstalled, which this state does by itself (pins vs installed).

## Respect what Salt ships

The Salt onedir already contains a lot of Python packages, and the extensions run inside that Python. A wheelhouse that carries its own
copy of one of them is wrong twice: it makes the download bigger, and `pip install --target <extras-3.N>` puts a second copy next to the
one Salt runs on (which one wins then depends on `sys.path`). Salt publishes the exact list per release:
`requirements/static/pkg/py<X.Y>/{linux,windows}.lock` in github.com/saltstack/salt. Found there (tag v3008.5, Python 3.14):

| | Linux | Windows |
|---|---|---|
| `cryptography` | 50.0.1 | 50.0.1 |
| `pyopenssl` | 26.4.0 | 26.4.0 |
| `pymssql` | not shipped | **2.3.11** (`requirements/base.txt`: `pymssql==2.3.11; sys_platform == 'win32'`) |
| `certifi`, `idna`, `urllib3`, `typing-extensions`, `six`, `python-dateutil`, `cffi`, `pycparser`, `requests`, `packaging`, `pyyaml`, `jinja2`, `markupsafe` ... | shipped (79 packages in all) | shipped (87) |
| `pg8000`, `psycopg`, `PyMySQL`, `oracledb`, `SQLAlchemy`, `redis`, `pymongo`, `elasticsearch`, kafka clients | **not shipped** | **not shipped** (PyMySQL and tzdata are shipped by the Salt 3007.x Windows onedir, not by 3008.5) |

What the build does with that (`SALT_TAG`, default `v3008.5` for Python 3.14 and `v3007.15` for 3.11; `v3006.27` is vendored too):

1. The lock files are vendored under `wheelhouse/requirements/salt-locks/<tag>/py<X.Y>.<linux|windows>.lock`, each with a header (tag, date,
   source URL). `refresh_salt_locks.sh [TAG:PYVER ...]` re-downloads them. The 3.11 lines differ from each other (cryptography 47 / 48 / 50),
   so each Salt line has its own lock; a 3.11 wheelhouse is built against one of them. Linux targets use the `linux` lock, Windows targets the
   `windows` lock (Salt publishes no Windows ARM64 onedir that we could find in its CI matrix at v3008.2, so that target uses the Windows lock
   as the best available statement; unverified).
2. **Pins are checked first** (`wheelhouse_tool.py check-pins`). A requirement line that names a Salt-owned package with a version other
   than Salt's fails the build before anything is downloaded: `ERROR requirements.common.txt:12: 'cryptography==50.0.2' - the Salt onedir
   (v3008.5, python 3.14, linux) already ships cryptography==50.0.1 ... Remove the pin ...`. A pin that is meant only for platforms where
   Salt does not ship the package carries the comment `# salt-wins`; where Salt ships it the line is dropped with a `NOTE:` (PyMySQL on a
   3.11 Windows Salt, tzdata).
3. **The resolver is told the Salt packages are installed.** For every package of the lock a metadata-only stub wheel is created at Salt's
   version plus a local label (`cryptography-50.0.1+salt3008.5`; PEP 440: `==50.0.1` matches it and it sorts above the real
   `50.0.1`), and Salt's versions are passed as constraints. `pip download` therefore never fetches those packages, and a dependency that
   wants a *newer* one is backtracked or refused by pip itself. Observed: `anyio 4.15.1` needs `typing_extensions>=4.16`, Salt ships 4.15.0,
   so the wheelhouse has `anyio 4.14.2`. The stubs are deleted from the output (`wheelhouse_tool.py strip`, run once to delete and once with
   `--check` to prove none is left) and the offline dry-run install uses the stubs again, i.e. it checks the wheelhouse **plus a Salt**.
4. Each target folder gets `SALT_OWNED.txt` (the lock as `name==version`, listed in `SHA256SUMS`).
5. **Windows `pymssql`:** x86_64 gets none: Salt's 2.3.11 satisfies the `mssql` extra. **2.3.11 has wheels for win32 and win_amd64 only (cp39 - cp314),
   none for win_arm64** (PyPI file list checked; 2.4.6 does have cp314 win_arm64 wheels, but it is not Salt's version), so the Windows ARM64
   target is built without the `mssql` extra. Linux gets `pymssql==2.4.6` (Salt ships none there).

**Size of the wheelhouse (Python 3.14, measured on the built folders).** "Before" is the wheelhouse before respecting Salt's lock files;
"after A" is that change alone; "after B" adds the move from psycopg to pg8000 (`psycopg` + `psycopg-binary` out, `pg8000` + `scramp`
+ `asn1crypto` in; Windows ARM64 gains PostgreSQL).

| Target | before | after A (Salt-owned) | after B (+ pg8000) |
|---|---|---|---|
| linux-x86_64 | 31 wheels, 23.9 MB | 21 wheels, 18.3 MB | 22 wheels, 13.3 MB |
| linux-arm64 | 31 wheels, 25.1 MB | 21 wheels, 19.5 MB | 22 wheels, 12.9 MB |
| windows-x86_64 | 31 wheels, 18.4 MB | 21 wheels, 11.8 MB | 22 wheels, 8.2 MB |
| windows-arm64 | 29 wheels, 12.8 MB | 19 wheels, 7.5 MB | 22 wheels, 7.7 MB (now with PostgreSQL) |

(`du` of each target folder, wheels plus `SHA256SUMS` and `SALT_OWNED.txt`; Python 3.11 against Salt v3007.15: 22 / 22 / 20 / 20 wheels.)

The minion helper obeys the same list. `pip install --target DIR` forces `--ignore-installed`, so with a target directory pip does not see
Salt's packages and would install every dependency again, `cryptography` included, into the extras directory (shown by
`test_helper_without_the_owned_list_in_target_mode_would_have_copied_it`). The helper therefore does not ask pip to resolve anything: it reads
the `Requires-Dist` of the wheels (markers evaluated for the minion's own interpreter), installs the wheelhouse wheels that are needed and not
installed yet with `pip install --no-deps`, leaves every package in `SALT_OWNED.txt` alone, refuses with a message naming the requirement when a
wheel needs a different version of a Salt-owned package (`... requires cryptography>=51, but this Salt ships cryptography==50.0.1 and the helper never
changes a package Salt ships`), and runs `pip check` as before. A wheelhouse without `SALT_OWNED.txt` is handled as before (no package is special).

## Build the wheelhouse

```bash
# our own wheels (stamped with an explicit version: an untagged checkout gives a dev version that cannot be pinned)
OWN=/tmp/own SQLX_DIR=$PWD SQLX_VERSION=0.1.0 \
  KAFKA_REPO=<our kafka repo> KAFKA_REF=<tag> KAFKA_VERSION=<x.y.z> bash deploy/wheelhouse/build_own_wheels.sh
# the wheelhouse for all four targets (pip >= 24, needs PyPI once)
OWN=/tmp/own OUT=/tmp/out PYVER=3.14 bash deploy/wheelhouse/build_wheelhouse.sh
```

`KAFKA_REPO` defaults in CI and in the Dockerfile to the **upstream** `salt-extensions/saltext-kafka`. That is a **stand-in for tests
only** (administration only, cannot read messages). The real deployment must build our own extension; never install both
(same `saltext.kafka` namespace).

Output in `$OUT`: `<target>/*.whl` + `<target>/SHA256SUMS`, `SHA256SUMS.all`, `VERSIONS.json` (resolved versions of our packages and every
wheel per target), `LICENCES.md`, `build-<target>.log`. Rules enforced by the script:

* `set -euo pipefail`; every target is attempted, failures are listed with **package + target** at the end and the exit status is
  non-zero; an incomplete wheelhouse gets no `VERSIONS.json`.
* `salt` is satisfied by a **build-time stub wheel** (`wheelhouse_tool.py stub`). Every extension declares `salt>=N`, and Salt is only
  published as an sdist, so `pip download --only-binary=:all:` could not otherwise finish. The stub is never copied to the output.
* Transitive versions are frozen in `requirements/constraints.py<PYV>.<target>.txt` (regenerate with `LOCK_UPDATE=1` when you
  change a pin, review the diff, commit). Without the lock a rebuild a month later would ship different versions.
* An offline `pip install --dry-run --no-index` of the finished folder (plus the stubs of Salt's own packages) proves it is complete.
* Licence gate: wheel metadata of all four targets; GPL/AGPL fails; LGPL is accepted (pymssql; psycopg only if a minion opts in with `postgres-psycopg`); MPL (certifi)
  is listed as "weak" for information. Where this host matches a target, `pip-licenses` is run on the installed set as a second opinion.

### Known gaps found by the real build (Python 3.14 and 3.11)

| Finding | Detail |
|---|---|
| `psycopg-binary` has **no wheel for win_arm64** | confirmed for cp311 and cp314 ("from versions: none"). This is why the default PostgreSQL driver is now `pg8000` (pure Python, `py3-none-any`): every target, Windows ARM64 included, gets `saltext-sqlx[postgres,...]`. Profiles that ask for `driver: psycopg` need the `postgres-psycopg` extra, which no target of this wheelhouse installs. |
| `redis-py-cluster 2.1.3` pins `redis<4` | confirmed from its metadata. `saltext-redis` declares an unpinned `redis`; both are pinned: `redis==3.5.3`. Whether `saltext-redis` works with redis-py 3.5 is **not tested**. |
| `saltext-elasticsearch` does **not** declare the `elasticsearch` client | confirmed (only under its `tests` extra). Pin it yourself (`elasticsearch==9.5.1`, a **guess**: it must match the server major; the extension has modules for 6, 8 and 9). |
| `cryptography` / `pyopenssl` | no longer built at all: Salt ships them (see "Respect what Salt ships"). The earlier `cryptography` 46.0.3 on win_arm64 vs 50.0.2 elsewhere is gone with them. |
| `pymssql 2.3.11` (Salt's Windows pin) has no win_arm64 wheel | the Windows ARM64 target has no `mssql` extra (it does have PostgreSQL, through pg8000). |
| `pymssql 2.4.6` needs manylinux_2_28 / 2_27 | glibc >= 2.28 on Linux minions (RHEL 7 / CentOS 7 / Ubuntu 18.04 are out). |
| Python 3.10 | `pymongo 4.19.0` has no cp310 wheel (see above). |
| Wheel names of the extensions differ (`saltext.redis-...` vs `saltext_kafka-...`) | harmless; the helper matches distributions by normalised name. |

## Bump a version and roll it out

1. Build the new wheel(s) (tag the extension, `*_VERSION=...`), run `build_wheelhouse.sh` (with `LOCK_UPDATE=1` if a pin changed),
   check `VERSIONS.json`, `LICENCES.md`, `SHA256SUMS.all`. Publish the folder tree as `/srv/salt/saltexts/wheels/` on the master
   (read-only for everybody but the build pipeline; or ship a new master image tag).
2. Change the pin in the pillar (`saltexts:packages`). **That is the only place a version lives.**
3. Canary, then fleet (each command is the same orchestration; only the target and the batch size change):

```bash
# canary: one batch, stops at the first failure
salt-run state.orchestrate orch.saltexts.rollout pillar='{"tgt":"G@role:canary","batch_size":2}'
# 10 % waves, then everybody
salt-run state.orchestrate orch.saltexts.rollout pillar='{"tgt":"G@saltexts:managed","batch_size":50}'
# dry run (test=True: nothing installed, nothing restarted, no waiting)
salt-run state.orchestrate orch.saltexts.rollout test=True pillar='{"tgt":"G@role:canary"}'
```

Tunables (orchestration pillar): `tgt`, `batch_size` (10), `wait_timeout` (600 s), `verify_attempts`/`verify_interval` (12 x 10 s),
`restart` (true), `force_restart` (false).

`test=True` (tested): `stage` reports what it would copy, nothing is installed, restarted or waited for. It cannot predict `apply` (the pins file
that `apply` checks is only written by `stage`), so a dry run shows the pending wheel/pin changes under `stage`, not under `apply`.

**Interrupted run.** `restart` is tied to "apply installed something". If the master, the orchestration or the network dies after `apply` but
before the restart, the next run sees `changed=no` and restarts nobody; the minions keep running the old main-process code (see the next
section) until something restarts them. Recover with `... pillar='{"tgt":"...","force_restart":true}'` (tested: restarts and waits even though
nothing changed).

What one batch does: `stage` (copy wheels, helper and pins) → `apply` (`pip install`, offline) → **only if apply installed something:**
`restart` (delayed minion restart) and `wait` for `salt/minion/<id>/start` of every minion of the batch, started in parallel so no event is
missed → `verify` (the modules in `saltexts:modules` are loaded in the *running* minion; retried while the minions reconnect). Every step is
`failhard`: the first failure ends the run, so **a failed batch is never followed by the next one**. An empty target is a failure, not a green run.

Stop criteria for a human: any red step; `wait` timing out (a minion did not come back: look at the minion service and its log before
doing anything else); `verify` red.

### Roll back

Rollback is an ordinary rollout with the previous pins (and the previous wheelhouse, which still contains the old wheels as long as you
did not remove them; `clean: True` makes the minions drop wheels the master no longer ships, so keep the old version in the wheelhouse
for as long as a rollback may be needed). Re-pin, then run the same commands. Tested: 1.0.0 → 2.0.0 → 1.0.0 across batches.

### Hourly convergence (opt-in)

`master.d/saltexts-schedule.conf` runs the rollout every hour; it is a no-op when nothing changed (tested: `changed=no`, restart and wait skipped).
It is **not installed by default** (the Docker image ships it as `*.disabled`). Enable it only after the first full rollout converged. The syntax of a
master-side `schedule` entry that calls a runner was **not run** here; test it on a staging master first.

## Is the restart mandatory? (stale-code experiment, `tests/test_stale_code.py`)

Observed on a real minion (Salt 3008.5, CPython 3.14, Linux): extension `saltext.demo` 1.0.0 loaded and called; 2.0.0 installed with pip
**while the minion kept running**; then calls through the master:

| Layer (what each reports) | after upgrade, no refresh | after `saltutil.refresh_modules` | after `saltutil.sync_all` | after minion restart |
|---|---|---|---|---|
| module file (`demo_mod.py`) | **2.0.0** | 2.0.0 | 2.0.0 | 2.0.0 |
| submodule imported at module top (`lib/helper.py`) | **2.0.0** | 2.0.0 | 2.0.0 | 2.0.0 |
| submodule imported at call time (`lib/late.py`) | **2.0.0** | 2.0.0 | 2.0.0 | 2.0.0 |
| package `__init__.py` (`saltext/demo/__init__.py`) | **1.0.0 (stale)** | 1.0.0 (stale) | 1.0.0 (stale) | 2.0.0 |

Every call ran in a **new job process** (a different pid each time, children of the minion). Job processes load the module file
and the submodules from disk themselves, so they see the new code even without `refresh_modules`; but the minion's main process had
already imported `saltext/demo/__init__.py` (Salt's entry-point scan imports the package to find its `modules/` directory) and the
children inherit it. A fresh `salt-call` or a fresh Python sees everything new.

Consequences: for an extension that is only execution modules and whose `__init__.py` is trivial, a restart is **not strictly required on
Linux**; a job can still mix old and new code (`__init__` old, module new). Anything the main process loads itself — engines, beacons,
the scheduler, grains, `__init__` side effects — stays old until restart. The state keeps the restart as the default (a mixed old/new
process is the worst thing to debug); `restart: false` skips it. **Not tested:** Windows (jobs are spawned, not forked, and loaded `.pyd`/`.dll`
files such as the driver extensions are locked, which can make pip fail to replace them while the minion runs), `salt-call` as a daemon
under systemd on a onedir, extensions with engines/beacons.

## Troubleshoot

| Symptom | Cause / action |
|---|---|
| `saltexts: wheels are built for Python X, this minion's Salt has Y` | `saltexts:python` pillar (or the wheelhouse) does not match the minion's Salt. A Salt upgrade across 3.10/3.11/3.14 needs a new wheelhouse. |
| `SHA-256 mismatch` / `X is listed in SHA256SUMS but missing` right after publishing a wheelhouse | the master's file list cache (`fileserver_list_cache_time`, default 20 s) served a new `SHA256SUMS` with the old file list. The check refuses to install a half-copied wheelhouse; the next run succeeds. Observed. |
| `pip install failed ... No matching distribution` | a pin that is not in the wheelhouse, or a dependency missing for this target. Marker untouched, the state stays red every run until fixed. |
| `pip check failed ... marker NOT written` | the installed set is broken (a package of the Salt onedir conflicts with a wheel). The packages are installed, the state stays red. Look at the message. |
| `wait_N` times out | the minion did not send `salt/minion/<id>/start`: service down, the restart command failed (look at `saltexts-restart.service` / the Windows service), or the master cannot see the minion. Nothing after that batch ran. |
| `verify_N` red: `module 'x' is not loaded` | the extension or one of its drivers is missing, or the minion still runs without the restart (`restart: false`). |
| `saltexts_apply` shows `changed` every run | the pin string differs from the installed version in a way PEP 440 does not equate; check `applied.json` in `<cachedir>/saltext-state/`. |
| Windows: `Access is denied` in pip | a loaded `.pyd`; stop the minion, install, start it (not automated; not tested). |

## Windows (reasoned, not executed)

The state sets `shell: powershell` and builds `& 'C:\...\python.exe' 'C:\...\saltexts_apply.py' '...pins.json' '...saltext-wheels' '...applied.json'; exit $LASTEXITCODE`.
Salt hands this to `powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -Command <string>` through `subprocess` with an argv list.
Reasoning about the layers: (1) the string contains **no double quote**, so `list2cmdline` adds no `\"` escaping; (2) single quotes are literal in PowerShell
(no `$` or backtick expansion), a `'` inside a path is doubled; (3) `exit $LASTEXITCODE` keeps the helper's exit code (otherwise PowerShell reports 1 for any non-zero);
(4) the helper writes **nothing to stderr on success** (Windows PowerShell 5.1 can turn native stderr output into an error record); (5) paths are joined with `\`
from `grains['kernel']`, not with `os.path` of the master. The sketch used double quotes inside the string; that works until a path contains `$` or a backtick, and it
needs `\"` escaping that was never checked. **Salt's `cmd.run` state forces `python_shell=True`**, so a `python_shell: False` workaround without a shell is impossible.
What still has to be proven on a real Windows minion: the whole state run, the restart command, and the locked-file behaviour above.

## Security notes

* The wheel cache and `pins.json` are written by the minion's user; `chmod 0644/0755` for the wheels, `0750` for the state directory (POSIX only).
* SHA256SUMS protects against truncated or half-copied files, **not** against a malicious master (it is served by the same master). Integrity of the
  wheelhouse is the build pipeline's and the master's job: write access to `/srv/salt/saltexts/wheels` only for the pipeline; sign `SHA256SUMS.all` in CI.
* Only master-side `eauth`/ACL decides who may run `state.orchestrate`; restrict `runner` access to `orch.saltexts.*` and give the canary a separate group.
* The state needs no secrets. `pip install` runs with `--no-index` and with `PIP_INDEX_URL`/`PIP_EXTRA_INDEX_URL` removed from the environment.
* `saltext-sqlx` itself: see `SECURITY.md` (read-only profiles; the database role is the real control).

## Design notes: what the sketch got wrong

See "Changes to the sketch" at the end of `docs/DEPLOYMENT_CURSOR_PROMPT.md`.
