"""Auto is a delegated execution capability, not a native permission policy."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.provider.capabilities import supports_auto_permission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions


@pytest.mark.parametrize('provider_type,auth_mode,supported', [
    ('anthropic', 'subscription', True),
    ('anthropic', 'api_key', False),
    ('openrouter', 'api_key', False),
    ('chatgpt', 'subscription', False),
    ('custom', 'api_key', False),
])
def test_auto_uses_execution_route(provider_type, auth_mode, supported, monkeypatch):
    monkeypatch.delenv('AIH_FLUID_PROVIDER', raising=False)
    monkeypatch.delenv('AIH_OMNI_PROVIDER', raising=False)
    assert supports_auto_permission('claude-sonnet-4-5', {
        'id': 'provider', 'provider_type': provider_type, 'auth_mode': auth_mode,
    }) is supported


@pytest.mark.asyncio
@pytest.mark.parametrize('delegated,expected,engine_mode', [
    # The native engine is sent the policy name ('default' asks on writes);
    # the Claude CLI is sent the mode name it implements itself.
    (False, 'default', 'on_write'),
    (True, 'auto', 'auto'),
])
async def test_legacy_auto_change_normalizes_only_native_routes(delegated, expected, engine_mode):
    adapter = HarnessAdapter()
    adapter._is_delegated = delegated
    adapter._engine = SimpleNamespace(submit=AsyncMock())
    adapter._options = SessionOptions(model='sonnet')
    await adapter.set_permission_mode('auto')
    assert adapter._engine.submit.call_args.args[0].mode == engine_mode
    assert adapter._options.permission_mode == expected
