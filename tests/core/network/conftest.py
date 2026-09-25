"""Shared fixtures for the multi-worker network tests."""
import pytest

from app.api import pairing
from app.network import engine, tokens, transport
from tests.core.network.harness import Worker


@pytest.fixture
def workers(tmp_path):
    """Factory for in-process ``Worker``s; closes them and resets network state."""
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


@pytest.fixture(autouse=True)
def _reset_pairing_limiter():
    """Every pairing in a test counts against the per-process exchange limiter."""
    pairing._exchange_limiter.reset()
    yield
    pairing._exchange_limiter.reset()
