# Cursor prompt: deploy and operate the Salt extensions on the real fleet

Hand this file to the engineering team. Paste the prompt block into Cursor (Agent mode) at the root of this repository.
Everything under `deploy/` was **built and tested in a Linux x86_64 lab sandbox** (Salt 3008.5, Python 3.14): see `deploy/README.md` and
"What is and is not verified" below. Windows, ARM64, a Salt onedir, a real `systemd` restart and the Docker build were NOT possible there.
The team's job is to prove those on the real environment, with evidence at every gate.

```text
You are working in the repository github.com/yogzblr/saltext-sqlx. Read, in this order: CLAUDE.md, deploy/README.md,
deploy/tests/README.md, deploy/ (every file), docs/USER_GUIDE.md, SECURITY.md, SPEC.md sections 7, 8 and 10. Rules:
- One gate at a time. Stop at each gate, show real command output (not a paraphrase), and wait for my approval.
- Never run anything against production minions until gate 4. Never push to main; use branches and pull requests.
- If a fact about our environment is unknown, ASK (list of questions below). Do not guess versions, paths or architectures.

GOAL: every Salt minion in our fleet (Linux and Windows, x86_64 and ARM64) gets, and keeps, a pinned set of Salt extensions
(saltext-sqlx, our saltext-kafka, saltext-elasticsearch, saltext-mongodb, saltext-redis) installed from WHEEL FILES SERVED BY THE
SALT MASTER (no internet on the minions), through an idempotent state, rolled out by a master-side orchestration that restarts
the minions safely and verifies they came back with the extensions loaded. The hourly run must be a no-op when nothing changed.

GATE 0, DISCOVERY (answer these from the real environment; ask me for anything you cannot read):
 1. Salt version on master and on each minion class; run `salt --versions-report` / `salt-call --versions-report`. Which Python
    does each minion's Salt onedir ship? Wheels are built for ONE Python version; report every distinct one. Known from Salt's source
    (cicd/shared-gh-workflows-context.yml per tag): 3008.x = 3.14; 3007.x up to 3007.14 = 3.10, later 3.11; 3006.x up to 3006.26 = 3.10,
    3006.27+ = 3.11. The pinned set does NOT build for 3.10 (pymongo 4.19.0 has no cp310 wheel). Is `salt-pip`/`extras-3.N` in use on the
    minions (decides `pip_target: auto`, see gate 2)? What is the minimum glibc on Linux minions (pymssql needs manylinux_2_28)?
 2. Inventory: count of minions per (kernel, cpuarch) from `salt '*' grains.item kernel cpuarch osarch pythonversion`. Are there
    Alpine/musl minions, 32-bit minions, macOS minions, proxy minions or salt-ssh targets? Those are out of scope unless I say so.
 3. How is the master deployed (Docker image? Kubernetes? HA / multiple masters / syndic?). Where do file_roots and pillar_roots
    come from (baked into the image, a volume, gitfs)? Is the master image built by CI, and which registry does it use?
 4. Network: can minions reach the master on 4505/4506 only? Is there an internal PyPI mirror/artifactory? Does the build host
    have access to pypi.org (needed once to build the wheelhouse), or must the build also be offline?
 5. How do minions run: systemd (Linux), Windows service name for the minion, any minion wrapper that blocks `systemd-run`?
 6. Where does our own Kafka extension live (repo URL, package name, version, client library)? It must NOT be installed together with the upstream
    salt-extensions/saltext-kafka (same `saltext.kafka` namespace).
 7. Which minions get which extensions (grain/pillar groups)? Which database minions use sqlx and which dialects/drivers?
 8. How does the existing health check call `test.ping` (cron on the master, minion scheduler, external monitor)?
 9. Change-management: canary group, batch size, maintenance windows, who approves a rollout, rollback expectations.
10. Licence policy: Apache-2.0/MIT only; LGPL accepted only as unmodified pip packages (psycopg, pymssql). Confirm.

GATE 1, BUILD THE WHEELHOUSE (CI job, reproducible):
 - Start from deploy/wheelhouse/build_own_wheels.sh, build_wheelhouse.sh and deploy/Dockerfile.master. Set PYVER to the Python found in gate 0
   (default 3.14 = Salt 3008). There is no official `saltproject/salt` image: the Dockerfile builds the master from the onedir tarball.
 - Every pin is already real (deploy/wheelhouse/requirements/*.txt) and the transitive set is frozen in constraints.py<PYV>.<target>.txt.
   CONFIRM the `elasticsearch` client major with the server (9.5.1 is a guess) and `redis==3.5.3` with the redis extension.
 - Build our OWN Kafka extension (KAFKA_REPO/KAFKA_REF/KAFKA_VERSION). The upstream salt-extensions/saltext-kafka used by the tests and the CI is a
   test stand-in only.
 - Build for all targets found in the inventory. A package with no wheel for a target must fail the build; for each such case decide
   with me (known: no psycopg-binary wheel for Windows ARM64 -> PostgreSQL profiles unsupported there unless we add pg8000).
 - Produce: wheel folders per target, SHA256SUMS, a licence report (pip-licenses) that proves no GPL, and VERSIONS.json for the pillar.
 - Verify offline installability with the dry-run in the script, and ALSO on a real machine of each target class (Linux x86, Linux ARM,
   Windows x86, Windows ARM if they exist): `python -m pip install --no-index --find-links <folder> ...` in Salt's own Python.
 - Supply chain: build in CI from pinned commits, store the wheelhouse as a build artifact, sign or hash it, record the SBOM.
 GATE 1 OUTPUT: build log, per-target file lists, SHA256SUMS, licence report, install proof per target class.

GATE 2, THE MINION STATE (deploy/salt/saltexts):
 - Make the state work on every target class. Linux x86_64 is proven (deploy/tests). Prove, per class, with evidence: the real pip invocation
   through `grains['pythonexecutable']` in the ONEDIR (plain pip installs into the onedir site-packages, which Salt documents as unsupported and
   replaces on every Salt upgrade; try `pip_target: auto`, which installs into `extras-3.N` like `salt-pip`), the Windows `cmd.run` text (single-quoted
   PowerShell literals, see deploy/README.md), file permissions, the cache directory, locked `.pyd` files on Windows.
 - Prove idempotency on each target class: first `state.apply saltexts` installs; the second run reports ZERO changes
   (`changed=no`); changing one pin in pillar changes exactly that package; `test=True` changes nothing.
 - Prove failure behaviour: a missing wheel, a Python-version mismatch, a dependency conflict (`pip check`) all FAIL the state loudly.
 - Prove the cleanup: `clean: True` removes wheels no longer shipped; the applied marker is correct after a failed run.
 GATE 2 OUTPUT: transcripts per target class.

GATE 3, THE ORCHESTRATION (deploy/salt/orch/saltexts/rollout.sls):
 - Test on a staging master with at least: 2 Linux x86, 1 Linux ARM, 1 Windows (and Windows ARM if present) minions.
 - Prove: (a) nothing changed -> no restart, no wait, verify passes; (b) version bump -> only that batch restarts, the master waits
   for `salt/minion/<id>/start`, the verify state passes; (c) kill a minion during restart -> the orchestration FAILS and does not
   continue to the next batch; (d) the restart commands work on systemd and on Windows services; (e) `retry`, `parallel: True`,
   `onchanges` on salt.function/salt.wait_for_event behave as the sketch assumes. If any assumption is wrong, fix the orchestration
   and say what you changed. Prove no minion is left without a salt-minion process.
 - Stale code: measured on Linux (deploy/README.md, tests/test_stale_code.py): job processes already run the new module files and new submodules,
   the minion's main process keeps the OLD package `__init__.py` (and anything else it imported). Repeat the experiment on Windows (jobs are spawned;
   loaded .pyd files are locked) and on a onedir, and on our real extensions (engines? beacons?). `restart: false` exists but the default stays restart.
 GATE 3 OUTPUT: orchestration run outputs for scenarios a-d and the stale-code experiment.

GATE 4, PRODUCTION ROLLOUT PLAN (do not execute without my written approval):
 - Canary group first (tgt=G@role:canary, small batch), then 10% waves, then the fleet. Show the exact commands, the stop criteria,
   and the rollback (previous pins + previous wheelhouse image tag; show that rollback is an ordinary rollout).
 - Enable the master schedule (deploy/master.d/saltexts-schedule.conf) only after the first full rollout converged; confirm hourly runs are no-ops.
 - Health checks: keep `test.ping` for liveness; add the extension check from the sqlx repo (`sqlx.healthcheck` once it exists) or
   `sys.list_modules` through deploy/salt/saltexts/verify.sls to your existing monitoring; define alerts for "minion alive but extension not loaded".
 - Security review: file permissions of the wheel cache, who may run state.orchestrate (master eauth), what is logged, and that wheels cannot be replaced
   by a minion. Master file_roots must be writable only by the build pipeline.

DELIVERABLES: pull requests with the final files under deploy/, deploy/README.md (operator runbook: build, bump a version, roll out, roll back,
troubleshoot), a CI pipeline definition, the evidence from each gate, and a list of anything still unverified.
```

