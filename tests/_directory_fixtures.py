"""Keep conftest fixtures visible when pytest collects a directory twice.

pytest 9.1 binds a conftest's fixtures (and its autouse list) to the first
``Directory`` node collected for that conftest's folder, and matches them by
node identity. A command line that names a file in a folder, then a file
directly in its parent, then the folder again, for example::

    pytest tests/core/network/test_api.py tests/core/test_usage_limits.py \
        tests/core/network/test_collections.py

re-collects the parent (files named directly are not deduplicated) and gets
a second node for the folder. The conftest was already consumed by the first
node, so tests under the second one fail with "fixture 'workers' not found"
and silently lose the folder's autouse fixtures.

This hook gives every later ``Directory`` node for the same folder the
conftest's fixtures too. Pytest versions that match conftest fixtures by
node id have no pending-conftest queue and need nothing, so it is a no-op
there.
"""
from __future__ import annotations

import pytest
from _pytest import nodes

_DIRECTORY = getattr(nodes, "Directory", None)
_FIRST_NODE = pytest.StashKey[dict]()


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    yield
    if _DIRECTORY is None or not isinstance(collector, _DIRECTORY):
        return
    session = collector.session
    manager = getattr(session, "_fixturemanager", None)
    if manager is None or not hasattr(manager, "_pending_conftests"):
        return
    first = session.stash.setdefault(_FIRST_NODE, {})
    owner = first.setdefault(collector.path, collector)
    if owner is not collector:
        conftest = collector.config.pluginmanager.get_plugin(str(collector.path / "conftest.py"))
        if conftest is not None:
            manager.parsefactories(holder=conftest, node=collector)
