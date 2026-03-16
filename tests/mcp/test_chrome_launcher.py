"""
Tests for Chrome launcher — finding Chrome, launching processes,
port allocation, and process management.
"""

import asyncio
import sys
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from app.mcp.chrome_launcher import (
    find_chrome,
    PortAllocator,
    DisplayAllocator,
    LaunchError,
    _chrome_base_args,
    wait_for_cdp_ready,
    kill_process_tree,
)


# ============================================================================
# find_chrome tests
# ============================================================================

class TestFindChrome:
    def test_find_chrome_returns_path_or_none(self):
        """find_chrome() should return a string path or None."""
        result = find_chrome()
        assert result is None or isinstance(result, str)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_find_chrome_windows(self):
        result = find_chrome()
        if result:
            assert "chrome" in result.lower()

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux/Mac only")
    def test_find_chrome_unix(self):
        result = find_chrome()
        if result:
            assert "chrom" in result.lower()


# ============================================================================
# Port allocation tests
# ============================================================================

class TestPortAllocator:
    @pytest.mark.asyncio
    async def test_allocate_sequential(self):
        alloc = PortAllocator(9000, 9005)
        ports = []
        for _ in range(6):
            ports.append(await alloc.allocate())
        assert ports == [9000, 9001, 9002, 9003, 9004, 9005]

    @pytest.mark.asyncio
    async def test_allocate_exhaustion(self):
        alloc = PortAllocator(9000, 9002)
        await alloc.allocate()
        await alloc.allocate()
        await alloc.allocate()
        with pytest.raises(LaunchError, match="No ports available"):
            await alloc.allocate()

    @pytest.mark.asyncio
    async def test_release_and_reuse(self):
        alloc = PortAllocator(9000, 9001)
        p1 = await alloc.allocate()
        p2 = await alloc.allocate()

        # Exhausted
        with pytest.raises(LaunchError):
            await alloc.allocate()

        # Release one
        await alloc.release(p1)

        # Can allocate again
        p3 = await alloc.allocate()
        assert p3 == p1

    @pytest.mark.asyncio
    async def test_used_count(self):
        alloc = PortAllocator(9000, 9005)
        assert alloc.used_count == 0
        await alloc.allocate()
        assert alloc.used_count == 1
        await alloc.allocate()
        assert alloc.used_count == 2

    @pytest.mark.asyncio
    async def test_release_unused_port(self):
        """Releasing an unallocated port should not error."""
        alloc = PortAllocator(9000, 9005)
        await alloc.release(8888)  # Never allocated
        assert alloc.used_count == 0


class TestDisplayAllocator:
    @pytest.mark.asyncio
    async def test_allocate_display(self):
        alloc = DisplayAllocator(100, 102)
        d1 = await alloc.allocate()
        d2 = await alloc.allocate()
        assert d1 == 100
        assert d2 == 101

    @pytest.mark.asyncio
    async def test_display_exhaustion(self):
        alloc = DisplayAllocator(100, 100)
        await alloc.allocate()
        with pytest.raises(LaunchError):
            await alloc.allocate()


# ============================================================================
# Chrome args tests
# ============================================================================

class TestChromeArgs:
    def test_base_args_contain_required_flags(self):
        args = _chrome_base_args("/tmp/profile", 9222)
        assert "--remote-debugging-port=9222" in args
        assert "--user-data-dir=/tmp/profile" in args
        assert "--no-first-run" in args
        assert "about:blank" in args

    def test_base_args_port_varies(self):
        args1 = _chrome_base_args("/tmp/p1", 9222)
        args2 = _chrome_base_args("/tmp/p2", 9333)
        assert "--remote-debugging-port=9222" in args1
        assert "--remote-debugging-port=9333" in args2


# ============================================================================
# Process management tests
# ============================================================================

class TestKillProcessTree:
    def test_kill_already_dead(self):
        """kill_process_tree should handle already-exited processes."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # Already exited
        kill_process_tree(mock_proc)  # Should not raise

    def test_kill_live_process(self):
        """kill_process_tree should attempt to kill live processes."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        mock_proc.pid = 12345

        with patch("subprocess.run"):
            kill_process_tree(mock_proc)


# ============================================================================
# Integration: Chrome launch + CDP ready (skipped if no Chrome)
# ============================================================================

@pytest.mark.asyncio
async def test_chrome_launches_and_cdp_ready():
    """Integration: Launch headless Chrome and verify CDP is reachable."""
    from app.mcp.chrome_launcher import find_chrome, launch_chrome_headless, kill_process_tree
    import tempfile

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found")

    tmpdir = tempfile.mkdtemp(prefix="chrome_test_")
    port = 9334

    proc = await launch_chrome_headless(
        chrome_path=chrome_path,
        user_data_dir=tmpdir,
        cdp_port=port,
    )

    try:
        ready = await wait_for_cdp_ready(port=port, timeout=15.0)
        assert ready, "Chrome CDP should be reachable after launch"
    finally:
        kill_process_tree(proc)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
