#!/usr/bin/env python3
"""Helpers for build_wheelhouse.sh. Standard library only, so it runs in any build image.

  stub      OUTDIR                      write a dependency-free stand-in wheel for `salt` (see below)
  names     DIR [--exclude-own OWNDIR]  print `name==version` for every wheel in DIR (sorted, normalised names)
  versions  --out FILE --pyver X.Y --own OWNDIR --root OUT    write VERSIONS.json
  licences  --root OUT [--markdown FILE] [--allow FILE]       licence report from wheel METADATA; exit 1 on GPL

Why a stub for `salt`: every Salt extension declares `salt>=...` as a dependency. Salt is only published as an sdist on PyPI, so
`pip download --only-binary=:all:` cannot resolve it, and the minions already have Salt (that is where the extension runs).
The stub is used ONLY at build time to let the resolver finish; it is never copied into the wheelhouse.
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


def cmd_stub(args) -> int:
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    dist = f"salt-{STUB_VERSION}.dist-info"
    files = {
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: salt\nVersion: {STUB_VERSION}\nSummary: BUILD-TIME STUB, never shipped\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: wheelhouse_tool\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for name, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    target = out / f"salt-{STUB_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    print(target)
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
        "own": own,
        "targets": targets,
        "pillar_hint": {
            "saltexts": {"python": args.pyver, "packages": {f"{k}[<extras>]": v for k, v in own.items()}},
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
    print(f"{len(rows)} distinct wheels; LGPL (accepted, unmodified pip packages): {', '.join(lgpl) or 'none'}")
    if unknown:
        print(f"WARNING: no licence metadata: {', '.join(unknown)}", file=sys.stderr)
    if bad:
        print(f"ERROR: GPL/AGPL licence in the wheelhouse: {', '.join(bad)}", file=sys.stderr)
        return 1
    print("no GPL/AGPL package")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stub")
    p.add_argument("outdir")
    p.set_defaults(fn=cmd_stub)
    p = sub.add_parser("names")
    p.add_argument("dir")
    p.add_argument("--exclude-own")
    p.set_defaults(fn=cmd_names)
    p = sub.add_parser("versions")
    p.add_argument("--out", required=True)
    p.add_argument("--pyver", required=True)
    p.add_argument("--own", required=True)
    p.add_argument("--root", required=True)
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
