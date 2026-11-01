"""Install pinned saltext wheels from a local wheelhouse. Prints a Salt 'stateful' result line. SKETCH."""
import json
import os
import subprocess
import sys
from importlib import metadata


def installed(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main(pins_file, wheels):
    with open(pins_file, encoding="utf-8") as fh:
        pins = json.load(fh)
    marker = os.path.join(wheels, "applied.json")
    try:
        with open(marker, encoding="utf-8") as fh:
            applied = json.load(fh)
    except (OSError, ValueError):
        applied = {}
    # a spec is "todo" if its version differs OR it was never applied (extras changed)
    todo = {s: v for s, v in pins.items() if installed(s.split("[", 1)[0]) != v or applied.get(s) != v}
    if not todo:
        print("changed=no comment='saltext packages already at the pinned versions'")
        return 0
    cmd = [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", wheels,
           "--no-input", "--disable-pip-version-check"] + [f"{s}=={v}" for s, v in todo.items()]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        sys.stderr.write(res.stdout[-2000:] + res.stderr[-2000:])
        return 1
    chk = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, check=False)
    if chk.returncode != 0:
        sys.stderr.write("pip check failed:\n" + chk.stdout[-2000:])
        return 1
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({**applied, **pins}, fh)
    print(f"changed=yes comment='installed {len(todo)} package(s)'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
