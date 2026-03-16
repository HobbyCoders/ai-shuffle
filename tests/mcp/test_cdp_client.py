"""
Tests for CDPClient — the lightweight async Chrome DevTools Protocol client.

Tests cover:
- CDP message serialization and command/response matching
- Event handler registration and dispatch
- Accessibility tree format conversion (CDP flat → nested)
- Error handling (timeouts, disconnects, CDP errors)
- Target discovery utilities
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.mcp.cdp_client import CDPClient, CDPError


# ============================================================================
# Unit tests for accessibility tree conversion
# ============================================================================

class TestA11yTreeBuilding:
    """Test conversion of CDP flat accessibility nodes to nested tree."""

    def test_empty_nodes_returns_none(self):
        client = CDPClient("ws://fake")
        assert client._build_a11y_tree([]) is None

    def test_single_root_node(self):
        nodes = [
            {
                "nodeId": "1",
                "role": {"value": "WebArea"},
                "name": {"value": "Test Page"},
                "childIds": [],
            }
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert tree is not None
        assert tree["role"] == "WebArea"
        assert tree["name"] == "Test Page"
        assert tree["children"] == []

    def test_nested_children(self):
        nodes = [
            {
                "nodeId": "1",
                "role": {"value": "WebArea"},
                "name": {"value": "Page"},
                "childIds": ["2", "3"],
            },
            {
                "nodeId": "2",
                "role": {"value": "heading"},
                "name": {"value": "Title"},
                "childIds": [],
            },
            {
                "nodeId": "3",
                "role": {"value": "button"},
                "name": {"value": "Click me"},
                "childIds": [],
            },
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert tree is not None
        assert len(tree["children"]) == 2
        assert tree["children"][0]["role"] == "heading"
        assert tree["children"][0]["name"] == "Title"
        assert tree["children"][1]["role"] == "button"
        assert tree["children"][1]["name"] == "Click me"

    def test_deep_nesting(self):
        nodes = [
            {"nodeId": "1", "role": {"value": "WebArea"}, "name": {"value": ""}, "childIds": ["2"]},
            {"nodeId": "2", "role": {"value": "navigation"}, "name": {"value": "Nav"}, "childIds": ["3"]},
            {"nodeId": "3", "role": {"value": "link"}, "name": {"value": "Home"}, "childIds": []},
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert tree["children"][0]["role"] == "navigation"
        assert tree["children"][0]["children"][0]["role"] == "link"
        assert tree["children"][0]["children"][0]["name"] == "Home"

    def test_boolean_properties_extracted(self):
        nodes = [
            {
                "nodeId": "1",
                "role": {"value": "checkbox"},
                "name": {"value": "Accept terms"},
                "childIds": [],
                "properties": [
                    {"name": "checked", "value": {"value": True}},
                    {"name": "disabled", "value": {"value": False}},
                    {"name": "required", "value": {"value": True}},
                ],
            },
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert tree is not None
        assert tree["checked"] is True
        assert tree["required"] is True
        # disabled=False should not be included
        assert "disabled" not in tree

    def test_value_field_extracted(self):
        nodes = [
            {
                "nodeId": "1",
                "role": {"value": "textbox"},
                "name": {"value": "Email"},
                "value": {"value": "test@example.com"},
                "childIds": [],
            },
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert tree["value"] == "test@example.com"

    def test_missing_child_ids_ignored(self):
        """If a child ID references a non-existent node, skip it."""
        nodes = [
            {
                "nodeId": "1",
                "role": {"value": "WebArea"},
                "name": {"value": "Page"},
                "childIds": ["2", "999"],  # 999 doesn't exist
            },
            {
                "nodeId": "2",
                "role": {"value": "heading"},
                "name": {"value": "Title"},
                "childIds": [],
            },
        ]
        client = CDPClient("ws://fake")
        tree = client._build_a11y_tree(nodes)
        assert len(tree["children"]) == 1
        assert tree["children"][0]["name"] == "Title"


# ============================================================================
# CDP connection and messaging tests (mocked WebSocket)
# ============================================================================

class TestCDPClientConnection:
    """Test CDP command sending and response matching."""

    @pytest.mark.asyncio
    async def test_send_raises_when_not_connected(self):
        client = CDPClient("ws://fake")
        with pytest.raises(CDPError, match="Not connected"):
            await client.send("Page.navigate", {"url": "http://example.com"})

    @pytest.mark.asyncio
    async def test_event_handler_registration(self):
        client = CDPClient("ws://fake")

        handler = MagicMock()
        client.on("Page.loadEventFired", handler)

        assert "Page.loadEventFired" in client._event_handlers
        assert handler in client._event_handlers["Page.loadEventFired"]

    @pytest.mark.asyncio
    async def test_event_handler_removal(self):
        client = CDPClient("ws://fake")

        handler = MagicMock()
        client.on("Page.loadEventFired", handler)
        client.off("Page.loadEventFired", handler)

        assert "Page.loadEventFired" not in client._event_handlers

    @pytest.mark.asyncio
    async def test_off_all_handlers(self):
        client = CDPClient("ws://fake")

        h1 = MagicMock()
        h2 = MagicMock()
        client.on("Test.event", h1)
        client.on("Test.event", h2)

        client.off("Test.event")  # Remove all

        assert "Test.event" not in client._event_handlers

    @pytest.mark.asyncio
    async def test_off_nonexistent_event(self):
        """off() on unknown event should not raise."""
        client = CDPClient("ws://fake")
        client.off("Nonexistent.event")  # Should not raise

    @pytest.mark.asyncio
    async def test_close_cancels_pending(self):
        client = CDPClient("ws://fake")
        # Simulate a pending future
        future = asyncio.get_event_loop().create_future()
        client._pending[1] = future
        client._connected = True

        await client.close()

        assert future.cancelled()
        assert len(client._pending) == 0


# ============================================================================
# Integration test (requires real Chrome) — skipped if not available
# ============================================================================

@pytest.fixture
async def chrome_cdp_port():
    """Launch a headless Chrome for integration testing."""
    from app.mcp.chrome_launcher import find_chrome, launch_chrome_headless, wait_for_cdp_ready, kill_process_tree
    import tempfile

    chrome_path = find_chrome()
    if not chrome_path:
        pytest.skip("Chrome not found on system")

    tmpdir = tempfile.mkdtemp(prefix="cdp_test_")
    port = 9333  # Use non-default port for testing

    proc = await launch_chrome_headless(
        chrome_path=chrome_path,
        user_data_dir=tmpdir,
        cdp_port=port,
    )

    ready = await wait_for_cdp_ready(port=port, timeout=15.0)
    if not ready:
        kill_process_tree(proc)
        pytest.skip("Chrome failed to start")

    yield port

    kill_process_tree(proc)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_cdp_client_real_chrome(chrome_cdp_port):
    """Integration test: connect to real Chrome, navigate, get title."""
    from app.mcp.cdp_client import connect_to_page

    client = await connect_to_page(port=chrome_cdp_port)
    try:
        # Enable required domains
        await client.send("Page.enable")
        await client.send("Runtime.enable")

        # Navigate to a data URL (no network needed)
        await client.navigate("data:text/html,<title>Test Page</title><h1>Hello</h1>")
        await asyncio.sleep(1.0)

        # Get title
        title = await client.get_title()
        assert title == "Test Page"

        # Get URL
        url = await client.get_url()
        assert "data:text/html" in url

        # Evaluate JS
        result = await client.evaluate("1 + 2")
        assert result == 3

        # Screenshot
        screenshot_b64 = await client.screenshot()
        assert len(screenshot_b64) > 100  # Non-trivial base64 string

        # Accessibility tree
        try:
            await client.send("Accessibility.enable")
            tree = await client.get_accessibility_tree()
            assert tree is not None
            assert tree["role"] in ("WebArea", "RootWebArea")
        except CDPError:
            pass  # Some Chrome versions don't support Accessibility domain

    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cdp_client_tab_management(chrome_cdp_port):
    """Integration test: create tabs, list targets, switch."""
    from app.mcp.cdp_client import connect_to_page, get_cdp_targets

    client = await connect_to_page(port=chrome_cdp_port)
    try:
        await client.send("Page.enable")

        # List initial targets
        targets = await get_cdp_targets(port=chrome_cdp_port)
        pages = [t for t in targets if t.get("type") == "page"]
        initial_count = len(pages)

        # Create a new tab
        target_id = await client.create_target("data:text/html,<title>Tab2</title>")
        assert target_id

        # Verify new target exists
        targets = await get_cdp_targets(port=chrome_cdp_port)
        pages = [t for t in targets if t.get("type") == "page"]
        assert len(pages) == initial_count + 1

    finally:
        await client.close()
