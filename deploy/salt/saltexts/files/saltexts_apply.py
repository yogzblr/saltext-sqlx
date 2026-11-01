"""Install pinned saltext wheels from a local wheelhouse folder and print a Salt "stateful" result line.

    python saltexts_apply.py PINS_JSON WHEELS_DIR MARKER_JSON [--target DIR | --target auto] [--check]

PINS_JSON    {"saltext-sqlx[postgres]": "0.1.0", ...}   (the extras are part of the key)
WHEELS_DIR   folder of wheels + SHA256SUMS (a copy of salt://saltexts/wheels/<target>)
MARKER_JSON  where the pins that were applied successfully are recorded (written ONLY after install and `pip check` succeeded)
--target     install into this directory like `salt-pip` does (the onedir "extras-X.Y" directory), with --upgrade.
             `auto` = the onedir extras directory when this Python is a relenv/onedir Python (sys.RELENV), otherwise no target.
--check      change nothing; exit 0 if everything is already at the pins, 1 if work is needed (used as `unless:` in the state)

Packages the Salt onedir already ships (WHEELS_DIR/SALT_OWNED.txt, written by build_wheelhouse.sh) are NEVER installed, upgraded or
downgraded by this helper. `pip install --target` makes pip ignore everything already installed (it would then try to install every
dependency again, cryptography included, into the extras directory where it shadows the copy Salt runs on), so pip is not asked to
resolve anything: the helper walks the Requires-Dist metadata of the wheels itself, installs the wheels of the wheelhouse that are
needed and not yet installed with `pip install --no-deps`, refuses with a clear message if a wheel needs a different version of a
Salt-owned package, and then runs `pip check`.

Exit status: 0 = ok (stdout: {"changed": "yes"|"no", "comment": "..."}), 1 = failed (reason on stderr, marker untouched).
Standard library only; it must run in the Salt onedir Python on Linux and Windows. On success nothing is written to stderr.
"""

import argparse
import email.parser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata

try:  # Salt ships packaging; pip vendors it; the standalone packaging may be absent
    from packaging.requirements import InvalidRequirement
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion
    from packaging.version import Version
except ImportError:  # pragma: no cover
    try:
        from pip._vendor.packaging.requirements import InvalidRequirement
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.version import InvalidVersion
        from pip._vendor.packaging.version import Version
    except ImportError:
        Version = None
        Requirement = None


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
    """Every *.whl (and SALT_OWNED.txt) must be listed in SHA256SUMS and match. Guards against truncated/partial/replaced copies (not authenticity)."""
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
    present = sorted(n for n in os.listdir(wheels) if n.endswith(".whl") or n == OWNED_FILE)
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


