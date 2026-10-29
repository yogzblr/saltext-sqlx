"""Render docs/WIRE_SHAPES.md from the JSON written by the probes.

Tables are generated from observed data only. Prose lives in docs/wire_shapes/_header.md and _findings.md and is
spliced in. Run from the repo root: python -I scripts/probes/render_wire_shapes.py
"""

import json
import os

DIALECTS = ["postgresql", "mysql", "mariadb", "oracle", "mssql"]
SA_MAIN, SA_NEW = "2.0.55", "2.1.6"
DATA = "docs/wire_shapes"


def load(dialect, sa):
    path = f"{DATA}/{dialect}__sa{sa}.json"
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["meta"], {r["case"]: r for r in d["results"]}


def cell(s, n=110):
    s = " ".join(str(s).split())
    s = s.replace("|", "\\|")
    return s if len(s) <= n else s[: n - 3] + "..."


def fields(r):
    parts = []
    if r.get("args_types"):
        parts.append("args(" + ",".join(r["args_types"]) + ")")
    if r.get("diag_fields"):
        keys = [
            k
            for k in r["diag_fields"]
            if not k.startswith("source_") and not k.startswith("severity")
        ]
        parts.append("diag{" + ",".join(keys) + "}")
    if r.get("args0_fields"):
        parts.append("args[0]{" + ",".join(r["args0_fields"]) + "}")
    attrs = [k for k in (r.get("driver_attrs") or {}) if k not in ("traceback",)]
    if attrs:
        parts.append("attrs{" + ",".join(attrs) + "}")
    if "traceback" in (r.get("driver_attrs") or {}):
        parts.append("attrs{traceback}")
    return " ".join(parts) or "-"


def why(r):
    return "no server available (section 0)" if r.get("reason") else "-"


def sqlblock(r):
    if r.get("parameters_block_in_str"):
        return "SQL + params"
    return "SQL" if r.get("sql_text_in_str") else "no"


def pw(r):
    leaked = [k for k, v in (r.get("password_in") or {}).items() if v]
    return "YES: " + ",".join(leaked) if leaked else "no"


def err_row(dialect, case, r, label=None):
    label = label or case
    if r is None:
        return f"| {dialect} | {label} | NOT RUN | | | | | | | case not defined |"
    if r["status"] == "NOT RUN":
        return f"| {dialect} | {label} | **NOT RUN** | | | | | | | {why(r)} |"
    if r["status"] == "n/a":
        return f"| {dialect} | {label} | n/a | | | | | | | {cell(r['reason'])} |"
    if r["status"] == "ok":
        val = r.get("data", r.get("value"))
        return f"| {dialect} | {label} | no error | | | | | | | returned {cell(val, 90)} |"
    sa = (r.get("sa_exc") or "(not SQLAlchemy) ").replace("sqlalchemy.exc.", "")
    drv = r["driver_exc"]
    sqlstate = r.get("sqlstate") or "-"
    vendor = r.get("vendor_code") if r.get("vendor_code") is not None else "-"
    sqltxt = sqlblock(r)
    return (
        f"| {dialect} | {label} | {sa} | `{drv}` | {sqlstate} | {vendor} | {cell(fields(r), 80)} | {pw(r)} | {sqltxt} | "
        f"{cell(r.get('message', ''))} |"
    )


HDR = (
    "| Dialect | Case | SQLAlchemy exception | Driver exception | SQLSTATE | Vendor code | Fields on the driver exception | "
    "Password in exception text | `[SQL:]`/`[parameters:]` block in `str(exc)` | Observed message |\n|---|---|---|---|---|---|---|---|---|---|"
)

REQUIRED = [
    ("syntax_error", "1. syntax error (`selec 1`)"),
    ("missing_table", "2. missing table"),
    ("missing_column", "3. missing column"),
    ("division_by_zero", "4. division by zero (`SELECT 1/0`)"),
    ("unique_violation", "5. unique violation (as `rw`)"),
    ("permission_denied_insert", "6. permission denied (`ro` INSERT)"),
    ("wrong_password", "8. wrong password"),
    ("unreachable_port", "9. unreachable port (port 1)"),
]
EXTRA = [
    ("division_by_zero_dml", "4b. division by zero in INSERT ... SELECT 1/0"),
    ("permission_denied_select", "6b. permission denied (`ro` SELECT other schema)"),
    ("ro_update", "6c. `ro` UPDATE"),
    ("ro_delete", "6d. `ro` DELETE"),
    ("ro_ddl", "6e. `ro` CREATE TABLE"),
    ("unknown_database", "x1. unknown database (as `rw`)"),
    ("connect_timeout_blackhole", "x2. connect timeout to 192.0.2.1"),
    ("bad_cast_bound_value", "x3. bad bound value (does the text echo it?)"),
    ("rw_write_then_rollback", "x4. `rw` write then rollback (query path)"),
]


