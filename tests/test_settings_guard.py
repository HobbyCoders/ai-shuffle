"""Exercise the teardown plugin in fresh pytest processes, not just its helper."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("body, leaked", [
    ("""
        def test_restored_patch(monkeypatch):
            monkeypatch.setattr(config, "settings", object())
    """, None),
    ("""
        def test_patched_fields(monkeypatch):
            monkeypatch.setattr(config.settings, "instance_id", "temporary")
            import app.core.event_gateway
    """, None),
    ("""
        def test_child_module_is_not_a_settings_binding():
            import app.db.settings
    """, None),
    ("""
        def test_late_import(monkeypatch):
            monkeypatch.setattr(config, "settings", object())
            import app.core.event_gateway
    """, "app.core.event_gateway"),
    ("""
        def test_unrestored_source():
            config.settings = object()
    """, "app.core.config"),
    ("""
        def test_module_fake_is_not_a_child_module():
            from types import ModuleType
            config.settings = ModuleType("fake_settings")
    """, "app.core.config"),
    ("""
        import pytest

        @pytest.fixture
        def late_finalizer(monkeypatch):
            monkeypatch.setattr(config, "settings", object())
            yield
            import app.core.event_gateway

        def test_finalizer(late_finalizer):
            pass
    """, "app.core.event_gateway"),
])
def test_settings_guard_after_all_finalizers(tmp_path, body, leaked):
    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "test_probe.py"
    probe.write_text("from app.core import config\n" + textwrap.dedent(body), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "fluid")]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "DATA_DIR": str(tmp_path / "data"),
    }
    # pytest 9.2 captures warnings from -p imports, including Settings' legacy
    # Pydantic config. Suppress only that deprecation, not other child warnings.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.settings_guard",
         "-W", "ignore:^Support for class-based `config` is deprecated, use ConfigDict instead[.]"
         ":pydantic.warnings.PydanticDeprecatedSince20",
         "--confcutdir=" + str(tmp_path), str(probe)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert "PydanticDeprecatedSince20" not in output
    if leaked:
        assert result.returncode == 1, output
        assert "Module settings leaked after test_probe.py::" in output
        assert leaked in output
        assert "1 passed, 1 error" in output
    else:
        assert result.returncode == 0, output
        assert "1 passed" in output
