"""T1 probe: PyMySQL authentication against MySQL 8 (caching_sha2_password) and MariaDB, through SQLAlchemy.
Matrix: TLS default vs ssl_disabled, cold vs warm server auth cache (FLUSH PRIVILEGES clears it).
Run it once in an environment WITH `cryptography` and once WITHOUT, label each run:
  python -I scripts/probes/probe_mysql_auth.py --label with-cryptography
Writes docs/wire_shapes/mysql_auth__<label>.json."""

import argparse
import importlib.metadata as md
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402
import pymysql  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--label", required=True)
ap.add_argument("--out", default="docs/wire_shapes")
args = ap.parse_args()
PORTS = {
    "mysql": int(os.environ.get("SQLX_MYSQL_PORT", "33061")),
    "mariadb": int(os.environ.get("SQLX_MARIADB_PORT", "33062")),
}
ADMIN_PW = os.environ.get("SQLX_ADMIN_PW", "admin_pw")


def flush(port):
    c = pymysql.connect(host="127.0.0.1", port=port, user="root", password=ADMIN_PW)
    c.cursor().execute("FLUSH PRIVILEGES")
    c.close()


def one(port, ssl_disabled):
    url = "mysql+pymysql://ro:ro_pw@127.0.0.1:%d/scratch" % port
    kw = {"connect_args": {"ssl_disabled": True}} if ssl_disabled else {}
    eng = sa.create_engine(url, poolclass=NullPool, **kw)
    try:
        with eng.connect() as c:
            cipher = c.exec_driver_sql("show session status like 'Ssl_cipher'").fetchone()[1]
            return {"status": "ok", "ssl_cipher": cipher or "(none: not encrypted)"}
    except Exception as e:  # pylint: disable=broad-except
        r = H.classify(e, ["ro_pw"])
        return {
            "status": "error",
            **{
                k: r[k]
                for k in (
                    "sa_exc",
                    "dbapi_error",
                    "driver_exc",
                    "message",
                    "vendor_code",
                    "sqlstate",
                    "password_in",
                )
            },
        }
    finally:
        eng.dispose()


rows = []
for server, port in PORTS.items():
    for ssl_disabled in (False, True):
        for cache in ("cold", "warm"):
            if cache == "cold":
                flush(port)
            rows.append(
                {
                    "server": server,
                    "ssl_disabled": ssl_disabled,
                    "auth_cache": cache,
                    **one(port, ssl_disabled),
                }
            )
meta = {"label": args.label}
try:
    meta["cryptography"] = md.version("cryptography")
except md.PackageNotFoundError:
    meta["cryptography"] = None
meta.update(PyMySQL=md.version("PyMySQL"), sqlalchemy=sa.__version__)
os.makedirs(args.out, exist_ok=True)
path = os.path.join(args.out, f"mysql_auth__{args.label}.json")
json.dump({"meta": meta, "results": rows}, open(path, "w"), indent=1)
print(path)
for r in rows:
    print(
        r["server"],
        "ssl_disabled=%s" % r["ssl_disabled"],
        r["auth_cache"],
        r["status"],
        r.get("ssl_cipher") or (r.get("sa_exc"), r.get("driver_exc"), r.get("message", "")[:90]),
    )
