"""Exercise the actual HTTP/WS listener boundary, including forwarded headers."""
import asyncio
import json
import os
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from app.serve import IngressApplication, bind_listener, configured_remote_port


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
        payload = json.dumps({'ingress': scope['shuffle.ingress'], 'peer': scope['client'][0],
                              'forwarded_https': scope['shuffle.forwarded_https']})
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
                        'X-Forwarded-Proto': 'https',
                        'X-Shuffle-Ingress': 'local',
                    }).json()
                    assert result == {'ingress': ingress, 'peer': '127.0.0.1', 'forwarded_https': ingress == 'remote'}
                    with connect(f'ws://127.0.0.1:{port}/', additional_headers={'X-Forwarded-Proto': 'https'}) as ws:
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


@pytest.mark.parametrize('server,peer,headers,secure', [
    (('127.0.0.1', 18001), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')], True),
    (('::1', 18001), ('::1', 1234), [(b'x-forwarded-proto', b'https')], True),
    (('127.0.0.1', 8000), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')], False),
    (('127.0.0.1', 9999), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')], False),
    (None, ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')], False),
    (('127.0.0.1', 18001), ('10.0.0.2', 1234), [(b'x-forwarded-proto', b'https')], False),
    (('10.0.0.1', 18001), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')], False),
    (('127.0.0.1', 18001), None, [(b'x-forwarded-proto', b'https')], False),
    (('127.0.0.1', 18001), ('127.0.0.1', 1234), [], False),
    (('127.0.0.1', 18001), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'http')], False),
    (('127.0.0.1', 18001), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https, http')], False),
    (('127.0.0.1', 18001), ('127.0.0.1', 1234), [(b'x-forwarded-proto', b'https')] * 2, False),
])
def test_https_attestation_only_from_protected_loopback_proxy(server, peer, headers, secure):
    received = []

    async def app(scope, receive, send):
        received.append(scope)

    wrapper = IngressApplication(app, 8000, 18001)
    asyncio.run(wrapper({'type': 'http', 'scheme': 'http', 'server': server,
                        'client': peer, 'headers': headers,
                        'shuffle.forwarded_https': True}, None, None))
    assert received[0]['shuffle.forwarded_https'] is secure
    assert received[0]['scheme'] == 'http'
    assert received[0]['client'] == peer
    assert received[0]['shuffle.ingress'] == ('local' if server and server[1] == 8000 else 'remote')


def test_isolated_remote_port_is_explicit_and_cannot_claim_normal_ports():
    assert configured_remote_port({}) == 8001
    assert configured_remote_port({'SHUFFLE_PROTECTED_PORT': '18081'}) == 8001
    assert configured_remote_port({'SHUFFLE_DESKTOP_ISOLATED': '1', 'SHUFFLE_PROTECTED_PORT': '18081'}) == 18081
    for value in ('8000', '8001', '80', '65536', 'invalid'):
        with pytest.raises(ValueError):
            configured_remote_port({'SHUFFLE_DESKTOP_ISOLATED': '1', 'SHUFFLE_PROTECTED_PORT': value})


def test_listeners_are_close_on_exec():
    with bind_listener('127.0.0.1', 0) as sock:
        assert sock.get_inheritable() is False


_CHILD_HOLDS = (
    "import os, sys\n"
    "fd, inode = int(sys.argv[1]), int(sys.argv[2])\n"
    "try:\n"
    "    print('held' if os.fstat(fd).st_ino == inode else 'free')\n"
    "except OSError:\n"
    "    print('free')\n"
)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX descriptor inheritance')
def test_uvloop_children_do_not_receive_the_listener():
    """uvloop spawns through libuv, which ignores close_fds: only close-on-exec
    keeps a worker listener out of cloudflared, CLI harnesses and agent shell
    commands (#2145)."""
    uvloop = pytest.importorskip('uvloop')
    with bind_listener('127.0.0.1', 0) as sock:
        sock.listen()
        inode = os.fstat(sock.fileno()).st_ino

        async def child_view() -> str:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-c', _CHILD_HOLDS, str(sock.fileno()), str(inode),
                stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            return out.decode().strip()

        assert uvloop.run(child_view()) == 'free'
        # Guard the test itself: an inheritable listener does reach the child.
        sock.set_inheritable(True)
        assert uvloop.run(child_view()) == 'held'


def test_same_listener_port_is_rejected():
    with pytest.raises(ValueError):
        IngressApplication(None, 8001)


@pytest.mark.parametrize("override,expected", [(None, 8001), ("18001", 18001)])
def test_supervisor_selects_protected_port_without_changing_trust(monkeypatch, override, expected):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import serve

    if override is None:
        monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    else:
        monkeypatch.setenv("SHUFFLE_DESKTOP_PROTECTED_PORT", override)
    binds = []

    def bind(host, port):
        binds.append((host, port))
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = (host, port)
        return sock

    from app.core import preview_access

    monkeypatch.setattr(serve, "bind_listener", bind)
    monkeypatch.delenv("SHUFFLE_PREVIEW_PORT", raising=False)
    monkeypatch.setattr(preview_access, "_listener_port", None)  # run() records the port
    server = MagicMock()
    monkeypatch.setattr(uvicorn, "Server", server)
    serve.run(None, SimpleNamespace(host="127.0.0.1", port=18000, log_level="error"))
    # Local, protected, then the agent browser's preview-only listener.
    assert binds == [("127.0.0.1", 18000), ("127.0.0.1", expected), ("127.0.0.1", 18002)]
    config = server.call_args.args[0]
    assert config.proxy_headers is False
    assert config.app.local_port == 18000
    assert config.app.remote_port == expected
    assert config.app.preview_port == 18002
    assert preview_access.listener_port() == 18002
    assert len(server.return_value.run.call_args.kwargs["sockets"]) == 3
    server.return_value.run.assert_called_once()


@pytest.mark.parametrize("failure", ["taken", "off", "invalid"])
def test_worker_starts_without_the_preview_listener(monkeypatch, failure):
    """The preview door is optional: a taken, disabled or misnamed port must
    never cost the worker its two required listeners."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import serve
    from app.core import preview_access

    monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    monkeypatch.delenv("SHUFFLE_PREVIEW_PORT", raising=False)
    if failure == "off":
        monkeypatch.setenv("SHUFFLE_PREVIEW_PORT", "0")
    elif failure == "invalid":
        monkeypatch.setenv("SHUFFLE_PREVIEW_PORT", "8001")  # the protected port
    binds = []

    def bind(host, port):
        if failure == "taken" and port == 18002:
            raise OSError("address already in use")
        binds.append((host, port))
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = (host, port)
        return sock

    monkeypatch.setattr(serve, "bind_listener", bind)
    monkeypatch.setattr(preview_access, "_listener_port", 9)
    server = MagicMock()
    monkeypatch.setattr(uvicorn, "Server", server)
    serve.run(None, SimpleNamespace(host="127.0.0.1", port=18000, log_level="error"))
    assert binds == [("127.0.0.1", 18000), ("127.0.0.1", 8001)]
    assert server.call_args.args[0].app.preview_port is None
    assert preview_access.listener_port() is None
    assert len(server.return_value.run.call_args.kwargs["sockets"]) == 2


@pytest.mark.parametrize("port", ["0", "65536", "18000", "not-a-port"])
def test_invalid_protected_port_fails_before_binding(monkeypatch, port):
    from types import SimpleNamespace
    from app import serve

    monkeypatch.setenv("SHUFFLE_DESKTOP_PROTECTED_PORT", port)
    monkeypatch.setattr(serve, "bind_listener", lambda *args: pytest.fail("must validate before binding"))
    with pytest.raises(ValueError):
        serve.run(None, SimpleNamespace(host="127.0.0.1", port=18000, log_level="error"))
