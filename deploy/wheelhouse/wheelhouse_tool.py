#!/usr/bin/env python3
"""Helpers for build_wheelhouse.sh. Standard library only, so it runs in any build image.

  stub      OUTDIR                      write a dependency-free stand-in wheel for `salt` (see below)
  names     DIR [--exclude-own OWNDIR]  print `name==version` for every wheel in DIR (sorted, normalised names)
  versions  --out FILE --pyver X.Y --own OWNDIR --root OUT    write VERSIONS.json
  licences  --root OUT [--markdown FILE] [--allow FILE]       licence report from wheel METADATA; exit 1 on GPL
  check-pins --lock LOCK [--out FILE] REQFILE...              refuse a pin that disagrees with what the Salt onedir ships
  constraints LOCK                                            print `name==version` of every package Salt ships (pip -c)
  salt-owned  LOCK --out FILE                                 write SALT_OWNED.txt (shipped next to the wheels; read by the minion helper)
  strip     DIR --lock LOCK [--check]                         delete Salt-owned wheels from DIR (--check: only fail if there are any)

Why a stub for `salt`: every Salt extension declares `salt>=...` as a dependency. Salt is only published as an sdist on PyPI, so
`pip download --only-binary=:all:` cannot resolve it, and the minions already have Salt (that is where the extension runs).
The stub is used ONLY at build time to let the resolver finish; it is never copied into the wheelhouse.

The same trick covers every package the Salt onedir already ships (`stub --lock`): one metadata-only stub wheel per package of
Salt's own lock file, at the SAME version plus the local label `+salt<tag>`. PEP 440 says `==50.0.1` matches `50.0.1+salt3008.5`
and that the local version sorts higher, so the resolver takes the stub and never downloads the real wheel; whatever the stub
leaves out of the wheelhouse is exactly what a minion lacks. See deploy/README.md, "Respect what Salt ships".
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

STUB_VERSION = "9999.0"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_name(path: Path) -> tuple[str, str]:
    parts = path.name[: -len(".whl")].split("-")
    if len(parts) < 5:
        raise ValueError(f"not a wheel file name: {path.name}")
    return norm(parts[0]), parts[1]


def _write_stub(out: Path, name: str, version: str, summary: str) -> Path:
    """One metadata-only, dependency-free, pure-Python wheel."""
    dist_name = re.sub(r"[-_.]+", "_", name)
    dist = f"{dist_name}-{version}.dist-info"
    files = {
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: {summary}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: wheelhouse_tool\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for fname, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{fname},sha256={digest},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    target = out / f"{dist_name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w") as zf:
        for fname, text in files.items():
            zf.writestr(fname, text)
    return target


_LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)")
_LOCK_HEAD = re.compile(r"^#\s*([a-z-]+):\s*(.+?)\s*$")


def parse_lock(path) -> tuple[dict, dict]:
    """Read one vendored Salt lock file: ({header key: value}, {normalised name: version})."""
    head: dict = {}
    pins: dict = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        found = _LOCK_HEAD.match(line)
        if found and found.group(1) in ("salt-tag", "python", "platform", "fetched", "source"):
            head[found.group(1)] = found.group(2)
            continue
        found = _LOCK_LINE.match(line)
        if found:
            pins[norm(found.group(1))] = found.group(2)
    if not pins:
        raise SystemExit(f"{path}: no pinned packages (not a Salt lock file?)")
    return head, pins


def local_label(head: dict) -> str:
    """`v3008.5` -> `salt3008.5` (a valid PEP 440 local version label)."""
    tag = re.sub(r"[^a-z0-9.]+", "", head.get("salt-tag", "").lower().lstrip("v"))
    return f"salt{tag}" if tag else "salt"


def cmd_stub(args) -> int:
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    print(_write_stub(out, "salt", STUB_VERSION, "BUILD-TIME STUB, never shipped"))
    if args.lock:
        head, pins = parse_lock(args.lock)
        skip = {norm(n) for n in args.skip or []}
        label = local_label(head)
        for name, version in sorted(pins.items()):
            if name not in skip:
                _write_stub(
                    out,
                    name,
                    f"{version}+{label}",
                    "BUILD-TIME STUB of a package the Salt onedir ships",
                )
    return 0


def wheels(directory: Path):
    return sorted(directory.glob("*.whl"))


def cmd_names(args) -> int:
    own = set()
    if args.exclude_own:
        own = {parse_wheel_name(p)[0] for p in wheels(Path(args.exclude_own))}
    for p in wheels(Path(args.dir)):
        name, ver = parse_wheel_name(p)
        if name not in own:
            print(f"{name}=={ver}")
    return 0


def cmd_constraints(args) -> int:
    _, pins = parse_lock(args.lock)
    for name, version in sorted(pins.items()):
        print(f"{name}=={version}")
    return 0


def cmd_salt_owned(args) -> int:
    head, pins = parse_lock(args.lock)
    pins = {n: v for n, v in pins.items() if n not in {norm(x) for x in args.skip or []}}
    lines = [
        "# Packages the Salt onedir already ships; the wheelhouse does NOT contain them and saltexts_apply.py never installs them.",
        f"# salt-tag: {head.get('salt-tag', '?')}  python: {head.get('python', '?')}  platform: {head.get('platform', '?')}",
        *[f"{name}=={version}" for name, version in sorted(pins.items())],
    ]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(pins)} packages)")
    return 0


_REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;]*)")


SALT_WINS = "salt-wins"


def cmd_check_pins(args) -> int:
    """
    A requirement line that names a Salt-owned package may only repeat Salt's own version (or carry no version at all).

    A line whose comment contains ``salt-wins`` is a pin for the platforms where Salt does NOT ship the package: where Salt does
    ship it, the line is dropped (``--out`` receives the filtered file) and Salt's version is the one that runs. Any other line that
    disagrees with Salt is an error.
    """
    head, pins = parse_lock(args.lock)
    where = f"{head.get('salt-tag', '?')}, python {head.get('python', '?')}, {head.get('platform', '?')}"
    bad, kept = [], []
    for req in args.reqfiles:
        for number, raw in enumerate(Path(req).read_text(encoding="utf-8").splitlines(), 1):
            line, _, comment = raw.partition("#")
            line = line.strip()
            found = _REQ_LINE.match(line) if line and not line.startswith("-") else None
            if not found or norm(found.group(1)) not in pins:
                kept.append(raw)
                continue
            name, spec = norm(found.group(1)), found.group(3).strip().replace(" ", "")
            if not spec or spec == f"=={pins[name]}":
                kept.append(raw)
            elif SALT_WINS in comment:
                print(
                    f"NOTE: '{line}' dropped: the Salt onedir ({where}) ships {name}=={pins[name]}, which is what will run",
                    file=sys.stderr,
                )
            else:
                bad.append((Path(req).name, number, line, name, pins[name]))
    for fname, number, line, name, version in bad:
        print(
            f"ERROR {fname}:{number}: '{line}' - the Salt onedir ({where}) already ships {name}=={version}. "
            f"Installing another version would shadow or conflict with the copy Salt runs on. "
            f"Remove the pin (or mark it '# {SALT_WINS}' if it is meant only for platforms where Salt does not ship {name}); "
            f"to get a newer {name} upgrade Salt and refresh the locks (deploy/wheelhouse/refresh_salt_locks.sh).",
            file=sys.stderr,
        )
    if args.out and not bad:
        Path(args.out).write_text("\n".join(kept) + "\n", encoding="utf-8")
    return 1 if bad else 0


def cmd_strip(args) -> int:
    head, pins = parse_lock(args.lock)
    label = local_label(head)
    hits = []
    for p in wheels(Path(args.dir)):
        name, ver = parse_wheel_name(p)
        if name in pins or ver.endswith(f"+{label}"):
            hits.append(p)
    if args.check:
        for p in hits:
            print(
                f"ERROR {p.name}: ships with Salt, must not be in the wheelhouse", file=sys.stderr
            )
        return 1 if hits else 0
    for p in hits:
        p.unlink()
    print(f"removed {len(hits)} Salt-owned wheel(s)" + "".join(f"\n  {p.name}" for p in hits))
    return 0


def cmd_versions(args) -> int:
    own = {}
    for p in wheels(Path(args.own)):
        name, ver = parse_wheel_name(p)
        own[name] = ver
    root = Path(args.root)
    targets = {}
    for d in sorted(x for x in root.iterdir() if x.is_dir()):
        pk = {}
        for p in wheels(d):
            name, ver = parse_wheel_name(p)
            pk[name] = ver
        targets[d.name] = pk
    doc = {
        "python": args.pyver,
        "salt_tag": args.salt_tag,
        "own": own,
        "targets": targets,
        "pillar_hint": {
            "saltexts": {
                "python": args.pyver,
                "packages": {f"{k}[<extras>]": v for k, v in own.items()},
            },
        },
    }
    Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


GPL_RE = re.compile(r"(?<![A-Za-z])(AGPL|GPL)\b|GNU (Affero )?General Public", re.I)
LGPL_RE = re.compile(r"\bLGPL|Lesser|Library General", re.I)
WEAK_RE = re.compile(r"MPL|Mozilla|EPL|Eclipse", re.I)


def licence_of(wheel: Path) -> tuple[str, list[str]]:
    with zipfile.ZipFile(wheel) as zf:
        meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            return "UNKNOWN", []
        msg = email.parser.Parser().parsestr(zf.read(meta).decode("utf-8", "replace"))
    expr = msg.get("License-Expression") or ""
    lic = (msg.get("License") or "").strip()
    if len(lic) > 200 or "\n" in lic:  # the whole licence text was pasted into the field
        lic = lic.split("\n", 1)[0][:80] + " ..."
    classifiers = [c for c in msg.get_all("Classifier") or [] if c.startswith("License ::")]
    text = expr or lic or "; ".join(c.split("::")[-1].strip() for c in classifiers) or "UNKNOWN"
    return text, classifiers


def classify(text: str, classifiers: list[str]) -> str:
    """ok (permissive) / weak (file-level copyleft, informational) / LGPL (accepted) / GPL (fails the build)."""
    blob = " ".join([text, *classifiers])
    if LGPL_RE.search(blob):
        # an LGPL package is accepted; a string that ALSO names a plain GPL alternative still counts as LGPL-only here
        return "LGPL"
    if GPL_RE.search(blob):
        return "GPL"
    if WEAK_RE.search(blob):
        return "weak"
    return "ok"


def cmd_licences(args) -> int:
    allow = set()
    if args.allow and Path(args.allow).exists():
        for line in Path(args.allow).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                allow.add(norm(line))
    root = Path(args.root)
    rows: dict[str, tuple[str, str, str, set[str]]] = {}
    for d in sorted(x for x in root.iterdir() if x.is_dir()):
        for w in wheels(d):
            name, ver = parse_wheel_name(w)
            text, cls = licence_of(w)
            kind = classify(text, cls)
            key = f"{name}=={ver}"
            if key in rows:
                rows[key][3].add(d.name)
            else:
                rows[key] = (text, kind, ver, {d.name})
    bad = []
    lines = ["| Package | Version | Licence | Class | Targets |", "|---|---|---|---|---|"]
    for key in sorted(rows):
        text, kind, ver, tg = rows[key]
        name = key.split("==")[0]
        if kind == "GPL" and name not in allow:
            bad.append(key)
        lines.append(f"| {name} | {ver} | {text.replace('|', '/')} | {kind} | {len(tg)} |")
    report = "\n".join(lines) + "\n"
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
    lgpl = sorted(k for k, v in rows.items() if v[1] == "LGPL")
    unknown = sorted(k for k, v in rows.items() if v[0] == "UNKNOWN")
    print(
        f"{len(rows)} distinct wheels; LGPL (accepted, unmodified pip packages): {', '.join(lgpl) or 'none'}"
    )
    if unknown:
        print(f"WARNING: no licence metadata: {', '.join(unknown)}", file=sys.stderr)
    if bad:
        print(f"ERROR: GPL/AGPL licence in the wheelhouse: {', '.join(bad)}", file=sys.stderr)
        return 1
    print("no GPL/AGPL package")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stub")
    p.add_argument("outdir")
    p.add_argument("--lock", help="also write a stub for every package of this Salt lock file")
    p.add_argument("--skip", action="append", help="package of the lock to leave out (repeatable)")
    p.set_defaults(fn=cmd_stub)
    p = sub.add_parser("constraints")
    p.add_argument("lock")
    p.set_defaults(fn=cmd_constraints)
    p = sub.add_parser("salt-owned")
    p.add_argument("lock")
    p.add_argument("--out", required=True)
    p.add_argument("--skip", action="append")
    p.set_defaults(fn=cmd_salt_owned)
    p = sub.add_parser("check-pins")
    p.add_argument("--lock", required=True)
    p.add_argument("--out", help="write the requirements without the lines that Salt overrides")
    p.add_argument("reqfiles", nargs="+")
    p.set_defaults(fn=cmd_check_pins)
    p = sub.add_parser("strip")
    p.add_argument("dir")
    p.add_argument("--lock", required=True)
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_strip)
    p = sub.add_parser("names")
    p.add_argument("dir")
    p.add_argument("--exclude-own")
    p.set_defaults(fn=cmd_names)
    p = sub.add_parser("versions")
    p.add_argument("--out", required=True)
    p.add_argument("--pyver", required=True)
    p.add_argument("--own", required=True)
    p.add_argument("--root", required=True)
    p.add_argument(
        "--salt-tag", default=None, help="the Salt release whose package lock was honoured"
    )
    p.set_defaults(fn=cmd_versions)
    p = sub.add_parser("licences")
    p.add_argument("--root", required=True)
    p.add_argument("--markdown")
    p.add_argument("--allow")
    p.set_defaults(fn=cmd_licences)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
