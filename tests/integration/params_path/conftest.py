"""
Fixtures for call-time connection parameters through a REAL master and minion (SPEC section 3.2).

Own package, so the existing ``salt_path`` suite keeps its configuration (feature off). Here the minion enables
``sqlx:_policy:allow_call_params`` and its only profile ``pg_dead`` points at a closed port with a wrong account: a call
works only if the call-supplied host/port/user/password/database are the ones used. The databases come from
``tests-support/env.sh`` and are never started or stopped here.
"""

# pylint: disable=unused-import,redefined-outer-name
import pytest

from tests.integration.salt_path.conftest import ADMIN_URLS
from tests.integration.salt_path.conftest import EAUTH_PASSWORD
from tests.integration.salt_path.conftest import EAUTH_USER
from tests.integration.salt_path.conftest import HOST
from tests.integration.salt_path.conftest import MARIADB_PORT
from tests.integration.salt_path.conftest import MYSQL_PORT
from tests.integration.salt_path.conftest import PG_PORT
from tests.integration.salt_path.conftest import RO_PASSWORD
from tests.integration.salt_path.conftest import _profile
from tests.integration.salt_path.conftest import api_port
from tests.integration.salt_path.conftest import call
from tests.integration.salt_path.conftest import client
from tests.integration.salt_path.conftest import databases_up
from tests.integration.salt_path.conftest import sdb_file
from tests.integration.salt_path.conftest import t10_tables
from tests.integration.salt_path.conftest import work_dir

SDB_REF = "sdb://t10_sdb/t10_pg_ro_password"
OTHER_USER = "t12_bob"
OTHER_PASSWORD = "t12_bob_pw_77aa"


@pytest.fixture(scope="package")
def eauth_file(work_dir):
    path = work_dir / "t12_eauth.txt"
    path.write_text(f"{EAUTH_USER}:{EAUTH_PASSWORD}\n{OTHER_USER}:{OTHER_PASSWORD}\n")
    return path


@pytest.fixture(scope="package")
def master_config(eauth_file, api_port):
    """
    ``file`` eauth. alice: ``host`` and ``profile`` are pinned by regex on every function she may call (SECURITY
    section 4: a kwargs rule needs the key to be PRESENT and uses ``re.match``, so anchor it). bob: only ``profile`` is
    pinned, so ``host`` is unrestricted by eauth and the minion-side policy is the only control.
    """
    pinned = {"profile": r"^pg_dead$", "host": r"^127\.0\.0\.1$"}
    return {
        "rest_cherrypy": {"port": api_port, "host": HOST, "disable_ssl": True},
        "netapi_enable_clients": ["local"],
        "log_level": "debug",
        "log_level_logfile": "debug",
        "external_auth": {
            "file": {
                "^filename": str(eauth_file),
                EAUTH_USER: [
                    {"*": [{"sqlx.ping": {"kwargs": pinned}}, {"sqlx.query": {"kwargs": pinned}}]}
                ],
                OTHER_USER: [{"*": [{"sqlx.ping": {"kwargs": {"profile": r"^pg_dead$"}}}]}],
            }
        },
    }


@pytest.fixture(scope="package")
def minion_config(sdb_file):
    dead = _profile("postgresql", 1, "sqlx_test", "nobody", "WRONG-BASE-PW-31f0")
    return {
        "log_level": "debug",
        "log_level_logfile": "debug",
        "sqlx": {
            "profiles": {"pg_dead": dead},
            "default_profile": "pg_dead",
            "_policy": {
                "allow_call_params": [
                    "host",
                    "port",
                    "database",
                    "username",
                    "password",
                    "dialect",
                    "read_only",
                    "statement_timeout_ms",
                ],
                "allowed_hosts": ["127.0.0.1"],
            },
        },
        "t10_sdb": {"driver": "yaml", "files": [str(sdb_file)]},
    }
