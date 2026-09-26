"""
Unit tests for the CLI bridge module's PTY availability check.

``PtyProcess`` itself is exercised through ``claude_cli_login``.
"""

import sys

import pytest

from app.core.cli_bridge import PTY_AVAILABLE, is_pty_available


class TestIsPtyAvailable:
    """Test PTY availability checking."""

    def test_is_pty_available_returns_bool(self):
        """is_pty_available should return a boolean."""
        result = is_pty_available()
        assert isinstance(result, bool)

    def test_is_pty_available_matches_constant(self):
        """is_pty_available should match PTY_AVAILABLE constant."""
        assert is_pty_available() == PTY_AVAILABLE

    @pytest.mark.skipif(sys.platform != 'win32', reason="Windows-specific test")
    def test_pty_not_available_on_windows(self):
        """PTY should not be available on Windows."""
        assert is_pty_available() is False