OWNED_FILE = "SALT_OWNED.txt"


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def load_owned(wheels):
    """{canonical name: version} of the packages Salt ships (empty if the wheelhouse has no SALT_OWNED.txt)."""
    owned = {}
    try:
        with open(os.path.join(wheels, OWNED_FILE), encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                name, sep, version = line.partition("==")
                if sep:
                    owned[canon(name)] = version.strip()
    except OSError:
        pass
    return owned


def wheel_index(wheels):
    """{canonical name: [(version, path)]} of the wheels in the folder (names and versions come from the file names)."""
    index = {}
    for fname in sorted(os.listdir(wheels)):
        if fname.endswith(".whl"):
            parts = fname[: -len(".whl")].split("-")
            if len(parts) >= 5:
                index.setdefault(canon(parts[0]), []).append(
                    (parts[1], os.path.join(wheels, fname))
                )
    return index


def wheel_requires(path):
    """Requires-Dist lines of a wheel."""
    with zipfile.ZipFile(path) as zf:
        meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            return []
        msg = email.parser.Parser().parsestr(zf.read(meta).decode("utf-8", "replace"))
    return msg.get_all("Requires-Dist") or []


def split_extras(spec):
    name, bracket = split_spec(spec)
    extras = [e.strip() for e in bracket.strip("[]").split(",") if e.strip()]
    return name, extras


def plan_install(todo, wheels, owned):
    """
    Return the wheel paths to install (``pip install --no-deps``) so that every pin in ``todo`` works.

    Walks Requires-Dist (markers evaluated for THIS interpreter). A requirement is met, in this order, by: a Salt-owned package that is
    installed and satisfies it (never touched; if it does not satisfy: ApplyError); a wheel of the wheelhouse (installed unless that
    exact version is already there); an installed package that satisfies it. Anything else is an ApplyError naming the requirement.
    """
    if Requirement is None:
        raise ApplyError(
            "the 'packaging' module is not available (neither packaging nor pip's vendored copy)"
        )
    index = wheel_index(wheels)
    chosen = {}  # canonical name -> (version, path)
    queue = []
    for spec, want in sorted(todo.items()):
        name, extras = split_extras(spec)
        key = canon(name)
        if key in owned:
            raise ApplyError(
                f"pip install failed: {name} is shipped by the Salt onedir ({key}=={owned[key]}); it cannot be pinned here"
            )
        match = [(v, p) for v, p in index.get(key, []) if same_version(v, want)]
        if not match:
            raise ApplyError(f"pip install failed: no wheel for {name}=={want} in {wheels}")
        chosen[key] = match[0]
        queue.append((key, match[0][1], extras))
    seen = set()
    while queue:
        key, path, extras = queue.pop()
        marker_key = (key, tuple(sorted(extras)))
        if marker_key in seen:
            continue
        seen.add(marker_key)
        for line in wheel_requires(path):
            try:
                req = Requirement(line)
            except InvalidRequirement:
                continue
            if req.marker is not None and not any(
                req.marker.evaluate({"extra": e}) for e in ["", *extras]
            ):
                continue
            dep = canon(req.name)
            have = installed_version(req.name)
            if dep in owned:
                if have is None:
                    raise ApplyError(
                        f"{os.path.basename(path)} requires {req}, which Salt provides ({dep}=={owned[dep]}) but which is not installed here"
                    )
                if req.specifier and not req.specifier.contains(have, prereleases=True):
                    raise ApplyError(
                        f"{os.path.basename(path)} requires {req}, but this Salt ships {dep}=={have} and the helper never changes a "
                        "package Salt ships; use a wheelhouse built for this Salt release (refresh the Salt locks and rebuild)"
                    )
                continue
            if dep in chosen and (
                not req.specifier or req.specifier.contains(chosen[dep][0], prereleases=True)
            ):
                queue.append((dep, chosen[dep][1], list(req.extras)))
                continue
            fits = [
                (v, p)
                for v, p in index.get(dep, [])
                if not req.specifier or req.specifier.contains(v, prereleases=True)
            ]
            if fits:
                chosen[dep] = max(fits, key=lambda vp: Version(vp[0]))
                queue.append((dep, chosen[dep][1], list(req.extras)))
            elif have is not None and (
                not req.specifier or req.specifier.contains(have, prereleases=True)
            ):
                continue
            else:
                raise ApplyError(
                    f"pip install failed: {os.path.basename(path)} requires {req}, which is neither in the wheelhouse nor installed "
                    f"({'installed: ' + have if have else 'not installed'})"
                )
    return {
        key: (v, p) for key, (v, p) in chosen.items() if not same_version(installed_version(key), v)
    }


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
    owned = load_owned(args.wheels)
    planned = plan_install(todo, args.wheels, owned)
    if planned:
        pip_args = ["install", "--no-index", "--no-deps", "--only-binary=:all:", "--no-input"]
        if target:
            pip_args += ["--target", target, "--upgrade"]
        pip_args += [p for _, p in sorted(planned.values(), key=lambda vp: vp[1])]
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
        for key, (version, _) in planned.items():
            prune_stale_dist_info(target, key, version)
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
