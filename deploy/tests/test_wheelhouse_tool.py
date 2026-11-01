"""wheelhouse_tool.py: licence gate, stub wheel, name listing."""

import subprocess
import sys

import pytest
from conftest import TOOL
from conftest import make_wheel
from conftest import write_sums


def tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def licensed(tmp_path, name, licence, classifier=None, sub="t1"):
    d = tmp_path / "root" / sub
    d.mkdir(parents=True, exist_ok=True)
    w = make_wheel(d, name, "1.0")
    # patch the METADATA in place
    import shutil
    import zipfile

    tmp = w.with_suffix(".tmp")
    with zipfile.ZipFile(w) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("METADATA"):
                extra = f"License: {licence}\n" if licence else ""
                if classifier:
                    extra += f"Classifier: {classifier}\n"
                data = data.replace(b"Version: 1.0\n", b"Version: 1.0\n" + extra.encode())
            zout.writestr(item, data)
    shutil.move(tmp, w)
    return tmp_path / "root"


@pytest.mark.parametrize(
    "lic,cls,fails",
    [
        ("MIT", None, False),
        ("Apache-2.0", None, False),
        ("LGPL-3.0-only", None, False),
        (
            "GNU LESSER GENERAL PUBLIC LICENSE",
            "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
            False,
        ),
        ("MPL-2.0", None, False),
        ("GPL-3.0-only", None, True),
        ("GPL-2.0-or-later", None, True),
        ("AGPL-3.0-only", None, True),
        (None, "License :: OSI Approved :: GNU General Public License v3 (GPLv3)", True),
        ("GNU General Public License v2", None, True),
    ],
)
def test_licence_gate(tmp_path, lic, cls, fails):
    root = licensed(tmp_path, "pkg", lic, cls)
    r = tool("licences", "--root", str(root))
    assert (r.returncode == 1) is fails, r.stdout + r.stderr


def test_allow_list_can_accept_a_reviewed_gpl_package(tmp_path):
    root = licensed(tmp_path, "pkg-x", "GPL-3.0-only")
    allow = tmp_path / "allow.txt"
    allow.write_text("# reviewed\npkg_x\n")
    assert tool("licences", "--root", str(root), "--allow", str(allow)).returncode == 0


def test_stub_wheel_installs_and_provides_salt(tmp_path):
    r = tool("stub", str(tmp_path / "stub"))
    assert r.returncode == 0
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--no-index",
            "--find-links",
            str(tmp_path / "stub"),
            "salt>=3006",
        ],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr


def test_names_excludes_own(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "own"
    make_wheel(a, "dep-one", "2.0")
    make_wheel(a, "saltext-demo", "1.0")
    make_wheel(b, "saltext-demo", "1.0")
    out = tool("names", str(a), "--exclude-own", str(b)).stdout.split()
    assert out == ["dep-one==2.0"]
