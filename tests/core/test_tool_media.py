"""Real image bytes survive tool execution, live adaptation and durable replay."""
import base64
import json
import os
from pathlib import Path
import struct
import zlib
from unittest.mock import AsyncMock

import pytest

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.fluid_transcript import _block_to_transcript, _inner_calls_by_block
from app.core.query_streaming import extract_mcp_image, truncate_large_payload
from fluid.events.model import ToolResult, event_from_dict, event_to_dict
from fluid.engine.native import NativeLoop
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.store.base import MemoryStore
from fluid.tools.builtins.read import _read_image
from fluid.tools.registry import _normalize


def png_bytes(color=(36, 110, 230)):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = (b'\x00' + bytes(color) * 96) * 64
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 96, 64, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


@pytest.mark.parametrize('name', ['Read', 'mcp__browser__screenshot'])
async def test_native_image_event_roundtrips_adapter_recorder_and_artifact(tmp_path, mock_db, monkeypatch, name):
    from app.core import artifact_store
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions, session_events, artifacts
    from app.core.provider.events import ToolResultBlock
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.api.artifacts import router, _require_auth_or_signed

    data = png_bytes((230, 115, 35) if name == 'Read' else (36, 110, 230))
    path = tmp_path / 'harmless.png'
    path.write_bytes(data)
    outcome = _read_image(path, 'image/png') if name == 'Read' else _normalize({
        'content': [{'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(data).decode()}],
    })
    loop = NativeLoop(provider=None, store=MemoryStore(), model='offline-fixture')
    loop._emit = AsyncMock()
    await loop._finish_tool('ses_media', 'turn_media', name, 'image-call', outcome)
    event = next(call.args[0] for call in loop._emit.call_args_list if isinstance(call.args[0], ToolResult))
    replayed = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert replayed == event
    content = HarnessAdapter()._translate(replayed)[0].content[0]
    assert isinstance(content, ToolResultBlock)
    screenshot = extract_mcp_image(content.content)
    assert screenshot == 'data:image/png;base64,' + base64.b64encode(data).decode()
    assert base64.b64encode(data).decode() not in truncate_large_payload(content.content)

    monkeypatch.setattr(artifact_store, 'ARTIFACTS_DIR', tmp_path / 'artifacts')
    sid = 'media-session'
    sessions.create_session(sid, 'default')
    recorder = await TurnRecorder.maybe_create(sid)
    await recorder.on_event({'type': 'tool_use', 'name': name, 'id': 'image-call', 'input': {'file_path': str(path)} if name == 'Read' else {}})
    await recorder.on_event({'type': 'tool_result', 'name': name, 'tool_use_id': 'image-call', 'output': outcome.text, 'screenshot': screenshot})
    await recorder.close()
    rows = session_events.get_events(sid)
    digest = next(row for row in rows if row['event_type'] == 'tool.result')['payload']['artifact_hashes'][0]
    assert artifacts.get_artifact(digest)['created_by_session'] == sid

    app = FastAPI()
    app.include_router(router)
    def authenticated(request: Request):
        request.state.is_admin = True
        return 'fixture-owner'
    app.dependency_overrides[_require_auth_or_signed] = authenticated
    with TestClient(app) as client:
        response = client.get(f'/api/v1/artifacts/{digest}?session_id={sid}')
        assert response.status_code == 200
        assert response.content == data
        assert response.headers['content-type'].startswith('image/png')
    if os.environ.get('TOOL_MEDIA_PREVIEW_DIR'):
        preview = Path(os.environ['TOOL_MEDIA_PREVIEW_DIR'])
        preview.mkdir(parents=True, exist_ok=True)
        (preview / ('read.json' if name == 'Read' else 'browser.json')).write_text(json.dumps({
            'name': name, 'events': rows, 'digest': digest,
            'data': base64.b64encode(data).decode(),
        }, default=str), encoding='utf-8')


def test_delegated_image_survives_live_and_node_replay():
    encoded = base64.b64encode(png_bytes()).decode()
    mapper = StreamJsonMapper(session_id='ses_media', turn_id='turn_media', model='offline', user_node_id='node_user')
    mapped = mapper.feed({'type': 'user', 'message': {'content': [{
        'type': 'tool_result', 'tool_use_id': 'screenshot-call', 'content': [
            {'type': 'text', 'text': 'Screenshot captured'},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': encoded}},
        ],
    }]}})
    event = next(item for item in mapped.events if isinstance(item, ToolResult))
    live = HarnessAdapter()._translate(event)[0].content[0].content
    block = mapped.message.content[0]
    stored = {'kind': 'toolresultblock', 'tool_use_id': block.tool_use_id,
              'content': [item.to_dict() for item in block.content]}
    replay = _block_to_transcript(stored)['content']
    assert extract_mcp_image(live) == extract_mcp_image(replay) == f'data:image/png;base64,{encoded}'
    assert encoded not in event.output


def test_code_mode_inner_image_is_retained_in_durable_projection():
    encoded = base64.b64encode(png_bytes()).decode()
    events = [{'event': {'_cls': 'ToolUse', 'tool_use_id': 'cell'}},
              {'event': {'_cls': 'ToolUse', 'tool_use_id': 'read', 'name': 'Read'}},
              {'event': {'_cls': 'ToolResult', 'tool_use_id': 'read', 'output': 'image read',
                         'images': [{'media_type': 'image/png', 'data': encoded}]}}]
    result = _inner_calls_by_block(events, {'cell'})['cell'][0]
    assert extract_mcp_image(result['output']) == f'data:image/png;base64,{encoded}'