## What is and is not verified (as of this handover)

Verified for real (Linux x86_64, Salt 3008.5, CPython 3.14.6; `deploy/tests`, 2026-11-01): the wheelhouse build for all four targets (3.14 and 3.11) and
its offline install on Linux x86_64; the helper; the state through `salt-call --local` (idempotency, one pin = one package, `test=True`, failures,
`clean: True`); the orchestration on a real master with three real minions (a no-op run, a pin change with real minion restarts, batches, rollback, a minion
that never returns, retry, parallel, onchanges, empty target, `test=True`); the stale-code experiment.

Rendered and reviewed only: the Windows and ARM64 branches of the state (the `cmd.run` text is asserted by a test).
NOT possible in the sandbox, so unverified: executing anything on Windows or ARM64; a Salt onedir (`salt-pip`, `extras-3.N`, `pip_target: auto`);
the real `systemd-run ... systemctl restart salt-minion` and the PowerShell `Restart-Service`; building `Dockerfile.master` (no registry);
the master `schedule:` entry; our own Kafka extension.

Known gaps (confirmed by the real build): no `psycopg-binary` wheel for Windows ARM64 (cp311 and cp314); `redis-py-cluster` 2.1.3 pins `redis<4`;
`saltext-elasticsearch` does not declare the `elasticsearch` client; no cp310 wheel for `pymongo` 4.19.0; `pymssql` needs glibc >= 2.28;
the upstream Kafka extension cannot read messages (administration only).

