"""Fresh-process ratchets for SDK-free connector discovery and empty startup."""
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SDK_ROOTS = ("aiogram", "discord", "slack_sdk", "googleapiclient", "google.oauth2", "asyncssh")


def run_fresh(tmp_path, source, **extra_env):
    from app.preview_run import preview_environment
    # Isolate plugin/credential homes too, not just DATA_DIR. A child must not
    # discover the launching worker's installed connector packages.
    env = preview_environment(tmp_path, 18972, 18973)
    env.update(TESTING="1", AI_SHUFFLE_PREVIEW="0", AI_SHUFFLE_PERF_LAZY_CONNECTORS="1")
    env.update(extra_env)
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=tmp_path,
                            env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_import_discovery_and_empty_connector_start_do_not_import_sdks(tmp_path):
    run_fresh(tmp_path, f"""
        import asyncio
        import sys
        from unittest.mock import patch
        import app.main
        from app.api.connectors import list_connector_types
        from app.core.connectors import get_available_types
        from app.core.connectors.manager import ConnectorManager
        from app.mcp.connector_gateway_sdk import get_gateway_tool_names

        async def exercise():
            types = await list_connector_types(token="test")
            from app.core import connector_packages
            expected = {{kind for kind in get_available_types() if connector_packages.is_offered(kind)}}
            assert {{row['type'] for row in types}} == expected
            assert all({{'capabilities', 'triggers', 'actions', 'tools'}} <= row.keys() for row in types)
            telegram = next(row for row in types if row['type'] == 'telegram')
            assert telegram['capabilities']['buttons']
            assert any(action['id'] == 'send_message' for action in telegram['actions'])
            assert 'mcp__builtins__telegram_invoke' in get_gateway_tool_names()
            with patch('app.db.database.get_active_connectors', return_value=[]):
                await ConnectorManager().start_all()
        asyncio.run(exercise())
        roots = {SDK_ROOTS!r}
        loaded = sorted(name for name in sys.modules
                        if any(name == root or name.startswith(root + '.') for root in roots))
        assert not loaded, f'Unused connector SDKs imported: {{loaded}}'
    """)


def test_resolving_telegram_does_not_import_other_connectors(tmp_path):
    run_fresh(tmp_path, """
        import sys
        from app.core.connectors import get_connector_class
        cls = get_connector_class('telegram')
        assert cls.__name__ == 'TelegramConnector'
        from app.core.connectors import TelegramConnector
        assert TelegramConnector is cls
        assert 'app.core.connectors.discord_connector' not in sys.modules
        assert 'app.core.connectors.gmail' not in sys.modules
        assert 'aiogram' not in sys.modules
    """)


def test_starting_telegram_imports_sdk_and_polls(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        import sys
        from unittest.mock import AsyncMock, patch
        from app.core.connectors import get_connector_class
        from app.core.connectors.base import ConnectorStatus
        cls = get_connector_class('telegram')
        assert 'aiogram' not in sys.modules
        async def exercise():
            connector = cls('test', 'telegram', {'bot_token': '123456:TEST'}, AsyncMock(), AsyncMock())
            # connect() must itself load and construct the real SDK objects.
            # It schedules polling without yielding, so intercept the network
            # only after connect has returned (no patch imports aiogram first).
            assert await connector.connect()
            assert connector.status == ConnectorStatus.CONNECTED
            assert connector._bot.__class__.__module__.startswith('aiogram.')
            with patch.object(connector._dp, 'start_polling', new_callable=AsyncMock) as poll:
                await asyncio.sleep(0)
                poll.assert_awaited_once_with(connector._bot, handle_as_tasks=False)
                await connector.disconnect()
        asyncio.run(exercise())
        assert 'aiogram' in sys.modules
        assert 'discord' not in sys.modules
    """)



def test_type_names_are_declarative_without_resolving_implementations(tmp_path):
    run_fresh(tmp_path, """
        import sys
        from app.core.connector_packages import CATALOG
        from app.core.connectors import get_available_types
        assert get_available_types() == list(CATALOG['packages'])
        assert not any('app.core.connectors.' + spec['module'] in sys.modules
                       for spec in CATALOG['packages'].values())
    """)


def test_eager_rollback_switch(tmp_path):
    run_fresh(tmp_path, """
        import sys
        import app.core.connectors
        assert 'aiogram' in sys.modules
        assert 'app.core.connectors.gmail' in sys.modules
    """, AI_SHUFFLE_PERF_LAZY_CONNECTORS="0")


def test_registry_overrides_and_unknown_type(monkeypatch):
    import pytest
    from app.core import connector_packages
    from app.core.connectors import registry
    from app.core.connectors.base import BaseConnector

    monkeypatch.setattr(registry, '_CONNECTOR_CLASSES', {})
    monkeypatch.setattr(connector_packages, 'runtime_class', lambda kind: None)
    registry.register_connector('custom', BaseConnector)
    assert registry.get_available_types()[-1] == 'custom'
    assert registry.get_connector_class('custom') is BaseConnector
    registry.register_connector('telegram', BaseConnector)
    assert registry.get_connector_class('telegram') is BaseConnector
    with pytest.raises(ValueError, match='Unknown connector type: missing'):
        registry.get_connector_class('missing')


def test_installed_package_precedes_bundled_and_disabled_never_falls_back(monkeypatch):
    import pytest
    from app.core import connector_packages
    from app.core.connectors import registry
    from app.core.connectors.base import BaseConnector

    monkeypatch.setattr(registry, '_CONNECTOR_CLASSES', {})
    def no_import(name):
        raise AssertionError(f'Bundled implementation unexpectedly loaded: {name}')
    monkeypatch.setattr(registry, 'import_module', no_import)
    monkeypatch.setattr(connector_packages, 'runtime_class', lambda kind: BaseConnector)
    assert registry.get_connector_class('telegram') is BaseConnector
    assert not registry._CONNECTOR_CLASSES

    def disabled(kind):
        raise ValueError('Connector plugin is disabled')
    monkeypatch.setattr(connector_packages, 'runtime_class', disabled)
    with pytest.raises(ValueError, match='Connector plugin is disabled'):
        registry.get_connector_class('telegram')
