# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""Dependency licence collection and the GPL gate (SECURITY section 6, BUILD_PROMPT R16).

Reads REAL package metadata with ``importlib.metadata`` (``License-Expression``, ``License``, ``Classifier``), walks the
requirement closure of ``saltext.sqlx`` with every extra (``postgres``, ``mysql``, ``oracle``, ``mssql``), and classifies
each distribution:

* ``gpl``     GPL / AGPL of any version, with or without an "exception": the check FAILS.
* ``lgpl``    LGPL: allowed only for the packages the owner accepted (decision 2026-10-29, unmodified pip dependencies):
              ``psycopg``, ``psycopg-binary`` (the binary wheel of the same project) and ``pymssql``.
* ``weak``    MPL / EPL style file-level copyleft: reported, allowed (unmodified).
* ``permissive``, ``unknown`` (no licence declared in the metadata: reported, listed in ``DECLARED_UNKNOWN``).

No third-party code is needed. Run as a script from the repository root (outside ``src/saltext``)::

    python tests/security/licences.py --check      # exit 1 on any GPL or unaccepted LGPL, print the table
    python tests/security/licences.py --notice     # print the NOTICE section for the current environment
"""

import importlib.metadata as md
import re
import sys

ROOT = "saltext.sqlx"
EXTRAS = ("postgres", "mysql", "oracle", "mssql")

#: LGPL packages the owner accepted as UNMODIFIED pip dependencies (BUILD_PROMPT R16, owner decision 2026-10-29).
ACCEPTED_LGPL = {"psycopg", "psycopg-binary", "pymssql"}

#: Distributions whose metadata declares no licence at all (reviewed by hand; keep this list short).
DECLARED_UNKNOWN = {
    "tempora": "no licence field, classifier or licence file in the package metadata"
}

#: Distributions that must never be installed or required (GPL drivers).
FORBIDDEN = {"mysqlclient", "mysql-python", "pymysql-gpl"}

_GPL = re.compile(
    r"(?<![A-Za-z])(?:A|L)?GPL|GNU (?:General|Affero)|GENERAL PUBLIC LICENSE", re.IGNORECASE
)
_LGPL = re.compile(r"LGPL|LESSER GENERAL PUBLIC|LIBRARY GENERAL PUBLIC", re.IGNORECASE)
_WEAK = re.compile(r"MPL|Mozilla Public|EPL|Eclipse Public|CDDL", re.IGNORECASE)


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def classify(text):
    """Return ``gpl`` / ``lgpl`` / ``weak`` / ``permissive`` / ``unknown`` for the licence text of one distribution."""
    text = (text or "").strip()
    if not text:
        return "unknown"
    # a licence that is an LGPL text mentions "General Public License" too, so test LGPL first
    if _LGPL.search(text):
        return "lgpl"
    if _GPL.search(text):
        return "gpl"
    if _WEAK.search(text):
        return "weak"
    return "permissive"


def licence_text(dist):
    """Licence statement from the metadata: expression, classifiers, and the first line of the ``License`` field."""
    meta = dist.metadata
    parts = []
    if meta.get("License-Expression"):
        parts.append(meta["License-Expression"])
    parts += [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License")
    ]
    field = (meta.get("License") or "").strip()
    if field:
        parts.append(" ".join(field.split())[:80])
    return " | ".join(parts)


def closure():
    """``{normalised name: Distribution}`` of the root package and everything its requirements pull in (installed only)."""
    from packaging.markers import default_environment  # pylint: disable=import-outside-toplevel
    from packaging.requirements import Requirement  # pylint: disable=import-outside-toplevel

    env = default_environment()
    seen = {}
    todo = [(ROOT, EXTRAS)]
    done = set()
    while todo:
        name, extras = todo.pop()
        key = (norm(name), tuple(sorted(extras)))
        if key in done:
            continue
        done.add(key)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        seen[norm(name)] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None and not any(
                req.marker.evaluate({**env, "extra": e}) for e in (extras or ("",))
            ):
                continue
            todo.append((req.name, tuple(req.extras)))
    return seen


def rows():
    """Sorted ``(name, version, licence text, class)`` for the closure, root excluded."""
    out = []
    for key, dist in sorted(closure().items()):
        if key == norm(ROOT):
            continue
        text = licence_text(dist)
        out.append((dist.metadata["Name"], dist.version, text, classify(text)))
    return out


def violations(table=None):
    """Messages for every GPL, every unaccepted LGPL and every forbidden distribution."""
    table = rows() if table is None else table
    bad = []
    for name, version, text, kind in table:
        if kind == "gpl" or norm(name) in FORBIDDEN:
            bad.append(f"GPL/forbidden: {name} {version} ({text})")
        elif kind == "lgpl" and norm(name) not in ACCEPTED_LGPL:
            bad.append(f"LGPL not accepted by the owner: {name} {version} ({text})")
    return bad


def render_notice(table=None):
    table = rows() if table is None else table
    lines = [
        f"| {n} | {v} | {(t or 'none declared in package metadata').replace(' | ', ' / ')} | {k} |"
        for n, v, t, k in table
    ]
    return "\n".join(lines)


def main(argv):
    table = rows()
    if "--notice" in argv:
        print(render_notice(table))
        return 0
    for name, version, text, kind in table:
        print(f"{kind:10} {name} {version}  {text}")
    bad = violations(table)
    for message in bad:
        print("FAIL", message, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
