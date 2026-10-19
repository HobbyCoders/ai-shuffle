"""Fail at the leaking test, including modules first imported under a fake."""
import sys
from types import ModuleType

import pytest

from app.core.config import settings as _SETTINGS


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    # The outer wrapper runs AFTER every fixture finalizer. A function-scoped
    # autouse fixture can run too early, while monkeypatch is still active.
    try:
        return (yield)
    finally:
        leaked = sorted(
            name for name, module in tuple(sys.modules.items())
            if name.startswith("app.") and module is not None
            and "settings" in vars(module)
            # Packages expose child modules too (e.g. app.db.settings).
            and not (isinstance(vars(module)["settings"], ModuleType)
                     and vars(module)["settings"].__name__ == name + ".settings")
            and vars(module)["settings"] is not _SETTINGS
        )
        if leaked:
            pytest.fail(
                "Module settings leaked after " + item.nodeid + ": "
                + ", ".join(leaked)
                + ". Patch fields on config.settings, not the singleton itself.",
                pytrace=False,
            )
