"""Tests for Claude-in-Chrome rebind after an account switch.

Covers the pure stale-pairing clear (``_clear_stale_pairing``), the eligibility
gating + notification of the public entrypoint, and the failover wiring that
invokes it. The browser's actual claude.ai re-sign-in can only be verified live
(needs a real second account + interactive extension sign-in) — see the module
docstring; these tests pin the automatable surface.
"""

import json
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import chrome_rebind as cr


# ---------------------------------------------------------------------------
# _clear_stale_pairing
# ---------------------------------------------------------------------------
class TestClearStalePairing:
    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data))

    def test_removes_pin_and_returns_true(self, tmp_path):
        cj = tmp_path / ".claude.json"
        self._write(cj, {
            "userID": "u1",
            "chromeExtension": {"pairedDeviceId": "dev-A", "pairedDeviceName": "Browser 2"},
        })
        with patch.object(cr, "_CLAUDE_JSON", cj):
            assert cr._clear_stale_pairing() is True
        data = json.loads(cj.read_text())
        assert "chromeExtension" not in data
        assert data["userID"] == "u1"  # unrelated keys preserved

    def test_no_pin_returns_false_and_untouched(self, tmp_path):
        cj = tmp_path / ".claude.json"
        self._write(cj, {"userID": "u1"})
        with patch.object(cr, "_CLAUDE_JSON", cj):
            assert cr._clear_stale_pairing() is False
        assert json.loads(cj.read_text()) == {"userID": "u1"}

    def test_empty_chrome_ext_returns_false(self, tmp_path):
        cj = tmp_path / ".claude.json"
        self._write(cj, {"chromeExtension": {"somethingElse": 1}})
        with patch.object(cr, "_CLAUDE_JSON", cj):
            assert cr._clear_stale_pairing() is False

    def test_missing_file_returns_false(self, tmp_path):
        with patch.object(cr, "_CLAUDE_JSON", tmp_path / "nope.json"):
            assert cr._clear_stale_pairing() is False

    def test_malformed_json_returns_false(self, tmp_path):
        cj = tmp_path / ".claude.json"
        cj.write_text("{ not json")
        with patch.object(cr, "_CLAUDE_JSON", cj):
            assert cr._clear_stale_pairing() is False


# ---------------------------------------------------------------------------
# _restart_supervised_chrome — exact-arg process matching
# ---------------------------------------------------------------------------
class TestRestartSupervisedChrome:
    def _proc(self, tmp_path, pid: str, argv: list[str]) -> None:
        d = tmp_path / pid
        d.mkdir()
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")

    def _run(self, tmp_path, monkeypatch, profile: str):
        # The product only ever runs this on the Linux container, where
        # ``_PROFILE_DIR`` is a POSIX path and ``str(_PROFILE_DIR)`` (used to
        # build the ``--user-data-dir=<profile>`` match) keeps forward slashes.
        # Use PurePosixPath so the match string matches the POSIX cmdline we
        # write into the fake /proc tree even when the test runs on Windows,
        # where a plain Path would stringify with backslashes and never match.
        monkeypatch.setattr(cr, "_PROFILE_DIR", PurePosixPath(profile))
        pids = [p.name for p in tmp_path.iterdir()]  # snapshot before patching listdir
        monkeypatch.setattr(cr.os, "listdir", lambda p: pids)
        real_open = open

        def fake_open(path, *a, **k):
            # Redirect /proc/<pid>/cmdline reads to our fake tree.
            p = str(path)
            if p.startswith("/proc/"):
                return real_open(str(tmp_path / p[len("/proc/"):]), *a, **k)
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", fake_open)
        killed: list[int] = []
        monkeypatch.setattr(cr.os, "kill", lambda pid, sig: killed.append(pid))
        result = cr._restart_supervised_chrome()
        return result, killed

    def test_signals_only_our_profile_main_process(self, tmp_path, monkeypatch):
        prof = "/home/appuser/chrome-profile"
        self._proc(tmp_path, "100", ["/usr/bin/chrome", f"--user-data-dir={prof}"])
        self._proc(tmp_path, "101", ["/usr/bin/chrome", f"--user-data-dir={prof}", "--type=renderer"])
        self._proc(tmp_path, "102", ["bash", "loop"])
        result, killed = self._run(tmp_path, monkeypatch, prof)
        assert result is True
        assert killed == [100]  # main only, renderer (--type=) skipped

    def test_does_not_signal_sibling_prefix_profile(self, tmp_path, monkeypatch):
        # A different Chrome whose profile path merely starts with ours.
        self._proc(tmp_path, "200", ["/usr/bin/chrome", "--user-data-dir=/home/appuser/chrome-profile-2"])
        result, killed = self._run(tmp_path, monkeypatch, "/home/appuser/chrome-profile")
        assert result is False
        assert killed == []


