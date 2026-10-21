"""Optional native voice packages never gate app installation/startup."""
import importlib
from unittest.mock import Mock

import pytest

from app.core import voice_dependencies as deps


@pytest.mark.parametrize("failure", [ImportError("private-path"), OSError("private-path")])
def test_native_import_failure_is_sanitized(monkeypatch, failure):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    def missing(name):
        raise failure
    monkeypatch.setattr(importlib, "import_module", missing)
    available, reason = deps.availability()
    assert available is False
    assert "not available on this system" in reason
    assert "private-path" not in reason
    with pytest.raises(ValueError, match="not available on this system"):
        deps.load_native()


def test_unrelated_voice_details_never_load_native(monkeypatch):
    from app.core.voice_models import model_details
    native = Mock(side_effect=AssertionError("must stay lazy"))
    monkeypatch.setattr(deps, "availability", native)
    model_details("gpt-live-1")
    model_details("grok-voice-latest")
    native.assert_not_called()


def test_optional_details_explain_unavailability(monkeypatch):
    from app.core.voice_models import model_details
    monkeypatch.setattr(deps, "availability", lambda: (False, "not available on this system"))
    details = model_details("gpt-live-1-codex")
    assert details["available"] is False
    assert details["unavailable_reason"] == "not available on this system"


@pytest.mark.parametrize("system,version", [("Darwin", "13.7"), ("Linux", "2.27")])
def test_older_system_does_not_attempt_import(monkeypatch, system, version):
    monkeypatch.setattr(deps.platform, "system", lambda: system)
    monkeypatch.setattr(deps.platform, "mac_ver", lambda: (version, (), ""))
    monkeypatch.setattr(deps.platform, "libc_ver", lambda: ("glibc", version))
    native = Mock(side_effect=AssertionError("must not import"))
    monkeypatch.setattr(importlib, "import_module", native)
    assert deps.availability()[0] is False
    native.assert_not_called()


def test_optional_install_failure_does_not_fail_app_install(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    (tmp_path / "requirements-voice.lock").write_text("synthetic")
    run = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.install(tmp_path) is False
    assert "not available on this system" in capsys.readouterr().out
    assert "--require-hashes" in run.call_args.args[0]
    assert "--only-binary=:all:" in run.call_args.args[0]


def test_unsupported_install_never_runs_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "platform_reason", lambda: "not available on this system")
    run = Mock(side_effect=AssertionError("no pip"))
    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.install(tmp_path) is False
    run.assert_not_called()
