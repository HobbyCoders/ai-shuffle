"""The app's kernel config carries the OS sandbox mode from settings."""
from app.core import config
from app.core.provider import harness_adapter


def test_kernel_config_takes_os_sandbox_from_settings(monkeypatch):
    monkeypatch.setattr(config.settings, "code_mode_os_sandbox", "required")
    assert harness_adapter._codemode_kernel_config().os_sandbox == "required"


def test_kernel_config_default_is_auto(monkeypatch):
    monkeypatch.setattr(config.settings, "code_mode_os_sandbox", "auto")
    assert harness_adapter._codemode_kernel_config().os_sandbox == "auto"
