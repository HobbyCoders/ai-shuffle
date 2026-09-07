"""Exercise the actual HTTP/WS listener boundary, including forwarded headers."""
import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from app.serve import IngressApplication, bind_listener


def test_two_sockets_preserve_tcp_identity_and_share_one_lifespan():
    lifecycle = []

    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                lifecycle.append(event['type'])
                await send({'type': event['type'] + '.complete'})
                if event['type'] == 'lifespan.shutdown':
                    return
        payload = json.dumps({'ingress': scope['shuffle.ingress'], 'peer': scope['client'][0]})
        if scope['type'] == 'websocket':
            await receive()
            await send({'type': 'websocket.accept'})
            await send({'type': 'websocket.send', 'text': payload})
            await send({'type': 'websocket.close'})
        else:
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': payload.encode()})

    with bind_listener('127.0.0.1', 0) as local, bind_listener('127.0.0.1', 0) as remote:
        ports = [local.getsockname()[1], remote.getsockname()[1]]
        config = uvicorn.Config(IngressApplication(app, *ports), proxy_headers=False, log_level='error')
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={'sockets': [local, remote]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline and thread.is_alive():
                time.sleep(.01)
            assert server.started
            with httpx.Client(trust_env=False) as client:
                for port, ingress in zip(ports, ['local', 'remote']):
                    result = client.get(f'http://127.0.0.1:{port}/', headers={
                        'X-Forwarded-For': '192.168.1.1',
                        'X-Forwarded-Host': f'127.0.0.1:{ports[0]}',
                        'X-Shuffle-Ingress': 'local',
                    }).json()
                    assert result == {'ingress': ingress, 'peer': '127.0.0.1'}
                    with connect(f'ws://127.0.0.1:{port}/') as ws:
                        assert json.loads(ws.recv()) == result
        finally:
            server.should_exit = True
            thread.join(10)
        assert not thread.is_alive()
    assert lifecycle == ['lifespan.startup', 'lifespan.shutdown']


def test_unrecognized_socket_cannot_inherit_local_marker():
    received = []

    async def app(scope, receive, send):
        received.append(scope['shuffle.ingress'])

    wrapper = IngressApplication(app, 8000)
    for server in [None, ('127.0.0.1', 8001), ('127.0.0.1', 9999)]:
        asyncio.run(wrapper({'type': 'http', 'server': server, 'shuffle.ingress': 'local'}, None, None))
    assert received == ['remote', 'remote', 'remote']


def test_same_listener_port_is_rejected():
    with pytest.raises(ValueError):
        IngressApplication(None, 8001)
