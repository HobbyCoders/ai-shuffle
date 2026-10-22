"""Attribution reaches both session engines, with no provider calls."""
import logging
from types import SimpleNamespace

import pytest

from app.core import commit_attribution
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import database
from fluid.context.attribution import COMMIT_TRAILER, PR_LINE, cli_attribution, prompt_block
from fluid.context.trim import estimate_tokens_of_text
from fluid.tools.registry import ToolRegistry


class CaptureLoop:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.agents = []

    async def _ensure_session(self):
        return SimpleNamespace(session_id="fluid-attribution")

    async def aclose(self):
        pass


@pytest.fixture
def capture_engines(monkeypatch):
    async def provider(kind, options):
        return SimpleNamespace(name=kind), None

    monkeypatch.setattr(ha, "NativeLoop", CaptureLoop)
    monkeypatch.setattr(ha, "_build_provider_client", provider)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])


async def connect(tmp_path, delegated=False, session_id=None):
    adapter = ha.HarnessAdapter()
    await adapter.connect(SessionOptions(
        model="sonnet" if delegated else "gpt-5",
        cwd=str(tmp_path), system_prompt="Agent instructions.",
        extra={"fluid_registry": ToolRegistry(), "app_session_id": session_id,
               **({} if delegated else {"fluid_provider": "openai_responses"})},
    ))
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_native_prompt_uses_owner_policy(tmp_path, capture_engines, enabled):
    commit_attribution.write(enabled)
    adapter = await connect(tmp_path)
    try:
        text = adapter._engine.kwargs["system"]
        assert text.count(prompt_block(enabled)) == 1
        assert prompt_block(not enabled) not in text
        # The snapshot survives prompt recompiles (for Task roster changes).
        commit_attribution.write(not enabled)
        compiled = adapter._compile_prompt_sections(
            adapter._prompt_inputs["system"], adapter._prompt_inputs["append_system"],
            adapter._options, **{k: v for k, v in adapter._prompt_inputs.items()
                                if k not in ("system", "append_system")},
        )
        assert compiled is not None
        assert compiled.text.count(prompt_block(enabled)) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_delegated_policy_reaches_cli_without_duplicate_prompt(
    tmp_path, capture_engines, enabled,
):
    import json

    commit_attribution.write(enabled)
    adapter = await connect(tmp_path, delegated=True)
    try:
        loop = adapter._engine
        argv = loop._build_argv()
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["attribution"] == cli_attribution(enabled)
        # CLI renders commit/pr prompt instructions itself from these settings.
        prompt = (loop.system or "") + (loop.append_system_prompt or "")
        assert "# Attribution" not in prompt
        assert COMMIT_TRAILER not in prompt and PR_LINE not in prompt
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_compiler_failure_keeps_attribution(tmp_path, capture_engines, monkeypatch, enabled):
    commit_attribution.write(enabled)
    monkeypatch.setattr(ha.HarnessAdapter, "_compile_prompt_sections", lambda *a, **kw: None)
    adapter = await connect(tmp_path)
    try:
        assert adapter._engine.kwargs["system"].count(prompt_block(enabled)) == 1
    finally:
        await adapter.disconnect()


def test_prompt_delta_is_short_and_fixed():
    for enabled in (True, False):
        block = prompt_block(enabled)
        assert block == prompt_block(enabled)
        assert estimate_tokens_of_text(block) < 80


def test_database_read_failure_defaults_on_with_safe_warning(monkeypatch, caplog):
    def fail(key):
        assert key == commit_attribution.SETTING_KEY
        raise RuntimeError("sensitive database connection details\nsecond line")

    monkeypatch.setattr(database, "get_system_setting", fail)
    with caplog.at_level(logging.WARNING, logger=commit_attribution.__name__):
        assert commit_attribution.read() is True

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()
    assert "defaulting to enabled" in record.getMessage()
    assert "sensitive" not in caplog.text
    assert "second line" not in caplog.text
    assert record.exc_info is None
    assert "\n" not in record.getMessage()


@pytest.mark.parametrize("value", ["", "TRUE", "FALSE", "invalid\nvalue", "x" * 200 + "hidden-tail"])
def test_malformed_setting_defaults_on_with_bounded_warning(value, caplog):
    database.set_system_setting(commit_attribution.SETTING_KEY, value)
    with caplog.at_level(logging.WARNING, logger=commit_attribution.__name__):
        assert commit_attribution.read() is True

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert repr(value[:80]) in message
    assert "defaulting to enabled" in message
    assert "\n" not in message
    if len(value) > 80:
        assert "..." in message
        assert value not in message
        assert "hidden-tail" not in message
        assert len(message) < 180


@pytest.mark.parametrize("value, enabled", [(None, True), ("true", True), ("false", False)])
def test_recognized_or_missing_setting_is_quiet(value, enabled, monkeypatch, caplog):
    monkeypatch.setattr(database, "get_system_setting", lambda key: value)
    with caplog.at_level(logging.WARNING, logger=commit_attribution.__name__):
        assert commit_attribution.read() is enabled
    assert not caplog.records


def test_setting_defaults_on_and_is_network_shared():
    from app.network.policy import is_shared_setting_key

    assert commit_attribution.read() is True
    assert is_shared_setting_key(commit_attribution.SETTING_KEY)
