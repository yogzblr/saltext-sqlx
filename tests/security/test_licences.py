# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""SECURITY section 6, last box: dependency licences are listed in ``NOTICE`` and no GPL is present.

Uses real package metadata (``tests/security/licences.py``). The same check runs without pytest or nox::

    python tests/security/licences.py --check
"""

import importlib.metadata as md
import pathlib
import tomllib

import pytest

from tests.security import licences

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def table():
    return licences.rows()


def test_closure_was_found(table):
    names = {licences.norm(r[0]) for r in table}
    assert {
        "salt",
        "sqlalchemy",
    } <= names, "the dependency walk found nothing: is saltext.sqlx installed?"


def test_no_gpl_and_no_unaccepted_lgpl(table):
    assert licences.violations(table) == []


def test_only_the_accepted_lgpl_packages_are_lgpl(table):
    lgpl = {licences.norm(r[0]) for r in table if r[3] == "lgpl"}
    assert lgpl <= licences.ACCEPTED_LGPL, lgpl - licences.ACCEPTED_LGPL


def test_packages_without_a_declared_licence_are_reviewed(table):
    unknown = {licences.norm(r[0]) for r in table if r[3] == "unknown"}
    assert unknown <= set(licences.DECLARED_UNKNOWN), unknown - set(licences.DECLARED_UNKNOWN)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("GPL-3.0-only", "gpl"),
        ("GPL-2.0-or-later", "gpl"),
        ("GPL-2.0-only WITH Classpath-exception-2.0", "gpl"),
        ("AGPL-3.0-or-later", "gpl"),
        ("GNU General Public License v2 (GPLv2)", "gpl"),
        ("GNU Affero General Public License v3", "gpl"),
        ("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007", "gpl"),
        ("MIT | GPL-2.0-only", "gpl"),
        ("LGPL-3.0-only", "lgpl"),
        ("GNU Lesser General Public License v2 or later (LGPLv2+)", "lgpl"),
        ("GNU LESSER GENERAL PUBLIC LICENSE Version 2.1, February 1999", "lgpl"),
        ("MPL-2.0", "weak"),
        ("Mozilla Public License 2.0 (MPL 2.0)", "weak"),
        ("MIT", "permissive"),
        ("Apache-2.0 OR BSD-3-Clause", "permissive"),
        ("BSD License", "permissive"),
        ("UPL-1.0 OR Apache-2.0", "permissive"),
        ("Python Software Foundation License", "permissive"),
        ("", "unknown"),
    ],
)
def test_classifier_recognises_licences(text, kind):
    assert licences.classify(text) == kind


def test_gate_fails_on_a_gpl_distribution(monkeypatch, capsys):
    fake = [
        ("evil-driver", "1.0", "GPL-2.0-only", "gpl"),
        ("mysqlclient", "2.2", "GPL-2.0-or-later", "gpl"),
    ]
    assert len(licences.violations(fake)) == 2
    monkeypatch.setattr(licences, "rows", lambda: fake)
    assert licences.main([]) == 1
    assert "FAIL GPL/forbidden: evil-driver" in capsys.readouterr().err


def test_gate_fails_on_an_lgpl_nobody_accepted():
    assert licences.violations([("other-lib", "1", "LGPL-2.1-only", "lgpl")])
    assert not licences.violations([("pymssql", "2.4.6", "LGPL-2.1", "lgpl")])


def test_gpl_driver_mysqlclient_is_neither_installed_nor_declared():
    with pytest.raises(md.PackageNotFoundError):
        md.distribution("mysqlclient")
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        declared += extra
    names = {
        licences.norm(d.replace("[", " ").split()[0].split(">")[0].split("<")[0].split("=")[0])
        for d in declared
    }
    assert not names & licences.FORBIDDEN


def test_notice_lists_every_runtime_and_optional_dependency(table):
    notice = (REPO / "NOTICE").read_text(encoding="utf-8").lower()
    missing = [r[0] for r in table if r[0].lower() not in notice]
    assert (
        not missing
    ), f"NOTICE lacks: {missing}; regenerate the table with `python tests/security/licences.py --notice`"


def test_notice_keeps_the_template_text_and_names_the_lgpl_decision():
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    assert notice.startswith("Salt Extension Modules for SQL databases")
    assert "Apache 2.0 license" in notice and "Copyright 2026 Yogesh Gundurao" in notice
    assert (
        "LGPL" in notice and "unmodified" in notice and "psycopg" in notice and "pymssql" in notice
    )
