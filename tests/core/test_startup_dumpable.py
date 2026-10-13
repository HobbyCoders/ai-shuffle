"""S2-X1: the app marks itself non-dumpable at startup on Linux.

Every model-run child (code-mode kernel, hooks, the CLI, MCP servers) runs as
the app's uid. While the app is dumpable such a child can read
``/proc/<app pid>/environ`` and so the app's start-up secrets, whatever the
child's own scrubbed environment says. ``harden_process_introspection`` calls
``prctl(PR_SET_DUMPABLE, 0)``; ``ALLOW_PROCESS_INTROSPECTION=true`` skips it.

The hook runs in a throwaway interpreter so the test process itself stays
dumpable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
import os, subprocess, sys
from app.core import startup
startup.settings.allow_process_introspection = sys.argv[1] == "1"
hardened = startup.harden_process_introspection(force=True)
probe = (
    "import sys\n"
    "try:\n"
    "    open('/proc/%s/environ' % sys.argv[1], 'rb').read()\n"
    "    print('read')\n"
    "except PermissionError:\n"
    "    print('denied')\n"
)
out = subprocess.run(
    [sys.executable, "-c", probe, str(os.getpid())],
    capture_output=True, text=True, timeout=60,
)
print(hardened, out.stdout.strip() or out.stderr.strip())
"""

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only"),
    pytest.mark.skipif(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        reason="root reads any /proc entry regardless of dumpable",
    ),
]


def _run(allow: bool) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT, "1" if allow else "0"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_child_cannot_read_the_hardened_apps_environ():
    assert _run(allow=False) == "True denied"


def test_allow_process_introspection_keeps_environ_readable():
    assert _run(allow=True) == "False read"


def test_testing_mode_keeps_the_test_process_dumpable(monkeypatch):
    """R13-8: every test that runs the app lifespan called the prctl on the
    pytest process itself. Under TESTING=1 the hardening is skipped unless
    forced, and the libc call is never made."""
    import ctypes

    from app.core import startup

    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setattr(startup.settings, "allow_process_introspection", False)
    calls = []

    def _recording_cdll(*_a, **_k):
        calls.append(_a)
        raise OSError("not in this test")

    monkeypatch.setattr(ctypes, "CDLL", _recording_cdll)
    assert startup.harden_process_introspection() is False
    assert calls == []
    # force=True still reaches the prctl (it fails here by design).
    assert startup.harden_process_introspection(force=True) is False
    assert len(calls) == 1


def test_non_linux_is_a_no_op(monkeypatch):
    from app.core import startup

    monkeypatch.setattr(startup.sys, "platform", "darwin")
    assert startup.harden_process_introspection() is False


def test_prctl_failure_is_logged_not_raised(monkeypatch, caplog):
    import ctypes

    from app.core import startup

    monkeypatch.setattr(startup.settings, "allow_process_introspection", False)

    def _broken(*_a, **_k):
        raise OSError("no libc")

    monkeypatch.setattr(ctypes, "CDLL", _broken)
    with caplog.at_level("WARNING", logger=startup.logger.name):
        assert startup.harden_process_introspection(force=True) is False
    assert "non-dumpable" in caplog.text


def test_setting_defaults_off():
    from app.core.config import Settings

    assert Settings.model_fields["allow_process_introspection"].default is False


def test_running_the_app_lifespan_leaves_the_test_process_dumpable(client):
    """R13-8: the ``client`` fixture runs the lifespan in this process."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c") or None)
    assert libc.prctl(3, 0, 0, 0, 0) == 1  # PR_GET_DUMPABLE
