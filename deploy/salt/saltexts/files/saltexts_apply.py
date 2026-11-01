"""Install pinned saltext wheels from a local wheelhouse folder and print a Salt "stateful" result line.

    python saltexts_apply.py PINS_JSON WHEELS_DIR MARKER_JSON [--target DIR | --target auto] [--check]

PINS_JSON    {"saltext-sqlx[postgres]": "0.1.0", ...}   (the extras are part of the key)
WHEELS_DIR   folder of wheels + SHA256SUMS (a copy of salt://saltexts/wheels/<target>)
MARKER_JSON  where the pins that were applied successfully are recorded (written ONLY after install and `pip check` succeeded)
--target     install into this directory like `salt-pip` does (the onedir "extras-X.Y" directory), with --upgrade.
             `auto` = the onedir extras directory when this Python is a relenv/onedir Python (sys.RELENV), otherwise no target.
--check      change nothing; exit 0 if everything is already at the pins, 1 if work is needed (used as `unless:` in the state)

Exit status: 0 = ok (stdout: {"changed": "yes"|"no", "comment": "..."}), 1 = failed (reason on stderr, marker untouched).
Standard library only; it must run in the Salt onedir Python on Linux and Windows. On success nothing is written to stderr.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata

try:  # pip vendors it; the standalone packaging may be absent in a onedir
    from packaging.version import InvalidVersion
    from packaging.version import Version
except ImportError:  # pragma: no cover
    try:
        from pip._vendor.packaging.version import InvalidVersion
        from pip._vendor.packaging.version import Version
    except ImportError:
        Version = None


class ApplyError(Exception):
    pass


def split_spec(spec):
    """'saltext-sqlx[postgres,mysql]' -> ('saltext-sqlx', '[postgres,mysql]')"""
    name, sep, rest = spec.partition("[")
    return name.strip(), (sep + rest) if sep else ""


def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def same_version(a, b):
    if a is None or b is None:
        return False
    if a == b:
        return True
    if Version is not None:
        try:
            return Version(a) == Version(b)
        except InvalidVersion:
            return False
    return False


def resolve_target(value):
    if not value:
        return None
    if value != "auto":
        return value
    relenv = getattr(sys, "RELENV", None)
    if relenv is None:
        return None
    override = os.environ.get("SALT_EXTRAS_DIR")
    if override:
        return override
    return os.path.join(str(relenv), "extras-{}.{}".format(*sys.version_info[:2]))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def verify_checksums(wheels):
    """Every *.whl must be listed in SHA256SUMS and match. Guards against truncated/partial/replaced copies (not authenticity)."""
    sums_file = os.path.join(wheels, "SHA256SUMS")
    if not os.path.isfile(sums_file):
        raise ApplyError(f"{sums_file} is missing: refusing to install unverified wheels")
    expected = {}
    with open(sums_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            digest, _, name = line.partition(" ")
            expected[name.strip().lstrip("*").lstrip("./")] = digest.lower()
    present = sorted(n for n in os.listdir(wheels) if n.endswith(".whl"))
    for name in present:
        if name not in expected:
            raise ApplyError(f"{name} is not listed in SHA256SUMS")
    for name, digest in expected.items():
        path = os.path.join(wheels, name)
        if not os.path.isfile(path):
            raise ApplyError(f"{name} is listed in SHA256SUMS but missing from {wheels}")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            raise ApplyError(f"SHA-256 mismatch for {name}")


def pending(pins, applied):
    """Specs that need work: version differs, or this exact spec (incl. extras) was never applied successfully."""
    todo = {}
    for spec, want in pins.items():
        name, _ = split_spec(spec)
        if not same_version(installed_version(name), want) or not same_version(
            applied.get(spec), want
        ):
            todo[spec] = want
    return todo


def pip_env(target):
    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env.pop("PIP_INDEX_URL", None)
    env.pop("PIP_EXTRA_INDEX_URL", None)
    if target:  # same trick as salt-pip: pip must see what is already in the extras directory
        env["PYTHONPATH"] = target
    return env


def run_pip(args, target):
    cmd = [sys.executable, "-m", "pip"] + args
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, env=pip_env(target), timeout=1800
    )


def prune_stale_dist_info(target, name, want):
    """`pip install --target DIR --upgrade` replaces the package folder but leaves the OLD <name>-<old>.dist-info next to the new one,
    and importlib.metadata may then report the old version forever (found by test_target_directory_install_like_salt_pip).
    """
    norm = re.sub(r"[-_.]+", "_", name).lower()
    for entry in os.listdir(target):
        m = re.match(r"^(.+)-([^-]+)\.dist-info$", entry)
        if (
            m
            and re.sub(r"[-_.]+", "_", m.group(1)).lower() == norm
            and not same_version(m.group(2), want)
        ):
            shutil.rmtree(os.path.join(target, entry), ignore_errors=True)


def write_marker(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic on POSIX and on Windows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pins_file")
    ap.add_argument("wheels")
    ap.add_argument("marker")
    ap.add_argument("--target", default="")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    pins = load_json(args.pins_file, None)
    if not isinstance(pins, dict) or not pins:
        raise ApplyError(f"{args.pins_file}: no pins (pillar saltexts:packages is empty)")
    target = resolve_target(args.target)
    if target:
        sys.path.insert(0, target)  # so installed_version() sees the extras directory
    applied = load_json(args.marker, {})
    todo = pending(pins, applied if isinstance(applied, dict) else {})

    if args.check:
        return 0 if not todo else 1
    if not todo:
        print(
            json.dumps(
                {"changed": "no", "comment": "saltext packages already at the pinned versions"}
            )
        )
        return 0

    verify_checksums(args.wheels)
    pip_args = [
        "install",
        "--no-index",
        "--find-links",
        args.wheels,
        "--only-binary=:all:",
        "--no-input",
    ]
    if target:
        pip_args += ["--target", target, "--upgrade"]
    pip_args += [f"{spec}=={want}" for spec, want in sorted(todo.items())]
    res = run_pip(pip_args, target)
    if res.returncode != 0:
        raise ApplyError("pip install failed:\n" + (res.stdout[-2000:] + res.stderr[-2000:]))
    chk = run_pip(["check"], target)
    if chk.returncode != 0:
        raise ApplyError(
            "pip check failed after the install (broken dependency set; marker NOT written):\n"
            + chk.stdout[-2000:]
            + chk.stderr[-1000:]
        )
    if target:
        for spec, want in todo.items():
            prune_stale_dist_info(target, split_spec(spec)[0], want)
    write_marker(args.marker, pins)
    print(
        json.dumps(
            {
                "changed": "yes",
                "comment": f"installed {len(todo)} package(s)",
                "installed": dict(sorted(todo.items())),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApplyError as exc:
        sys.stderr.write(f"saltexts_apply: {exc}\n")
        sys.exit(1)
