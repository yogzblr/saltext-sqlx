# saltext-sqlx — TASKS

One task at a time, in order. Each ends at a **gate**; stop there and summarise. Unit tests first for anything in `lib/`.

| # | Task | Gate |
|---|---|---|
| T0 | Scaffold with the Salt extension copier template (`copier copy https://github.com/salt-extensions/salt-extension-copier .`, name `sqlx`, module `sqlx_mod`). Add dependency extras (SPEC section 7). Copy this pack's docs in. | `pip install -e '.[tests,dev]'` works; `nox -s lint` and an empty `nox -s unit` pass; `salt-call --local sys.list_functions sqlx` lists the stub |
| T1 | **Pin the facts.** Start the five test databases (TEST_PLAN section 2, fixing the untested images). Run `scripts/sa_probe.py` (set `SQLX_PROBE_URL`; it is PostgreSQL-flavoured, so port its cases per dialect) and record every real exception class, SQLSTATE/vendor code, message field, timeout mechanism, read-only behaviour and bypass result in `docs/WIRE_SHAPES.md`. Fix SPEC sections 7–8 where reality differs. Also test SQLAlchemy 2.1.x (D4) and driver exception text for password leakage. | `WIRE_SHAPES.md` has a table row per dialect × case; SPEC corrected; no `TBD` left in sections 7–8 |
| T2 | `lib/options.py` + `lib/profiles.py` | `test_options`, `test_profiles` green |
| T3 | `lib/secrets.py` (sdb resolution, masking filter) | `test_secrets` green, including log-capture tests |
| T4 | `lib/dialects.py` + `lib/engine.py` (adapters for all four, `URL.create`, allowlists, session setup) | `test_dialects` green; functional `ping` green on all dialects |
| T5 | `lib/errors.py` (the single error path) | `test_errors` green; functional **error fidelity** green on all dialects |
| T6 | `lib/serialize.py` + `lib/runner.py` (`query` path, limits, rollback) | `test_serialize`, `test_runner`, `test_text_binds` green; functional `query` green on all dialects |
| T7 | `modules/sqlx_mod.py`: `query`, `ping`, `table_list`, `__virtual__`, exception translation | `test_module` green; manual `salt-call sqlx.query` shows `ERROR: [E_SQL] ...` for a bad statement |
| T8 | `lib/checks.py` + `check_query_result`, `check_row_count` | `test_checks` green; functional `check_*` green |
| T9 | `execute` + policy (`allow_execute`, verbs, `dry_run`) | `test_runner` execute cases and functional `execute` green; `test_guard` green |
| T10 | Salt integration (LocalClient path, `__pub_*`, sdb, eauth) | TEST_PLAN section 5 items 1–5 green |
| T11 | Security pass: SECURITY section 6 checklist, read-only proofs per dialect, `NOTICE`, README with the recorded smoke command | every checklist box ticked or an explicit written exception |

Do not start T7 before T5 and T6 pass: the module must be a thin shell over tested libraries.
