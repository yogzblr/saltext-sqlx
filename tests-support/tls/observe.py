"""Print the final caller-visible message of every TLS failure case on the three TLS test servers.

Run (never from inside src/saltext): ``python -I tests-support/tls/observe.py``. The output is what
docs/WIRE_SHAPES.md section 11 records. The expired-server case swaps the server certificate and restores it.
"""

import pathlib
import subprocess
import sys

from saltext.sqlx.lib import engine
from saltext.sqlx.lib import profiles

PKI = pathlib.Path("/var/tmp/sqlx-tls-dbs/pki")
HERE = pathlib.Path(__file__).resolve().parent
POLICY = {"ssl_certs_dir_root": str(PKI)}
SERVERS = {
    "postgresql 16.15": ("postgresql", 54339, "pg"),
    "mysql 8.0.46": ("mysql", 33071, "my"),
    "mariadb 10.11.14": ("mysql", 33072, "my"),
}


def run(dialect, port, family, directory, who="cert", host="localhost", query=None, user=None):
    cert_user = user or "tls_cert"
    users = {"cert": (cert_user, None if family == "pg" else "tls_cert_pw"), "pw": ("tls_pw", "tls_pw_pw")}
    name, password = users[who]
    raw = {"dialect": dialect, "host": host, "port": port, "database": "tlsdb", "username": name, "connect_timeout_s": 5}
    if password:
        raw["password"] = password
    if directory:
        raw["ssl_certs_dir"] = str(PKI / directory)
    if query:
        raw["query"] = query
    try:
        profile = profiles.validate_profile("t", raw, POLICY)
        engine.ping(profile, password)
    except Exception as exc:  # pylint: disable=broad-except
        return str(exc)
    return "OK"


def main():
    cases = [
        ("unknown CA (client has the wrong ca.pem)", dict(directory="wrong-ca")),
        ("hostname not in SAN (127.0.0.2)", dict(directory="good", host="127.0.0.2")),
        ("expired client certificate", dict(directory="expired-client")),
        ("client certificate from another CA", dict(directory="other-client")),
        ("client certificate required, none sent", dict(directory="ca-only")),
        ("server requires TLS, client used none", dict(directory=None, who="pw", query="plain")),
    ]
    for label, (dialect, port, family) in SERVERS.items():
        print(f"== {label}")
        for case, kw in cases:
            kw = dict(kw)
            if kw.get("query") == "plain":
                kw["query"] = {"sslmode": "disable"} if family == "pg" else {"ssl_disabled": True}
            print(f"- {case}:\n    {run(dialect, port, family, **kw)}")
        if family == "pg":
            print(f"- client certificate CN does not match the user:\n    {run(dialect, port, family, 'good', user='tls_mismatch')}")
        which = "postgresql" if family == "pg" else ("mysql" if port == 33071 else "mariadb")
        subprocess.run(["bash", str(HERE / "swap_server_cert.sh"), "expired", which], check=True, capture_output=True)
        try:
            print(f"- expired SERVER certificate:\n    {run(dialect, port, family, 'good')}")
        finally:
            subprocess.run(["bash", str(HERE / "swap_server_cert.sh"), "good", which], check=True, capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