def main():
    runs = {d: load(d, SA_MAIN) for d in DIALECTS}
    new = {d: load(d, SA_NEW) for d in DIALECTS}
    out = []
    out.append(open(f"{DATA}/_header.md", encoding="utf-8").read().rstrip() + "\n")

    # 0. run status
    out.append("## 0. What ran\n")
    out.append(
        "| Dialect | Server (observed) | Driver | SQLAlchemy | Status | Reason |\n|---|---|---|---|---|---|"
    )
    for d in DIALECTS:
        meta, recs = runs[d]
        ping = recs.get("ping")
        srv = "-"
        if ping and ping["status"] == "ok":
            srv = cell(ping["data"]["version"], 70)
        drivers = ", ".join(f"{k} {v}" for k, v in meta["drivers"].items() if v)
        if meta["server_reachable"]:
            status = "RAN (all cases)"
            reason = ""
        else:
            ran = sum(1 for r in recs.values() if r["status"] not in ("NOT RUN",))
            status = f"NOT RUN (server); {ran} client-side records only"
            reason = meta["not_run_reason"]
        out.append(
            f"| {d} | {srv} | {drivers} | {SA_MAIN} (and {SA_NEW}) | {status} | {cell(reason, 160)} |"
        )
    out.append("")

    # 1. error cases
    out.append("## 1. Error shapes: one row per dialect x case\n")
    out.append(
        "`NOT RUN` rows could not be executed (section 0). A row for a client-side case on Oracle or SQL Server "
        "(unreachable port, connect timeout) is real: it needs no server. "
        f"All rows are SQLAlchemy {SA_MAIN}; section 8 compares {SA_NEW}.\n"
    )
    out.append(HDR)
    for case, label in REQUIRED + EXTRA:
        for d in DIALECTS:
            out.append(err_row(d, case, runs[d][1].get(case), label))
    out.append("")

    # 2. timeouts
    out.append("## 2. Statement timeout (case 7)\n")
    out.append(
        "| Dialect | Case | Mechanism | Statement | Elapsed (s) | Outcome | SQLAlchemy / driver exception | SQLSTATE | Vendor code | Message / result |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    for d in DIALECTS:
        recs = runs[d][1]
        names = [c for c, r in recs.items() if "mechanism" in r]
        for c in names:
            r = recs[c]
            if r["status"] == "NOT RUN":
                out.append(
                    f"| {d} | {c} | {cell(r.get('mechanism', ''))} | `{cell(r.get('statement', ''), 50)}` | | **NOT RUN** | | | | {why(r)} |"
                )
                continue
            chk = r.get("checks")
            mech = r.get("mechanism", "") + (f" (readback {chk})" if chk else "")
            if r["status"] == "ok":
                out.append(
                    f"| {d} | {c} | {cell(mech, 90)} | `{cell(r.get('statement', ''), 50)}` | {r.get('elapsed_s', '')} | **no error** | | | | returned {cell(r['value'], 60)} |"
                )
            else:
                sa = (r.get("sa_exc") or "").replace("sqlalchemy.exc.", "")
                out.append(
                    f"| {d} | {c} | {cell(mech, 90)} | `{cell(r.get('statement', ''), 50)}` | {r.get('elapsed_s', '')} | error | "
                    f"{sa} / `{r['driver_exc']}` | {r.get('sqlstate') or '-'} | {r.get('vendor_code') if r.get('vendor_code') is not None else '-'} | {cell(r['message'], 90)} |"
                )
    out.append("")

    # 3. guard and bypass
    out.append("## 3. In-session read-only guard and bypass attempts (cases 10 and 11)\n")
    out.append(
        "Account `rw`, table `scratch.t_probe`. Each attempt runs the statements in order on one connection, then "
        "inserts a marker row and commits. *Persisted* is the row count read back on a second connection (0 = refused "
        "or lost, 1 = the write is durable).\n"
    )
    out.append(
        "| Dialect | Attempt (pre-statements, then INSERT + COMMIT) | Outcome | Failed at step | Rows persisted | SQLSTATE | Vendor code | Message |\n|---|---|---|---|---|---|---|---|"
    )
    for d in DIALECTS:
        recs = runs[d][1]
        for c, r in recs.items():
            if not (c.startswith("guard") or c.startswith("bypass") or c.startswith("connlevel")):
                continue
            if r["status"] == "NOT RUN":
                out.append(f"| {d} | {cell(c, 120)} | **NOT RUN** | | | | | {why(r)} |")
            elif r["status"] == "ok":
                out.append(
                    f"| {d} | {cell(c, 120)} | **write succeeded** | | {r['persisted_row_count']} | | | |"
                )
            else:
                out.append(
                    f"| {d} | {cell(c, 120)} | refused | {cell(r.get('failed_at_step'), 40)} | {r['persisted_row_count']} | {r.get('sqlstate') or '-'} | "
                    f"{r.get('vendor_code') if r.get('vendor_code') is not None else '-'} | {cell(r['message'], 80)} |"
                )
    out.append("")

    # 4. types
    out.append("## 4. Types returned for `SELECT * FROM scratch.orders` (through `text()`)\n")
    cols = []
    for d in DIALECTS:
        r = runs[d][1].get("types_roundtrip")
        if r and r["status"] == "ok":
            for c in r["data"]["1"]:
                if c not in cols:
                    cols.append(c)
    out.append("| Column | " + " | ".join(DIALECTS) + " |\n|---|" + "---|" * len(DIALECTS))
    for c in cols:
        cells = []
        for d in DIALECTS:
            r = runs[d][1].get("types_roundtrip")
            if r is None or r["status"] == "NOT RUN":
                cells.append("**NOT RUN**")
            elif c not in r["data"]["1"]:
                cells.append("-")
            else:
                t, v = r["data"]["1"][c]
                n = r["data"]["2"][c][0]
                cells.append(f"`{t}` {cell(json.dumps(v, ensure_ascii=False), 40)}; row 2: `{n}`")
        out.append(f"| {c} | " + " | ".join(cells) + " |")
    out.append("")

    # 5. traps
    out.append("## 5. `text()` bind traps and `%` handling\n")
    out.append("Live results (account `rw`):\n")
    out.append("| Case | SQL | " + " | ".join(DIALECTS) + " |\n|---|---|" + "---|" * len(DIALECTS))
    trap_cases = [
        ("text_trap_colon_cast", "`select :a::int`"),
        ("text_trap_json_colon_literal", "`select '{\"a\":1}'`"),
        ("text_trap_json_colon_escaped", "`select '{\"a\"\\:1}'`"),
        ("text_percent_in_text", "`like '%b%'` via `text()`"),
        ("text_cast_bind", "`CAST(:a AS int)` via `text()`, a=5"),
        ("driver_sql_percent_no_params", "`like '%b%'` via `exec_driver_sql()`, no params"),
    ]
    for c, sql in trap_cases:
        cells = []
        for d in DIALECTS:
            r = runs[d][1].get(c)
            if r is None or r["status"] == "NOT RUN":
                cells.append("**NOT RUN**")
            elif r["status"] == "ok":
                cells.append("ok " + cell(r["value"], 40))
            else:
                code = r.get("vendor_code") or r.get("sqlstate") or ""
                cells.append(f"ERR `{r['driver_exc']}` {code}: {cell(r['message'], 55)}")
        out.append(f"| {c} | {sql} | " + " | ".join(cells) + " |")
    out.append(
        "\nClient-side compilation (no server needed, so it exists for every dialect). `bind names` are the parameters SQLAlchemy found:\n"
    )
    out.append("| Statement | " + " | ".join(DIALECTS) + " |\n|---|" + "---|" * len(DIALECTS))
    labels = list(runs["postgresql"][1]["text_trap_compile_client_side"]["compiled"])
    for lab in labels:
        cells = []
        for d in DIALECTS:
            comp = runs[d][1]["text_trap_compile_client_side"]["compiled"][lab]
            cells.append(
                f"binds {comp.get('bind_names')} -> `{cell(comp.get('compiled', comp.get('error')), 45)}`"
            )
        out.append(f"| {cell(lab, 40)} | " + " | ".join(cells) + " |")
    out.append(
        "\nParamstyle per dialect: "
        + ", ".join(
            f"{d} = {runs[d][1]['text_trap_compile_client_side']['paramstyle']}" for d in DIALECTS
        )
        + ".\n"
    )

    # 6. auth matrix
    out.append("## 6. PyMySQL authentication matrix (through SQLAlchemy)\n")
    out.append(
        "Account `ro`, default plugin `caching_sha2_password` on MySQL 8.0.46, `mysql_native_password` on MariaDB. *Cold* = right after `FLUSH PRIVILEGES` (empties the server's auth cache).\n"
    )
    out.append(
        "| `cryptography` | Server | `ssl_disabled` | Auth cache | Outcome | Detail |\n|---|---|---|---|---|---|"
    )
    for label in ("with-cryptography", "without-cryptography"):
        with open(f"{DATA}/mysql_auth__{label}.json", encoding="utf-8") as f:
            a = json.load(f)
        ver = a["meta"]["cryptography"] or "absent"
        for r in a["results"]:
            if r["status"] == "ok":
                det = f"connected, Ssl_cipher = {r['ssl_cipher']}"
                oc = "ok"
            else:
                det = f"`{r['driver_exc']}` (SQLAlchemy wrapped: {r['dbapi_error']}): {cell(r['message'], 90)}"
                oc = "**error**"
            out.append(
                f"| {ver} | {r['server']} | {r['ssl_disabled']} | {r['auth_cache']} | {oc} | {det} |"
            )
    out.append("")

    # 7. SQL_ASCII
    with open(f"{DATA}/postgresql_sqlascii.json", encoding="utf-8") as f:
        asc = json.load(f)["results"]
    out.append("## 7. PostgreSQL database created as SQL_ASCII\n")
    out.append(
        "| Step | Outcome | SQLAlchemy exception | Driver / raw exception | Message |\n|---|---|---|---|---|"
    )
    for r in asc:
        out.append(
            f"| {r['step']} | {r['status']} | {r.get('sa_exc')} | `{r.get('driver_exc', '')}` (DBAPIError: {r.get('dbapi_error')}) | {cell(r.get('message', r.get('value')))} |"
        )
    out.append("")

    # 8. SA 2.1
    out.append(f"## 8. SQLAlchemy {SA_NEW} against the same cases (decision D4)\n")
    keys = [
        "status",
        "sa_exc",
        "driver_exc",
        "sqlstate",
        "vendor_code",
        "message",
        "failed_at_step",
        "persisted_row_count",
    ]
    rows = []
    total = 0
    for d in DIALECTS:
        a, b = runs[d][1], new[d][1]
        nsame = 0
        for c in a:
            ra, rb = a[c], b.get(c)
            total += 1
            diffs = []
            for k in keys:
                x, y = ra.get(k), (rb or {}).get(k)
                if isinstance(x, str):
                    x = x.replace("/e/20/", "/e/XX/")
                if isinstance(y, str):
                    y = y.replace("/e/21/", "/e/XX/")
                if k == "message":
                    x = (x or "").split("CONNECTION_ID=")[0] if "CONNECTION_ID" in (x or "") else x
                    y = (y or "").split("CONNECTION_ID=")[0] if "CONNECTION_ID" in (y or "") else y
                if x != y:
                    diffs.append(k)
            if c == "text_trap_compile_client_side" and ra.get("compiled") != (rb or {}).get(
                "compiled"
            ):
                diffs.append("compiled")
            if diffs:
                rows.append((d, c, diffs))
            else:
                nsame += 1
        out.append(
            f"- {d}: {nsame} of {len(a)} records identical on {SA_MAIN} and {SA_NEW} (ignoring the documentation-URL suffix and the random Oracle `CONNECTION_ID`)."
        )
    out.append("")
    if rows:
        out.append(
            "Differences: " + "; ".join(f"{d}/{c}: {','.join(k)}" for d, c, k in rows) + "\n"
        )
    else:
        out.append("No differing record.\n")
    meta21 = new["postgresql"][0]
    out.append(
        f"Versions used for the {SA_NEW} run: "
        + json.dumps({"sqlalchemy": meta21["sqlalchemy"], **meta21["drivers"]})
        + ".\n"
    )

    out.append(open(f"{DATA}/_findings.md", encoding="utf-8").read().rstrip() + "\n")
    with open("docs/WIRE_SHAPES.md", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("docs/WIRE_SHAPES.md written")


if __name__ == "__main__":
    main()
