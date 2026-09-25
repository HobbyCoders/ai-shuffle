"""The session pool reaches the delegated leg's claude CLI process.

``subprocess_pid`` feeds the dead-tree reaper and the OOM-score adjustment. It
reads ``adapter.subprocess_pid()``, which the harness adapter answers from the
DelegatedLoop's live process; the native leg runs in-process and has none.
"""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import session_pool as sp
from app.core.config import settings
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.session_pool import SessionState


def _adapter(*, delegated: bool, engine) -> HarnessAdapter:
    adapter = HarnessAdapter()
    adapter._is_delegated = delegated
    adapter._engine = engine
    return adapter


def test_harness_adapter_reports_the_delegated_cli_pid():
    adapter = _adapter(delegated=True, engine=SimpleNamespace(pid=4242))
    assert adapter.subprocess_pid() == 4242


def test_harness_adapter_has_no_child_on_the_native_leg_or_before_connect():
    assert _adapter(delegated=False, engine=SimpleNamespace(pid=4242)).subprocess_pid() is None
    assert _adapter(delegated=True, engine=None).subprocess_pid() is None
    # Delegated, but the first turn has not spawned the CLI yet.
    assert _adapter(delegated=True, engine=SimpleNamespace(pid=None)).subprocess_pid() is None


def test_pool_subprocess_pid_reads_the_adapter():
    state = SessionState(adapter=_adapter(delegated=True, engine=SimpleNamespace(pid=4242)))
    assert sp.subprocess_pid(state) == 4242


def test_pool_subprocess_pid_is_none_for_non_int_answers_and_errors():
    assert sp.subprocess_pid(SessionState(adapter=MagicMock())) is None
    broken = MagicMock()
    broken.subprocess_pid.side_effect = RuntimeError("boom")
    assert sp.subprocess_pid(SessionState(adapter=broken)) is None


@pytest.mark.skipif(not os.path.exists("/proc/self/oom_score_adj"), reason="needs /proc")
def test_oom_score_reaches_the_delegated_cli_process(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(settings, "sdk_subprocess_oom_score_adj", 377)
        state = SessionState(
            adapter=_adapter(delegated=True, engine=SimpleNamespace(pid=child.pid)),
        )
        sp.mark_subprocess_oom_killable(state)
        with open(f"/proc/{child.pid}/oom_score_adj") as f:
            assert int(f.read()) == 377
    finally:
        child.kill()
        child.wait(timeout=5)
