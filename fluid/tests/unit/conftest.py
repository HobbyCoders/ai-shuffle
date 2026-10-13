"""Unit-tree setup: pin the code-mode OS sandbox for deterministic runs.

``KernelConfig.os_sandbox`` defaults to ``auto``, which sandboxes the kernel
child under bubblewrap on a host that has it and not on one that does not
(most CI runners). The kernel tests here exercise the child's *in-process*
layers (the audit hook, the builtins guards, the environment scrub) and must
see the same child on every host, so the tree runs with the sandbox ``off``
unless ``FLUID_TEST_CODE_MODE_OS_SANDBOX`` says otherwise. The sandbox itself
is covered by ``test_codemode_os_sandbox.py``, which sets the mode explicitly.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _pin_code_mode_os_sandbox(monkeypatch):
    monkeypatch.setenv(
        "CODE_MODE_OS_SANDBOX",
        os.environ.get("FLUID_TEST_CODE_MODE_OS_SANDBOX", "off"),
    )
