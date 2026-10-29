# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
import pytest

from tests.security import harness


@pytest.fixture(scope="module", params=harness.KEYS)
def server(request):
    srv = harness.Server(request.param)
    reason = srv.reachable()
    if reason:
        pytest.skip(reason)
    return srv


@pytest.fixture
def salt(server):
    """The module with ``__salt__`` injected; profiles ``ro``, ``rw`` (execute), ``rw_query``, ``rw_guard``."""
    with harness.injected(server.profiles()) as module:
        yield module


@pytest.fixture(scope="module")
def meta(server):
    """The throwaway account ``t11_meta`` whose password is full of metacharacters; dropped afterwards."""
    harness.make_meta_account(server)
    try:
        yield server
    finally:
        harness.drop_meta_account(server)
