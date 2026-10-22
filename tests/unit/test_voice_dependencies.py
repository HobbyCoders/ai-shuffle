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


@pytest.mark.parametrize("usable", [False, True])
def test_pip_success_is_complete_only_after_native_imports_work(monkeypatch, tmp_path, usable):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    (tmp_path / "requirements-voice.lock").write_text("synthetic")
    run = Mock(side_effect=[Mock(returncode=0), Mock(returncode=0 if usable else 1)])
    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.install(tmp_path) is usable
    assert run.call_args_list[0].kwargs["timeout"] == 180
    assert 0 < run.call_args_list[1].kwargs["timeout"] <= 180
    assert run.call_args_list[1].args[0][-1] == "import aiortc, av"


@pytest.mark.asyncio
async def test_failed_optional_install_is_retryable_on_next_call(monkeypatch):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    state = {"ready": False, "attempts": 0}
    monkeypatch.setattr(deps, "availability", lambda: (state["ready"], None if state["ready"] else deps.UNAVAILABLE))

    def install(root):
        state["attempts"] += 1
        state["ready"] = state["attempts"] == 2
        return state["ready"]

    monkeypatch.setattr(deps, "install", install)
    assert await deps.ensure_available() == (False, deps.UNAVAILABLE)
    assert await deps.ensure_available() == (True, None)
    assert await deps.ensure_available() == (True, None)
    assert state["attempts"] == 2


@pytest.mark.asyncio
async def test_no_duplicate_installer_is_queued_behind_an_active_attempt(monkeypatch):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    monkeypatch.setattr(deps, "availability", lambda: (False, deps.UNAVAILABLE))
    install = Mock(side_effect=AssertionError("must not start a second installer"))
    monkeypatch.setattr(deps, "install", install)
    deps._INSTALL_LOCK.acquire()
    try:
        assert await deps.ensure_available() == (False, deps.UNAVAILABLE)
    finally:
        deps._INSTALL_LOCK.release()
    install.assert_not_called()


def test_optional_cli_reports_failure_without_running_real_pip(monkeypatch):
    import runpy
    import sys
    monkeypatch.setattr(sys, "argv", [deps.__file__, "--install"])
    run = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(deps.subprocess, "run", run)
    monkeypatch.setattr(deps.platform, "system", lambda: "Linux")
    monkeypatch.setattr(deps.platform, "libc_ver", lambda: ("glibc", "2.28"))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(deps.__file__, run_name="__main__")
    assert error.value.code == 1
    run.assert_called_once()


def test_native_probe_cannot_extend_the_install_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "platform_reason", lambda: None)
    (tmp_path / "requirements-voice.lock").write_text("synthetic")
    monkeypatch.setattr(deps, "time", Mock(monotonic=Mock(side_effect=[100.0, 280.0])))
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.install(tmp_path) is False
    run.assert_called_once()


@pytest.mark.asyncio
async def test_unsupported_system_never_retries_install_on_call_start(monkeypatch):
    monkeypatch.setattr(deps, "availability", lambda: (False, deps.UNAVAILABLE))
    monkeypatch.setattr(deps, "platform_reason", lambda: deps.UNAVAILABLE)
    install = Mock(side_effect=AssertionError("unsupported system"))
    monkeypatch.setattr(deps, "install", install)
    assert await deps.ensure_available() == (False, deps.UNAVAILABLE)
    install.assert_not_called()
