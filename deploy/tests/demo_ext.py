"""A tiny REAL Salt extension, packaged as a wheel, used to observe upgrades.

saltext.demo VERSION v:
    saltext/demo/__init__.py            __version__ = v
    saltext/demo/lib/helper.py          VERSION = v      imported by the module at LOAD time (module top level)
    saltext/demo/lib/late.py            VERSION = v      imported by the module at CALL time (first call)
    saltext/demo/modules/demo_mod.py    execution module `demo`;  demo.version -> what each layer reports
Entry point group `salt.loader` makes Salt scan the package for modules/ (exactly like saltext-sqlx).
"""
import base64
import hashlib
import zipfile
from pathlib import Path

MODULE = '''"""demo execution module"""
import os

import saltext.demo.lib.helper as helper          # imported when the loader loads this file
from saltext import demo as package

__virtualname__ = "demo"
MODULE_FILE_VERSION = "{v}"                       # the module file itself


def __virtual__():
    return __virtualname__


def version():
    """What every layer of the extension reports (v1/v2 mixed = stale code)."""
    from saltext.demo.lib import late                 # imported at first CALL

    return {{"module_file": MODULE_FILE_VERSION, "package_init": package.__version__, "helper_at_load": helper.VERSION,
            "late_at_call": late.VERSION, "pid": os.getpid()}}
'''


def make_demo_wheel(directory, version):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"saltext_demo-{version}.dist-info"
    files = {
        "saltext/demo/__init__.py": f'__version__ = "{version}"\n',
        "saltext/demo/lib/__init__.py": "",
        "saltext/demo/lib/helper.py": f'VERSION = "{version}"\n',
        "saltext/demo/lib/late.py": f'VERSION = "{version}"\n',
        "saltext/demo/modules/__init__.py": "",
        "saltext/demo/modules/demo_mod.py": MODULE.format(v=version),
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: saltext.demo\nVersion: {version}\nRequires-Dist: salt>=3006\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": "[salt.loader]\nsaltext.demo = saltext.demo\n",
    }
    rec = []
    blobs = {}
    for n, text in files.items():
        data = text.encode()
        blobs[n] = data
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rec.append(f"{n},sha256={digest},{len(data)}")
    rec.append(f"{dist}/RECORD,,")
    blobs[f"{dist}/RECORD"] = ("\n".join(rec) + "\n").encode()
    path = directory / f"saltext_demo-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in blobs.items():
            zf.writestr(n, data)
    return path