# ---------------------------------------------------------------------------
# rebind_browser_after_account_switch — gating + notification
# ---------------------------------------------------------------------------
class TestRebindEntrypoint:
    async def test_noop_when_not_container(self):
        with patch.object(cr, "_is_container", return_value=False), \
             patch.object(cr, "_clear_stale_pairing") as clr:
            await cr.rebind_browser_after_account_switch(
                session_id="s", api_user_id=None, new_account_name="B")
        clr.assert_not_called()

    async def test_noop_when_chrome_disabled(self):
        with patch.object(cr, "_is_container", return_value=True), \
             patch.object(cr, "_chrome_enabled", return_value=False), \
             patch.object(cr, "_clear_stale_pairing") as clr:
            await cr.rebind_browser_after_account_switch(
                session_id="s", api_user_id=None, new_account_name="B")
        clr.assert_not_called()

    async def test_noop_when_never_set_up(self):
        # Enabled container but no pin and no live bridge → nothing to rebind.
        with patch.object(cr, "_is_container", return_value=True), \
             patch.object(cr, "_chrome_enabled", return_value=True), \
             patch.object(cr, "_clear_stale_pairing", return_value=False), \
             patch.object(cr, "_bridge_active", return_value=False), \
             patch("app.services.notification_service.emit_notification",
                   new=AsyncMock()) as emit:
            await cr.rebind_browser_after_account_switch(
                session_id="s", api_user_id="u", new_account_name="B")
        emit.assert_not_awaited()

    async def test_clears_pin_and_notifies_when_pinned(self):
        emit = AsyncMock()
        with patch.object(cr, "_is_container", return_value=True), \
             patch.object(cr, "_chrome_enabled", return_value=True), \
             patch.object(cr, "_clear_stale_pairing", return_value=True) as clr, \
             patch.object(cr, "_bridge_active", return_value=False), \
             patch.object(cr, "_restart_supervised_chrome") as restart, \
             patch("app.services.notification_service.emit_notification", new=emit):
            await cr.rebind_browser_after_account_switch(
                session_id="sess-9", api_user_id="user-1", new_account_name="Work")
        clr.assert_called_once()
        restart.assert_not_called()  # restart is env-gated, off by default
        emit.assert_awaited_once()
        kwargs = emit.await_args.kwargs
        assert kwargs["category"] == "chrome_rebind"
        assert kwargs["session_id"] == "sess-9"
        assert "Work" in kwargs["message"]
        assert emit.await_args.args[0] == "user-1"

    async def test_notifies_when_bridge_active_even_without_pin(self):
        emit = AsyncMock()
        with patch.object(cr, "_is_container", return_value=True), \
             patch.object(cr, "_chrome_enabled", return_value=True), \
             patch.object(cr, "_clear_stale_pairing", return_value=False), \
             patch.object(cr, "_bridge_active", return_value=True), \
             patch("app.services.notification_service.emit_notification", new=emit):
            await cr.rebind_browser_after_account_switch(
                session_id=None, api_user_id=None, new_account_name=None)
        emit.assert_awaited_once()
        # Falls back to the "admin" recipient when api_user_id is None.
        assert emit.await_args.args[0] == "admin"

    async def test_restart_invoked_when_env_set(self, monkeypatch):
        monkeypatch.setenv("CHROME_REBIND_RESTART", "1")
        with patch.object(cr, "_is_container", return_value=True), \
             patch.object(cr, "_chrome_enabled", return_value=True), \
             patch.object(cr, "_clear_stale_pairing", return_value=True), \
             patch.object(cr, "_bridge_active", return_value=False), \
             patch.object(cr, "_restart_supervised_chrome") as restart, \
             patch("app.services.notification_service.emit_notification",
                   new=AsyncMock()):
            await cr.rebind_browser_after_account_switch(
                session_id="s", api_user_id=None, new_account_name="B")
        restart.assert_called_once()

    async def test_never_raises_on_internal_error(self):
        # A blowup inside the guarded body must be swallowed.
        with patch.object(cr, "_is_container", side_effect=RuntimeError("boom")):
            await cr.rebind_browser_after_account_switch(
                session_id="s", api_user_id=None, new_account_name="B")
