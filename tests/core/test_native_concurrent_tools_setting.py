"""The app's native loop honours fluid's ``max_concurrent_tools`` setting.

The app builds ``NativeLoop`` itself, so fluid's own config loader never runs;
the app setting (env ``FLUID_MAX_CONCURRENT_TOOLS``, as for the fluid CLI) is
what reaches the loop. Default 1: no early fetch.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, settings
from app.core.provider.types import SessionOptions


class _CapturingLoop:
    kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        from fluid.tools.registry import ToolCtx

        _CapturingLoop.kwargs = kwargs
        self.ctx = kwargs.get("ctx") or ToolCtx()
        self.agents = list(kwargs.get("agents") or ())

    async def _ensure_session(self):
        class _Meta:
            session_id = "ses_x"

        return _Meta()

    async def aclose(self):
        pass


async def _fake_provider_client(kind, options):
    return object(), None


async def _loop_kwargs(monkeypatch, tmp_path) -> dict:
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    _CapturingLoop.kwargs = {}
    await ha.HarnessAdapter().connect(SessionOptions(
        model="gpt-5", cwd=str(tmp_path), extra={"fluid_provider": "openai_responses"},
    ))
    return _CapturingLoop.kwargs


def test_the_setting_defaults_to_one_and_reads_the_fluid_env(monkeypatch):
    assert Settings.model_fields["fluid_max_concurrent_tools"].default == 1
    monkeypatch.setenv("FLUID_MAX_CONCURRENT_TOOLS", "4")
    assert Settings().fluid_max_concurrent_tools == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [(1, 1), (4, 4), (0, 1)])
async def test_native_loop_gets_the_setting(monkeypatch, tmp_path, value, expected):
    monkeypatch.setattr(settings, "fluid_max_concurrent_tools", value)
    assert (await _loop_kwargs(monkeypatch, tmp_path))["max_concurrent_tools"] == expected
