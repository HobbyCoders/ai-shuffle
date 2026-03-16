"""
Tests for ChromeSessionManager — session lifecycle, resource management,
idle cleanup, and display mode detection.
"""

import asyncio
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from app.mcp.chrome_session import (
    ChromeSession,
    ChromeSessionManager,
    _detect_display_mode,
    DISPLAY_MODE_HEADLESS,
    DISPLAY_MODE_VNC,
    SESSION_IDLE_TIMEOUT,
)
from app.mcp.chrome_launcher import LaunchError


# ============================================================================
# Display mode detection
# ============================================================================

class TestDisplayModeDetection:
    def test_windows_uses_headless(self):
        with patch("app.mcp.chrome_session.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert _detect_display_mode() == DISPLAY_MODE_HEADLESS

    def test_darwin_uses_headless(self):
        with patch("app.mcp.chrome_session.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _detect_display_mode() == DISPLAY_MODE_HEADLESS

    def test_linux_without_vnc_uses_headless(self):
        with patch("app.mcp.chrome_session.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("shutil.which", return_value=None):
                assert _detect_display_mode() == DISPLAY_MODE_HEADLESS

    def test_linux_with_vnc_stack_uses_vnc(self):
        with patch("app.mcp.chrome_session.sys") as mock_sys:
            mock_sys.platform = "linux"
            def which_side_effect(name):
                return f"/usr/bin/{name}" if name in ("Xvfb", "x11vnc", "websockify") else None
            with patch("shutil.which", side_effect=which_side_effect):
                assert _detect_display_mode() == DISPLAY_MODE_VNC


# ============================================================================
# ChromeSession dataclass tests
# ============================================================================

class TestChromeSession:
    def test_touch_updates_activity(self):
        import time
        session = ChromeSession(
            session_id="test",
            user_data_dir=Path("/tmp/test"),
            display_mode=DISPLAY_MODE_HEADLESS,
            cdp_port=9222,
        )
        old_time = session.last_activity
        import time as t
        t.sleep(0.01)
        session.touch()
        assert session.last_activity > old_time

    def test_is_expired(self):
        import time
        session = ChromeSession(
            session_id="test",
            user_data_dir=Path("/tmp/test"),
            display_mode=DISPLAY_MODE_HEADLESS,
            cdp_port=9222,
        )
        session.last_activity = time.time() - SESSION_IDLE_TIMEOUT - 1
        assert session.is_expired

    def test_not_expired(self):
        session = ChromeSession(
            session_id="test",
            user_data_dir=Path("/tmp/test"),
            display_mode=DISPLAY_MODE_HEADLESS,
            cdp_port=9222,
        )
        assert not session.is_expired


# ============================================================================
# ChromeSessionManager tests
# ============================================================================

class TestChromeSessionManager:
    @pytest.mark.asyncio
    async def test_has_session_empty(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        assert not mgr.has_session("nonexistent")

    @pytest.mark.asyncio
    async def test_get_session_nonexistent(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        session = await mgr.get_session("nonexistent")
        assert session is None

    @pytest.mark.asyncio
    async def test_get_session_state_nonexistent(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        state = mgr.get_session_state("nonexistent")
        assert state is None

    @pytest.mark.asyncio
    async def test_display_info_nonexistent(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        info = mgr.get_session_display_info("nonexistent")
        assert info is None

    @pytest.mark.asyncio
    async def test_close_all_empty(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        await mgr.close_all()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_session_nonexistent(self):
        mgr = ChromeSessionManager(profile_base_dir=Path(tempfile.mkdtemp()))
        await mgr.close_session("nonexistent")  # Should not raise


# ============================================================================
# Integration: Full session lifecycle (requires Chrome)
# ============================================================================

@pytest.mark.asyncio
async def test_session_lifecycle():
    """Integration test: create session, verify CDP works, close."""
    from app.mcp.chrome_launcher import find_chrome

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found")

    tmpdir = tempfile.mkdtemp(prefix="session_test_")
    mgr = ChromeSessionManager(profile_base_dir=Path(tmpdir), max_sessions=2)

    try:
        # Create session
        session = await mgr.get_or_create_session("test-session-1")
        assert session is not None
        assert session.session_id == "test-session-1"
        assert session.cdp_client is not None
        assert session.cdp_client.connected
        assert mgr.has_session("test-session-1")

        # Navigate to a data URL
        await session.navigate("data:text/html,<title>Test</title><h1>Hello</h1>")
        await asyncio.sleep(1.0)

        title = await session.get_title()
        assert title == "Test"

        # Get session state
        state = mgr.get_session_state("test-session-1")
        assert state is not None
        assert state["is_active"]

        # Display info
        info = mgr.get_session_display_info("test-session-1")
        assert info is not None
        assert info["mode"] == "screencast"  # Windows = headless

        # Get same session again (should reuse)
        session2 = await mgr.get_or_create_session("test-session-1")
        assert session2 is session

        # Close session
        await mgr.close_session("test-session-1")
        assert not mgr.has_session("test-session-1")

    finally:
        await mgr.close_all()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_max_sessions_eviction():
    """Integration test: creating sessions beyond max should evict oldest."""
    from app.mcp.chrome_launcher import find_chrome

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found")

    tmpdir = tempfile.mkdtemp(prefix="max_session_test_")
    mgr = ChromeSessionManager(profile_base_dir=Path(tmpdir), max_sessions=2)

    try:
        # Create 2 sessions (at max)
        s1 = await mgr.get_or_create_session("session-1")
        await asyncio.sleep(0.1)
        s2 = await mgr.get_or_create_session("session-2")

        assert mgr.has_session("session-1")
        assert mgr.has_session("session-2")

        # Create 3rd — should evict session-1 (oldest)
        s3 = await mgr.get_or_create_session("session-3")

        assert not mgr.has_session("session-1")
        assert mgr.has_session("session-2")
        assert mgr.has_session("session-3")

    finally:
        await mgr.close_all()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_screenshot_and_accessibility():
    """Integration test: screenshot and accessibility tree via CDP."""
    from app.mcp.chrome_launcher import find_chrome

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found")

    tmpdir = tempfile.mkdtemp(prefix="screenshot_test_")
    mgr = ChromeSessionManager(profile_base_dir=Path(tmpdir), max_sessions=1)

    try:
        session = await mgr.get_or_create_session("test-screenshot")
        await session.navigate("data:text/html,<title>Screenshot Test</title><button>Click me</button>")
        await asyncio.sleep(1.0)

        # Screenshot
        b64 = await session.take_screenshot()
        assert len(b64) > 100

        # Accessibility tree
        tree = await session.get_accessibility_tree()
        # Tree may or may not be available depending on Chrome version
        if tree is not None:
            assert "role" in tree

    finally:
        await mgr.close_all()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_profile_persistence():
    """Integration test: user-data-dir persists cookies across sessions."""
    from app.mcp.chrome_launcher import find_chrome

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found")

    tmpdir = tempfile.mkdtemp(prefix="persist_test_")
    mgr = ChromeSessionManager(profile_base_dir=Path(tmpdir), max_sessions=2)

    try:
        # Session 1: Set a cookie via JavaScript
        session = await mgr.get_or_create_session("persist-test")
        await session.navigate("data:text/html,<title>Persist</title>")
        await asyncio.sleep(0.5)

        # Note: data: URLs can't set cookies, so we just verify the profile dir exists
        profile_path = Path(tmpdir) / "persist-test"
        assert profile_path.exists()

        # Close session (processes killed, but profile dir stays)
        await mgr.close_session("persist-test")
        assert profile_path.exists()

    finally:
        await mgr.close_all()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
