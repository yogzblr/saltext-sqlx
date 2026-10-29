# Claude Code prompts — build and test saltext-sqlx in github.com/yogzblr/saltext-sqlx

The repo currently has one commit and a one-line `README.md`. These prompts take it from there. Use one prompt per Claude
Code session (or per `/clear`), in order. Each ends at a gate, so you review before the next one.

## 0. One-time import (run in your terminal, not in Claude)

```bash
git clone https://github.com/yogzblr/saltext-sqlx && cd saltext-sqlx
unzip -o /path/to/saltext-sqlx-pack.zip -d /tmp/sqlx-pack
cp -r /tmp/sqlx-pack/saltext-sqlx-pack/. .          # CLAUDE.md, SPEC.md, ..., .cursor/, scripts/
git checkout -b chore/import-build-pack
git add -A && git commit -m "docs: import saltext-sqlx build pack" && git push -u origin chore/import-build-pack
# open a PR, merge it to main, then:
git checkout main && git pull
claude
```

Needs on the machine: Python 3.10+, `git`, `pip`, and Docker (for T1 and the functional tests). Without Docker, Claude
must stop at T1 rather than guess (rule R18).

---

## 1. Session 1 — scaffold (T0)

```text
You are working in the repository github.com/yogzblr/saltext-sqlx (a Salt extension, package `saltext.sqlx`).

Read CLAUDE.md, then the documents it lists, in the order it gives. They are the contract. Do not start coding from memory
of how Salt extensions "usually" look: follow SPEC.md and ARCHITECTURE.md.

Do task T0 from TASKS.md and nothing else:
1. Create branch feat/t0-scaffold from main.
2. Scaffold with the Salt extension copier template (copier copy https://github.com/salt-extensions/salt-extension-copier .).
   Project name `sqlx`, execution module name `sqlx_mod` with virtualname `sqlx`. If the template URL or its questions differ from
   what TASKS.md says, tell me what changed and continue with the closest equivalent.
   The repo root already contains my docs (CLAUDE.md, SPEC.md, ... .cursor/, scripts/). Keep them. If copier wants to overwrite
   README.md, keep my README and merge the template's install/usage sections into it.
3. Add the dependency extras from SPEC.md section 7: base `SQLAlchemy>=2.0,<2.1`; extras postgres=`psycopg[binary]`,
   mysql=`PyMySQL`, oracle=`oracledb`, mssql=`pymssql`. Do not add any other runtime dependency.
4. Create the empty package layout from ARCHITECTURE.md section 1 (modules/sqlx_mod.py stub with __virtual__, lib/ with empty modules).
5. Add `docs/WIRE_SHAPES.md` as a stub (title + "filled in by T1").
6. Make sure the CI workflow the template generated runs lint and unit tests. Do not add database services yet.

Gate (run these and show me the real output, do not paraphrase it):
  python -m venv .venv && . .venv/bin/activate && pip install -e '.[tests,dev]'
  nox -s lint
  nox -s unit            # may have no tests yet; it must run without error
  salt-call --local sys.list_functions sqlx     # lists the stub

Commit in small logical commits, push the branch, and open a pull request titled "T0: scaffold" whose description contains
the gate output. Do not merge it. Do not push to main. Then stop and summarise: what you created, what differed from
TASKS.md, and anything you had to guess.
```

## 2. Session 2 — pin the facts on real databases (T1)

```text
Read CLAUDE.md and the documents it lists. We are on task T1 (TASKS.md). T0 is merged.

Create branch feat/t1-wire-shapes. Goal: replace every assumption about MySQL, MariaDB, Oracle and SQL Server in SPEC.md
sections 7 and 8 with behaviour you observed on real servers. PostgreSQL is already verified; re-verify it too.

1. Fix and start the test databases in tests-support/ using Docker (TEST_PLAN.md section 2). The image tags and flags there have
   never been run. Fix them. Create the PostgreSQL database as UTF8. Each database needs a read-only account `ro` and a
   `rw` account on a scratch schema, and a seeded `orders` table with text, numeric, timestamp, UUID/GUID, binary, JSON and NULL
   columns. Record every change in tests-support/NOTES.md.
2. Port scripts/sa_probe.py to each dialect (put the ports in scripts/probes/). For each dialect record, for these cases:
   syntax error, missing table, missing column, division by zero, unique violation, permission denied, statement timeout,
   wrong password, unreachable port, write attempt under the in-session read-only guard, and each bypass attempt from
   SECURITY.md section 2: the SQLAlchemy exception class, the driver exception class, the SQLSTATE or vendor code, the
   available message fields, and whether the exception text contains the password.
3. Test the timeout mechanism and the read-only mechanism in SPEC.md section 7 for each dialect, and what happens with
   SQLAlchemy 2.1.x (decision D4).
4. Write all of it into docs/WIRE_SHAPES.md as one table row per dialect x case.
5. Correct SPEC.md sections 7 and 8 and SECURITY.md section 2 wherever reality differs. List every correction in the PR.

If a database cannot run in this environment (for example SQL Server or Oracle on an unsupported CPU, or Docker is not
available), do not guess. Mark that dialect "NOT RUN" in WIRE_SHAPES.md with the reason, and tell me.

Gate: docs/WIRE_SHAPES.md has a row per dialect x case with real observed values or an explicit NOT RUN; SPEC.md sections 7-8
contain no unverified claim that is not labelled as such; tests-support/ starts all runnable databases with one command.
Open a PR "T1: wire shapes", do not merge, and stop with a summary table of what ran and what did not.
```

