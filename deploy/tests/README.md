# deploy/tests

Tests for everything under `deploy/`: the wheelhouse tool, the minion helper, the minion state, the orchestration and the
stale-code experiment. They use real virtualenvs, real wheels, real `pip`, `salt-call --local` and a real Salt master with real
minions (pytest-salt-factories). Nothing is mocked except the one command that cannot run in a test (`systemctl restart`), which
is replaced by a command that makes a watcher thread stop and start the minion process for real.

## Run

```bash
# 1. the interpreter that runs pytest must be the Python the wheelhouse is built for (3.14 = Salt 3008) and have Salt in it
uv venv --python 3.14 /tmp/saltenv && uv pip install -p /tmp/saltenv/bin/python "salt==3008.5" pytest pytest-salt-factories pyyaml pip

# 2. build the wheelhouse (needs PyPI; ~1 minute) from this checkout and the Kafka stand-in
PY=/tmp/saltenv/bin/python
OWN=/tmp/own OUT=/tmp/out
SQLX_DIR=$PWD SQLX_VERSION=0.1.0 KAFKA_REPO=https://github.com/salt-extensions/saltext-kafka KAFKA_VERSION=0.0.1 \
  PATH=$(dirname $PY):$PATH OWN=$OWN bash deploy/wheelhouse/build_own_wheels.sh
PATH=$(dirname $PY):$PATH OWN=$OWN OUT=$OUT PYVER=3.14 bash deploy/wheelhouse/build_wheelhouse.sh

# 3. tests
SALTEXTS_WHEELHOUSE=$OUT $PY -m pytest deploy/tests -v
```

| File | Needs | Time |
|---|---|---|
| `test_wheelhouse_tool.py` | nothing | seconds |
| `test_apply.py` (helper, real venvs and pip) | nothing; the last test needs `SALTEXTS_WHEELHOUSE` | ~1.5 min |
| `test_state.py` (salt-call --local, renders the Windows/ARM branches) | Salt in the pytest interpreter; `SALTEXTS_WHEELHOUSE` for the run tests | ~2 min |
| `test_orchestration.py` (master + 3 minions, scenarios a-e) | Salt + pytest-salt-factories; `SALTEXTS_WHEELHOUSE` | ~8 min |
| `test_stale_code.py` (the "is a restart needed" experiment) | same | ~35 s |

Tests that need a wheelhouse **skip** (with the reason) when `SALTEXTS_WHEELHOUSE` is unset, or when its Python or target does not
match the interpreter running pytest. `pytest -rs` shows the skips: a green run with skips proves less than a run without.

Linux only: the Windows and ARM branches are rendered and reviewed, never executed (`deploy/README.md`, "What is verified").
