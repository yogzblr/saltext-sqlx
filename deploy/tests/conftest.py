"""Shared fixtures for the deploy/ tests. See deploy/tests/README.md."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DEPLOY = TESTS.parent
APPLY = DEPLOY / "salt" / "saltexts" / "files" / "saltexts_apply.py"
TOOL = DEPLOY / "wheelhouse" / "wheelhouse_tool.py"
STATE_ROOT = DEPLOY / "salt"


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_wheelhouse: needs a built wheelhouse (env SALTEXTS_WHEELHOUSE)")
    config.addinivalue_line("markers", "needs_salt: needs `salt` importable in the interpreter running pytest")


def _wheel_bytes(name, version, requires=(), extras=None, body=""):
    """A minimal but valid pure-Python wheel."""
    norm = name.replace("-", "_").replace(".", "_")
    dist = f"{norm}-{version}.dist-info"
    meta = [f"Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    for r in requires:
        meta.append(f"Requires-Dist: {r}")
    for extra, reqs in (extras or {}).items():
        meta.append(f"Provides-Extra: {extra}")
        for r in reqs:
            meta.append(f"Requires-Dist: {r}; extra == '{extra}'")
    files = {
        f"{norm}/__init__.py": f"__version__ = {version!r}\n{body}\n".encode(),
        f"{dist}/METADATA": ("\n".join(meta) + "\n").encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rec = []
    for n, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rec.append(f"{n},sha256={digest},{len(data)}")
    rec.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = ("\n".join(rec) + "\n").encode()
    return f"{norm}-{version}-py3-none-any.whl", files


def make_wheel(directory, name, version, requires=(), extras=None, body=""):
    filename, files = _wheel_bytes(name, version, requires, extras, body)
    path = Path(directory) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)
    return path


def write_sums(directory, crlf=False, star=False):
    directory = Path(directory)
    lines = []
    for w in sorted(directory.glob("*.whl")):
        lines.append(f"{hashlib.sha256(w.read_bytes()).hexdigest()} {'*' if star else ' '}{w.name}")
    (directory / "SHA256SUMS").write_bytes((("\r\n" if crlf else "\n").join(lines) + "\n").encode())


@pytest.fixture
def make_wheelhouse(tmp_path):
    """make_wheelhouse({"pkg": ["1.0", "2.0"]}) -> directory with those wheels and a SHA256SUMS."""

    def _make(spec, sub="wheels", **kw):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        for name, versions in spec.items():
            for v in versions:
                make_wheel(d, name, v, **kw.get(name, {}))
        write_sums(d)
        return d

    return _make


class Env:
    """A real virtualenv with a stand-in `salt` installed (the Salt the minions already have)."""

    def __init__(self, root: Path):
        self.root = root
        venv.EnvBuilder(with_pip=True, clear=True).create(root)
        self.python = root / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        stub = subprocess.run([sys.executable, str(TOOL), "stub", str(root / "stubdir")], check=True, capture_output=True, text=True)
        self.pip("install", "--no-index", "--find-links", str(root / "stubdir"), "salt")

    def pip(self, *args, check=True):
        return subprocess.run([str(self.python), "-m", "pip", "--disable-pip-version-check", *args],
                              capture_output=True, text=True, check=check)

    def version(self, name):
        r = subprocess.run([str(self.python), "-c", f"import importlib.metadata as m;print(m.version({name!r}))"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def apply(self, pins, wheels, marker, *extra, pins_file=None, env=None):
        pins_file = pins_file or (self.root / "pins.json")
        Path(pins_file).write_text(json.dumps(pins), encoding="utf-8")
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([str(self.python), str(APPLY), str(pins_file), str(wheels), str(marker), *extra],
                              capture_output=True, text=True, env=e)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path / "venv")


def result_line(proc):
    """The helper's stdout is one JSON object (what Salt's `stateful: True` parses)."""
    return json.loads(proc.stdout.strip().splitlines()[-1])


def wheelhouse_root():
    p = os.environ.get("SALTEXTS_WHEELHOUSE")
    return Path(p) if p else None


@pytest.fixture(scope="session")
def real_wheelhouse():
    root = wheelhouse_root()
    if root is None or not (root / "VERSIONS.json").exists():
        pytest.skip("set SALTEXTS_WHEELHOUSE to the OUT directory of build_wheelhouse.sh (needs VERSIONS.json)")
    return root


@pytest.fixture(scope="session")
def salt_factories_config():
    return {"code_dir": None, "inject_sitecustomize": False, "start_timeout": 180}
