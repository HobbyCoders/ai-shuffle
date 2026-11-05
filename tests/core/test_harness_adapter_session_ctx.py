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


@pytest.mark.parametrize("provider_id,model,expected", [
    (None, "sonnet", "sonnet"),
    ("account", "gpt-6.1-sol", "prov:account:gpt-6.1-sol"),
])
async def test_native_orchestration_gets_current_runtime_binding(monkeypatch, tmp_path, provider_id, model, expected):
    from types import SimpleNamespace

    import app.core.provider.harness_adapter as ha
    from app.core import session_orchestration

    monkeypatch.setattr(ha, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    adapter = ha.HarnessAdapter()
    await adapter.connect(SessionOptions(model=model, cwd=str(tmp_path),
                          extra={"fluid_provider": "openai_responses", "provider_pin_id": provider_id}))
    ctx = _CtxLoop.captured["ctx"]
    assert session_orchestration._runtime_model(ctx) == expected
    # The real live-set-model callback updates _sdk_options. The producer must
    # read it at call time, not keep the model captured when connect began.
    adapter._sdk_options = SimpleNamespace(model="haiku", aih_provider_id=None)
    assert session_orchestration._runtime_model(ctx) == "haiku"
    await adapter.disconnect()


@pytest.mark.parametrize("provider_id,model,expected", [
    (None, "sonnet", "sonnet"),
    ("account", "gpt-6.1-sol", "prov:account:gpt-6.1-sol"),
])
async def test_delegated_orchestration_bridge_carries_runtime_binding(
        mock_db, monkeypatch, tmp_path, provider_id, model, expected):
    from fluid.tools.registry import ToolOutcome

    from app.core import session_orchestration
    from app.mcp.builtin_tools import create_builtin_tools_server

    captured = []

    def make_handler(supplier):
        async def handle(action, data, ctx):
            captured.append(session_orchestration._runtime_model(ctx))
            return ToolOutcome(text="handled")
        return handle

    monkeypatch.setattr(session_orchestration, "make_handler", make_handler)
    server = create_builtin_tools_server(session_id="caller", model=model, provider_id=provider_id,
                                         cwd=str(tmp_path), is_admin=True)
    start = next(tool for tool in server["instance"].tools if tool.name == "StartSession")
    result = await start.handler({"title": "Child", "prompt": "go", "request_id": "r1"})
    assert not result.get("isError")
    assert captured == [expected]
