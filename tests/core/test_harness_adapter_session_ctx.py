"""A native leg must dispatch tools with the session id it actually got.

``connect()`` builds the ``ToolCtx`` from ``options.session_id or
options.resume_session_id`` — both None for a brand-new session. The real fluid
id is minted a few lines later by ``_ensure_session()``, and until it was
written back onto the ctx every session-scoped tool ran with
``session_id=None``: the Note tool wrote a shared ``default.md`` while the
notes reminder read ``<session>.md``, and two sessions on one project quietly
overwrote each other's notes.
"""

import pytest
from fluid.tools.registry import ToolCtx

from app.core.provider.types import SessionOptions

MINTED = "ses_minted"


class _CtxLoop:
    """NativeLoop stand-in that keeps the ctx it was constructed with (the real
    one does: ``self.ctx = ctx or ToolCtx()``)."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.ctx = kwargs.get("ctx") or ToolCtx()
        self.agents = list(kwargs.get("agents") or ())

    async def _ensure_session(self):
        class _Meta:
            session_id = MINTED

        return _Meta()

    async def aclose(self):
        pass


async def _fake_provider_client(kind, options):
    return object(), None


@pytest.mark.asyncio
async def test_new_session_gets_the_minted_id_written_back(monkeypatch, tmp_path):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )

    # Born with nothing (a new session has neither id), stamped once it exists.
    assert _CtxLoop.captured["ctx"].session_id == MINTED
    assert adapter._sdk_session_id == MINTED


@pytest.mark.asyncio
async def test_resumed_session_keeps_the_id_it_was_built_with(monkeypatch, tmp_path):
    """The write-back only fills a HOLE — it never renames a live session."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            resume_session_id="ses_existing",
            extra={"fluid_provider": "openai_responses"},
        )
    )

    assert _CtxLoop.captured["ctx"].session_id == "ses_existing"
