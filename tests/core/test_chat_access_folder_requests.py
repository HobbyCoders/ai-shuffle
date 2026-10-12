"""Folder request suggestions are editable hints, never authorization."""
import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import chat_access, chat_files
from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
from app.mcp import chat_access_sdk
from tests.core import test_chat_access, test_chat_access_feedback

broker = test_chat_access.broker
client = test_chat_access_feedback.client


@pytest.mark.asyncio
async def test_sdk_suggestion_reaches_card_and_only_private_submission_opens_folder(client, broker, monkeypatch, tmp_path):
    from app.api import chat_access as api
    monkeypatch.setattr(api, "checked", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_access_sdk, "broker", broker)
    monkeypatch.setattr(chat_files, "available", lambda: True)
    monkeypatch.setattr(broker, "_notify", lambda *args, **kwargs: None)
    # A portable descriptor stub: real folder confinement has platform tests.
    descriptor_target = tmp_path / "descriptor"
    descriptor_target.touch()
    opened = Mock(side_effect=lambda path: os.open(descriptor_target, os.O_RDONLY))
    monkeypatch.setattr(chat_files, "open_root", opened)
    tools = {t.name: t for t in recorded_sdk_server_tools(chat_access_sdk.create_chat_access_server("chat"))}
    tool = tools["RequestChatAccess"]
    assert "suggested_path" in tool.input_schema["properties"]
    assert "suggested_path" not in tool.input_schema["required"]
    requested = asyncio.create_task(tool.handler({
        "kind": "folder", "purpose": "Test isolated folder", "suggested_path": "/workspace/proposed",
    }))
    try:
        async with asyncio.timeout(3):
            while not broker.requests:
                await asyncio.sleep(0)
        snapshot = (await client.get("/api/v1/chat-access/chat")).json()
        pending = snapshot["requests"][0]
        assert pending["suggested_path"] == "/workspace/proposed"
        assert not requested.done() and not broker.grants
        opened.assert_not_called()
        endpoint = f"/api/v1/chat-access/chat/requests/{pending['id']}"
        response = await client.post(endpoint, json={"connection": {
            "kind": "folder", "label": "Test", "path": "/workspace/private-choice",
        }})
        assert response.status_code == 200
        opened.assert_called_once_with("/workspace/private-choice")
        result = await requested
        handle = json.loads(result["content"][0]["text"])
        assert set(handle) == {"id", "kind", "expires_at", "writable"}
        assert handle["writable"] is False
        assert "/workspace" not in json.dumps(result)
        assert "/workspace" not in json.dumps(await broker.resources("chat"))
        broker.revoke("chat", handle["id"])
    finally:
        requested.cancel()
        await asyncio.gather(requested, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,path", [
    ("machine", "/workspace/test"), ("service", "/workspace/test"),
    ("folder", "relative"), ("folder", "/bad\npath"), ("folder", "/bad\0path"),
    ("folder", "/" + "a" * 4096), ("folder", None),
])
async def test_invalid_suggestion_opens_no_request(broker, kind, path):
    with pytest.raises(chat_access.AccessDenied, match="absolute folder path"):
        await broker.request("chat", kind, "Test", suggested_path=path)
    assert not broker.requests and not broker.grants


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (FileNotFoundError("private-path-sentinel"), "Folder not found on the worker"),
    (NotADirectoryError("private-path-sentinel"), "Choose a folder, not a file or symbolic link."),
    (PermissionError("private-path-sentinel"), "The worker cannot read this folder."),
])
async def test_folder_failure_is_private_specific_and_retryable(client, broker, monkeypatch, error, expected, caplog):
    monkeypatch.setattr(chat_files, "available", lambda: True)
    monkeypatch.setattr(broker, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_files, "open_root", Mock(side_effect=error))
    requested = asyncio.create_task(broker.request("chat", "folder", "Test"))
    try:
        async with asyncio.timeout(3):
            while not broker.requests:
                await asyncio.sleep(0)
        pending = next(iter(broker.requests.values()))
        assert "suggested_path" not in pending.public()  # older callers remain valid
        response = await client.post(f"/api/v1/chat-access/chat/requests/{pending.id}", json={
            "connection": {"kind": "folder", "path": "/private-path-sentinel", "label": "Test"},
        })
        assert response.status_code == 400
        assert response.json()["detail"].startswith(expected)
        if isinstance(error, FileNotFoundError):
            assert "For Docker, use a mounted container path" in response.json()["detail"]
            assert "ServerWorker" not in response.json()["detail"]
        assert "SSH" not in response.text
        assert "private-path-sentinel" not in response.text + caplog.text
        assert not pending.claiming and not requested.done() and not broker.grants
    finally:
        requested.cancel()
        await asyncio.gather(requested, return_exceptions=True)


@pytest.mark.skipif(not chat_files.available(), reason="real Linux/macOS folder broker required")
@pytest.mark.asyncio
async def test_missing_real_folder_can_be_created_then_approved_on_same_request(client, broker, monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "_notify", lambda *args, **kwargs: None)
    # Resolve the platform temp root (macOS /var may be an alias); no links in the selected path.
    path = str(tmp_path.resolve() / "empty-folder")
    requested = asyncio.create_task(broker.request("chat", "folder", "Test", suggested_path=path))
    try:
        async with asyncio.timeout(3):
            while not broker.requests:
                await asyncio.sleep(0)
        pending = next(iter(broker.requests.values()))
        endpoint = f"/api/v1/chat-access/chat/requests/{pending.id}"
        body = {"connection": {"kind": "folder", "path": path, "label": "Test"}}
        failed = await client.post(endpoint, json=body)
        assert failed.status_code == 400 and failed.json()["detail"].startswith("Folder not found")
        assert path not in failed.text and not broker.grants and not requested.done()
        os.mkdir(path)
        accepted = await client.post(endpoint, json=body)
        assert accepted.status_code == 200
        handle = await requested
        assert path not in json.dumps(handle)
        assert await broker.run("chat", handle["id"], "list", {"path": ""}) == {"entries": []}
        broker.revoke("chat", handle["id"])
    finally:
        requested.cancel()
        await asyncio.gather(requested, return_exceptions=True)