## Changes to the sketch (found by running it)

Bugs, each reproduced first and fixed (details in `deploy/README.md` and the commit messages):

1. `file.recurse ... clean: True` on the folder that also held `pins.json`, `applied.json` and the helper deleted them on every run: the state reported
   changes every hour and the marker never survived. Wheels and state now live in separate folders.
2. The `cmd.run` **state forces `python_shell=True`** (also for `unless`); a `python_shell: False` workaround fails with "multiple values for keyword
   argument". Arguments are now single-quoted literals for sh and PowerShell.
3. The "stateful" output `changed=yes comment='... pkg==1.0 ...'` is split on every `=` by Salt and fails to parse. The helper prints one JSON object.
4. `onchanges` on a `salt.state` is true when ANY state of the sls changed, so copying a wheel would have restarted the batch. The sls is split
   into `stage` (files) and `apply` (pip install only); the restart is tied to `apply`.
5. `test.fail_without_changes` lost its message ("Failure!"); replaced by `test.configurable_test_state`.
6. `saltext-sqlx`, the Kafka extension and the others declare `salt>=N`, Salt is sdist-only, so `pip download --only-binary=:all:` cannot resolve any of
   them: the build now satisfies `salt` with a build-time stub wheel that is never shipped.
7. Pins `0.0.0` could not work: an untagged checkout builds a dev version (`0.1.dev132+g...`); own wheels are stamped explicitly.
8. The Dockerfile's purity check `-py3-none-any.whl$` rejects `py2.py3-none-any` (what these projects build); `FROM saltproject/salt:3008` does not exist
   (no official image); the wheelhouse had no lock file, so a rebuild would silently change dependencies; the hourly schedule was enabled by default.
9. `pip install --target DIR --upgrade` leaves the old `.dist-info` next to the new one; the helper prunes it in target mode.

Assumptions of the sketch that were wrong or incomplete: `python -m pip` in the onedir is the supported way (it is not; `salt-pip`/extras is);
`wait_for_event` after a restart is race-free (it is only because it runs `parallel: True` from the start of the restart step; `test=True` skips it);
a new wheelhouse is visible at once (the master's file list is cached for 20 s: observed as a checksum failure that heals on the next run);
the restart is mandatory (partly: see above); `onchanges` works on `salt.function`, `salt.wait_for_event` and `salt.state` (confirmed, with the
caveat in 4); `retry: {until: True}` on `salt.state` works (confirmed); `parallel: True` runs steps concurrently (confirmed: two 6 s `salt.function` steps
started 7 ms apart).

Guesses that remain: `elasticsearch==9.5.1`; `redis==3.5.3` working with `saltext-redis`; `pip_target: auto` on a onedir; PowerShell behaviour on Windows
(reasoned from Salt's source); that `systemd-run --on-active=10` survives the minion's cgroup being stopped (documented systemd behaviour, not run).