## 3. Sessions 3–10 — one task each (T2–T9), same template

Paste this, changing only the task id and branch name:

```text
Read CLAUDE.md and the documents it lists. We are on task <TASK ID> in TASKS.md. All earlier tasks are merged.

1. Branch feat/<task-id>-<short-name> from main.
2. Test first: write the tests TEST_PLAN.md lists for this task, run them, and show me they fail for the right reason.
3. Implement only what this task covers. Follow BUILD_PROMPT.md rules R1-R20. If SPEC.md is wrong or ambiguous, change SPEC.md in
   the same commit and say so; do not work around it.
4. Run the task's gate from TASKS.md exactly, including the functional tests against every database that is running, and
   show me the real output. Report any dialect you could not run.
5. Commit in small logical commits, push the branch, open a PR "<TASK ID>: <name>" with the gate output in the description.
   Do not merge. Do not push to main.
6. Stop and summarise the diff, the gate result, and anything you decided that was not in the documents.

Do not start the next task. Do not add a dependency without asking.
```

| Task | Branch name | Extra instruction to add |
|---|---|---|
| T2 | `feat/t2-options-profiles` | none |
| T3 | `feat/t3-secrets` | Include the log-capture tests; prove no secret appears in any log record or exception on any error path |
| T4 | `feat/t4-dialects-engine` | Use `URL.create`; functional `ping` must pass on every running dialect |
| T5 | `feat/t5-errors` | Drive the mapping table from `docs/WIRE_SHAPES.md`, not from memory |
| T6 | `feat/t6-runner` | Include `test_text_binds.py`; query must roll back (verify from a second connection) |
| T7 | `feat/t7-module` | Show `salt-call --local sqlx.query ...` returning `ERROR: [E_SQL] ...` and retcode 1 for a bad statement |
| T8 | `feat/t8-checks` | Retry loop must use an injectable clock in tests |
| T9 | `feat/t9-execute` | `commit(` must appear only in `run_execute`; prove `dry_run` and the disabled profile leave data unchanged |

After T6 add one extra request: "Extend the GitHub Actions workflow to run the PostgreSQL functional tests with a service
container, and run the other dialects in a separate scheduled (nightly) workflow."

## 4. Session 11 — Salt integration (T10)

```text
Read CLAUDE.md and the documents it lists. We are on task T10. Branch feat/t10-salt-integration.

Write tests/integration/ using pytest-salt-factories (TEST_PLAN.md section 5): start a real master and minion with the
extension installed and a PostgreSQL profile. Cover, through LocalClient with JSON-typed kwargs: typed kwargs arrive typed;
__pub_* keys do not trigger E_OPTION_UNKNOWN while a typo does; a bad statement returns "ERROR: [E_SQL] ..." with retcode 1
(use full_return); an sdb:// password resolves; an external_auth regex limit on `profile` (SECURITY.md section 4) refuses a
non-matching profile.

If salt-api can be started in this environment, also run the curl command from SPEC.md section 11 and put the exact command
and its real output in README.md. If it cannot, say so and leave a clearly marked TODO instead of inventing output.
PR "T10: salt integration", do not merge, stop with a summary.
```

## 5. Session 12 — security pass (T11) and a fresh-eyes review

```text
Read CLAUDE.md and SECURITY.md. We are on task T11. Branch feat/t11-security.

Work through the SECURITY.md section 6 checklist one box at a time. For each box, show the command or test that proves it
and its output; tick it only when proven, otherwise write the exception and the reason beside it. Re-run the read-only
bypass attempts on every running dialect and record the results in docs/WIRE_SHAPES.md. Add NOTICE with the licence of every
runtime dependency (run pip-licenses). Fail the build if a GPL licence appears.

Then act as a hostile reviewer who has not seen the earlier work: try to make sqlx.query write data, leak a password, reach a
host that is not in a profile, or return an unbounded result. Report every attempt and its result, even the ones that failed.
PR "T11: security pass", do not merge, stop with the checklist and the attempt log.
```

## 6. If a session goes wrong

- Claude invents a database behaviour: reply "R18 — show me the observed output or mark it NOT RUN."
- Claude widens scope: reply "Revert that; this task covers only <items from TASKS.md>."
- Claude wants to push to main or merge: refuse; the PR review is your control.
- The gate fails: do not move on. Paste the failure back and ask for the root cause before a fix.
