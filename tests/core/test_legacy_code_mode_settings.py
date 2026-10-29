"""Retired sandbox settings must not prevent an existing worker from starting."""

import pytest

from app.core.config import Settings


@pytest.mark.parametrize("value", ["auto", "required", "off", "legacy-unknown"])
def test_retired_code_mode_os_sandbox_env_and_stored_field_are_ignored(monkeypatch, tmp_path, value):
    # Cover process env, an old .env file, and a serialized settings input.
    monkeypatch.setenv("CODE_MODE_OS_SANDBOX", value)
    env_file = tmp_path / "legacy.env"
    env_file.write_text(f"CODE_MODE_OS_SANDBOX={value}\n", encoding="utf-8")
    settings = Settings(_env_file=env_file, code_mode_os_sandbox=value)
    assert "code_mode_os_sandbox" not in type(settings).model_fields
    assert not hasattr(settings, "code_mode_os_sandbox")
