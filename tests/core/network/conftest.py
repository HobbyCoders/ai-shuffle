"""Shared fixtures for the multi-worker network tests."""
import pytest

from app.api import network as network_api
from app.api import pairing
from app.network import engine, tokens, transport
from tests.core.network.harness import Worker


@pytest.fixture
def workers(tmp_path):
    """Factory for in-process ``Worker``s; closes them and resets network state."""
    # A prior app lifespan registers materialization callbacks. Do not inherit
    # them: these workers explicitly drive apply side effects themselves.
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


_PER_PROCESS_LIMITERS = (
    pairing._exchange_limiter,
    network_api._join_limiter,
    network_api._challenge_limiter,
)


@pytest.fixture(autouse=True)
def _reset_request_limiters():
    """Start every test with empty per-process request limiters.

    Every in-process worker calls from the same client address, so pairings,
    joins and peer challenges (one per ``PeerLink.authenticate``) all count
    against one per-minute bucket. Without a reset, a test's outcome depended
    on how many earlier tests had authenticated in the last minute: a run of
    the network tests alone came within a few challenges of the 60/min cap,
    and an unlucky order got HTTP 429 instead of a device token.
    """
    for limiter in _PER_PROCESS_LIMITERS:
        limiter.reset()
    yield
    for limiter in _PER_PROCESS_LIMITERS:
        limiter.reset()
