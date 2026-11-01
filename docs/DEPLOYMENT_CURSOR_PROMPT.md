# Cursor prompt: deploy and operate the Salt extensions on the real fleet

Hand this file to the engineering team. Paste the prompt block into Cursor (Agent mode) at the root of this repository.
Everything under `deploy/` is a **sketch that has been tested only in a lab** (see "What is and is not verified").
The team's job is to take it to production in the real environment, with evidence at every gate.

```text
You are working in the repository github.com/yogzblr/saltext-sqlx. Read, in this order: CLAUDE.md, deploy/README.md (if
present), deploy/ (every file), docs/USER_GUIDE.md, SECURITY.md, SPEC.md sections 7, 8 and 10. Rules:
- One gate at a time. Stop at each gate, show real command output (not a paraphrase), and wait for my approval.
- Never run anything against production minions until gate 4. Never push to main; use branches and pull requests.
- If a fact about our environment is unknown, ASK (list of questions below). Do not guess versions, paths or architectures.

GOAL: every Salt minion in our fleet (Linux and Windows, x86_64 and ARM64) gets, and keeps, a pinned set of Salt extensions
(saltext-sqlx, our saltext-kafka, saltext-elasticsearch, saltext-mongodb, saltext-redis) installed from WHEEL FILES SERVED BY THE
SALT MASTER (no internet on the minions), through an idempotent state, rolled out by a master-side orchestration that restarts
the minions safely and verifies they came back with the extensions loaded. The hourly run must be a no-op when nothing changed.

GATE 0, DISCOVERY (answer these from the real environment; ask me for anything you cannot read):
 1. Salt version on master and on each minion class; run `salt --versions-report` / `salt-call --versions-report`. Which Python
    does each minion's Salt onedir ship (3.10? 3.11? 3.12?)? Wheels are built for ONE Python version; report every distinct one.
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
 - Start from deploy/Dockerfile.master and deploy/wheelhouse/build_wheelhouse.sh. Set PYVER to the Python found in gate 0.
 - Pin every version in deploy/wheelhouse/requirements/*.txt (no placeholders left). Match the `elasticsearch` client major to the server.
 - Build for all targets found in the inventory. A package with no wheel for a target must fail the build; for each such case decide
   with me (known: no psycopg-binary wheel for Windows ARM64 -> PostgreSQL profiles unsupported there unless we add pg8000).
 - Produce: wheel folders per target, SHA256SUMS, a licence report (pip-licenses) that proves no GPL, and VERSIONS.json for the pillar.
 - Verify offline installability with the dry-run in the script, and ALSO on a real machine of each target class (Linux x86, Linux ARM,
   Windows x86, Windows ARM if they exist): `python -m pip install --no-index --find-links <folder> ...` in Salt's own Python.
 - Supply chain: build in CI from pinned commits, store the wheelhouse as a build artifact, sign or hash it, record the SBOM.
 GATE 1 OUTPUT: build log, per-target file lists, SHA256SUMS, licence report, install proof per target class.

GATE 2, THE MINION STATE (deploy/salt/saltexts):
 - Make the state work on every target class. Fix the platform-specific parts with evidence: the real pip invocation through
   `grains['pythonexecutable']`, the `cmd.run` quoting on Windows (PowerShell), file permissions, the cache directory.
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
 - Confirm the stale-code problem is real or not: upgrade an extension WITHOUT restarting and show whether the running minion still
   uses the old code (so we know whether the restart is mandatory for upgrades on Linux and Windows).
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

- Verified by reading: Salt scans extension entry points with a 0.5 second cache; PyPI wheel availability per target for the sqlx drivers
  (checked for Python 3.11); the existence and module list of the upstream `salt-extensions/saltext-kafka`.
- NOT verified: every step that touches a real minion restart, Windows (PowerShell quoting, service restart, locked files),
  ARM targets, `retry`/`parallel`/`onchanges` on orchestration states, whether a restart is mandatory after an upgrade,
  the onedir pip path, and the behaviour of our own Kafka extension in this scheme.
- Known gaps: no `psycopg-binary` wheel for Windows ARM64 (Python 3.11); `redis-py-cluster` 2.1.3 pins `redis<4`;
  `saltext-elasticsearch` does not declare the `elasticsearch` client as a runtime dependency; the upstream Kafka extension cannot read
  messages (administration only).
