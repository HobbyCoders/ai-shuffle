"""The scripted fake model-provider server used by native-leg tests.

The implementation lives in ``scripts/devloop/fake_wire.py`` so the tests and
the dev loop's fake AI (``python -m scripts.devloop.fake_ai``) share ONE
copy of the wire builders and server. It is loaded by path: the fluid suite
runs with ``fluid/`` as its rootdir, where ``scripts`` is not importable as
the repo's package. Everything public is re-exported unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_NAME = "ai_shuffle_devloop_fake_wire"
_PATH = Path(__file__).resolve().parents[3] / "scripts" / "devloop" / "fake_wire.py"


def _load():
    loaded = sys.modules.get(_NAME)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_NAME, _PATH)
    assert spec is not None and spec.loader is not None, _PATH
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


_wire = _load()

CUT = _wire.CUT
Chat = _wire.Chat
FakeProviderServer = _wire.FakeProviderServer
Messages = _wire.Messages
Recorded = _wire.Recorded
Reply = _wire.Reply
Responses = _wire.Responses
Sleep = _wire.Sleep
error_reply = _wire.error_reply
models_reply = _wire.models_reply
sse = _wire.sse
