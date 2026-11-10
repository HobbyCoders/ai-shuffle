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


def _recording_server(calls):
    # serve.run() subclasses Server to signal passive streams before draining.
    # Preserve the class boundary while recording constructor/run arguments.
    class RecordingServer(uvicorn.Server):
        def __init__(self, config):
            calls(config)
            super().__init__(config)

        def run(self, **kwargs):
            calls.return_value.run(**kwargs)

    return RecordingServer


@pytest.fixture(autouse=True)
def _isolated_preview_listener(monkeypatch):
    # serve.run() records both globals. Restoring only the port leaks a
    # loopback bind into later workspace-card tests on the same worker.
    from app.core import preview_access

    monkeypatch.setattr(preview_access, "_listener_port", None)
    monkeypatch.setattr(preview_access, "_listener_host", None)
    from app.network import lan

    monkeypatch.setattr(lan, "_listener_port", None)


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
@pytest.mark.parametrize("preview_host", [None, "0.0.0.0"])
def test_supervisor_selects_protected_port_without_changing_trust(
    monkeypatch: pytest.MonkeyPatch, override: str | None, expected: int,
    preview_host: str | None,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import serve

    if preview_host is None:
        monkeypatch.delenv("SHUFFLE_PREVIEW_HOST", raising=False)
    else:
        monkeypatch.setenv("SHUFFLE_PREVIEW_HOST", preview_host)
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
    monkeypatch.setattr(uvicorn, "Server", _recording_server(server))
    serve.run(None, SimpleNamespace(host="127.0.0.1", port=18000, log_level="error"))
    # Local, protected, the agent browser's preview-only listener, then the
    # loopback-bound worker's LAN-sync listener on all interfaces.
    assert binds == [
        ("127.0.0.1", 18000), ("127.0.0.1", expected),
        (preview_host or "127.0.0.1", 18002), ("0.0.0.0", 18003),
    ]
    assert preview_access._listener_host == (preview_host or "127.0.0.1")
    config = server.call_args.args[0]
    assert config.proxy_headers is False
    assert config.app.local_port == 18000
    assert config.app.remote_port == expected
    assert config.app.preview_port == 18002
    assert preview_access.listener_port() == 18002
    assert config.app.lan_port == 18003
    from app.network import lan
    assert lan.listener_port() == 18003
    assert len(server.return_value.run.call_args.kwargs["sockets"]) == 4
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
    monkeypatch.setenv("SHUFFLE_LAN_PORT", "off")
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
    monkeypatch.setattr(uvicorn, "Server", _recording_server(server))
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


# ---------------------------------------------------------------------------
# The LAN-sync listener of a loopback-bound (desktop) worker
# ---------------------------------------------------------------------------

def test_lan_port_only_for_loopback_bound_workers():
    from app.serve import configured_lan_port

    # Bound to the network already (Docker, server installs): no extra listener.
    for host in ("0.0.0.0", "10.0.0.5", "::", ""):
        assert configured_lan_port({}, host, 8000, 8001, 8002) is None
    for host in ("127.0.0.1", "::1", "[::1]", "localhost"):
        assert configured_lan_port({}, host, 8000, 8001, 8002) == 8003
    assert configured_lan_port({}, "127.0.0.1", 18080, 18081, 18082) == 18083
    # The default steps over the protected and preview ports.
    assert configured_lan_port({}, "127.0.0.1", 7998, 8001, 8002) == 8003
    assert configured_lan_port({"SHUFFLE_LAN_PORT": "9100"}, "127.0.0.1", 8000, 8001, 8002) == 9100
    for off in ("0", "off", "OFF", "false", "no"):
        assert configured_lan_port({"SHUFFLE_LAN_PORT": off}, "127.0.0.1", 8000, 8001, 8002) is None
    for bad in ("8000", "8001", "8002", "65536", "-1", "port"):
        with pytest.raises(ValueError):
            configured_lan_port({"SHUFFLE_LAN_PORT": bad}, "127.0.0.1", 8000, 8001, 8002)


def test_network_bound_worker_opens_no_lan_listener(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import serve
    from app.network import lan

    monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    monkeypatch.delenv("SHUFFLE_PREVIEW_PORT", raising=False)
    monkeypatch.delenv("SHUFFLE_PREVIEW_HOST", raising=False)
    monkeypatch.delenv("SHUFFLE_LAN_PORT", raising=False)
    binds = []

    def bind(host, port):
        binds.append((host, port))
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = (host, port)
        return sock

    monkeypatch.setattr(serve, "bind_listener", bind)
    monkeypatch.setattr(lan, "_listener_port", 9)
    server = MagicMock()
    monkeypatch.setattr(uvicorn, "Server", _recording_server(server))
    serve.run(None, SimpleNamespace(host="0.0.0.0", port=18000, log_level="error"))
    assert binds == [("0.0.0.0", 18000), ("127.0.0.1", 8001), ("0.0.0.0", 18002)]
    assert server.call_args.args[0].app.lan_port is None
    assert lan.listener_port() is None


def test_worker_starts_when_the_lan_port_is_taken(monkeypatch, caplog):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import serve
    from app.network import lan

    monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    monkeypatch.delenv("SHUFFLE_PREVIEW_PORT", raising=False)
    monkeypatch.delenv("SHUFFLE_PREVIEW_HOST", raising=False)
    monkeypatch.delenv("SHUFFLE_LAN_PORT", raising=False)
    binds = []

    def bind(host, port):
        if port == 18003:
            raise OSError("address already in use")
        binds.append((host, port))
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = (host, port)
        return sock

    monkeypatch.setattr(serve, "bind_listener", bind)
    monkeypatch.setattr(lan, "_listener_port", 9)
    server = MagicMock()
    monkeypatch.setattr(uvicorn, "Server", _recording_server(server))
    with caplog.at_level("WARNING", logger="app.serve"):
        serve.run(None, SimpleNamespace(host="127.0.0.1", port=18000, log_level="error"))
    assert binds == [("127.0.0.1", 18000), ("127.0.0.1", 8001), ("127.0.0.1", 18002)]
    assert server.call_args.args[0].app.lan_port is None
    assert lan.listener_port() is None
    assert len(server.return_value.run.call_args.kwargs["sockets"]) == 3
    warnings = [r.getMessage() for r in caplog.records if "LAN sync listener is off" in r.getMessage()]
    assert len(warnings) == 1 and "address already in use" in warnings[0]


def _lan_wrapper(received):
    async def app(scope, receive, send):
        received.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return IngressApplication(app, 8000, 8001, 8002, lan_port=8003)


def _call(wrapper, scope):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    asyncio.run(wrapper(scope, receive, send))
    return sent


@pytest.mark.parametrize("path", [
    "/api/v1/network/peer/lan/hello", "/api/v1/network/peer/lan/session", "/api/v1/network/peer/lan/call",
])
def test_lan_listener_passes_only_the_sealed_paths_without_local_trust(path):
    received = []
    sent = _call(_lan_wrapper(received), {
        "type": "http", "method": "POST", "path": path, "scheme": "http",
        "server": ("10.0.0.5", 8003), "client": ("10.0.0.9", 5000),
        "headers": [(b"x-forwarded-proto", b"https"), (b"x-shuffle-ingress", b"local")],
        "shuffle.ingress": "local", "shuffle.forwarded_https": True,
    })
    assert sent[0]["status"] == 200
    assert received[0]["shuffle.ingress"] == "lan"
    assert received[0]["shuffle.forwarded_https"] is False


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/api/v1/auth/status"), ("GET", "/api/v1/sessions"), ("POST", "/api/v1/sessions"),
    ("GET", "/health"), ("GET", "/api/v1/network/peer/lan/hello"), ("POST", "/api/v1/network/peer/exchange"),
    ("POST", "/api/v1/network/peer/control/stop"), ("POST", "/api/v1/network/peer/lan/hello/"),
    ("POST", "/api/v1/network/peer/lan"), ("POST", "/api/v1/auth/login"),
])
def test_lan_listener_refuses_everything_else(method, path):
    received = []
    sent = _call(_lan_wrapper(received), {
        "type": "http", "method": method, "path": path, "server": ("10.0.0.5", 8003),
        "client": ("10.0.0.9", 5000), "headers": [],
    })
    assert received == []
    assert sent[0]["status"] == 403
    assert b"sealed sync" in sent[1]["body"]


def test_lan_listener_refuses_websockets():
    received = []
    sent = _call(_lan_wrapper(received), {
        "type": "websocket", "path": "/api/v1/network/peer/lan/call", "server": ("10.0.0.5", 8003),
        "client": ("10.0.0.9", 5000), "headers": [],
    })
    assert received == []
    assert sent == [{"type": "websocket.close", "code": 4403}]


def test_lan_listener_port_must_be_its_own():
    for port in (8000, 8001, 8002):
        with pytest.raises(ValueError):
            IngressApplication(None, 8000, 8001, 8002, lan_port=port)


@pytest.mark.parametrize("client,host", [(("10.0.0.9", 5000), "10.0.0.5:8003"),
                                         (("127.0.0.1", 5000), "127.0.0.1:8003")])
def test_lan_ingress_is_never_a_trusted_local_owner(monkeypatch, client, host):
    """The owner-auth checks admit only the ``local`` listener: a request that
    reached the LAN-sync port is no owner, whether from the LAN or loopback."""
    from starlette.requests import Request
    from app.core import owner_auth

    monkeypatch.setattr(owner_auth.desktop_listener, "configured", lambda: False)

    def request(ingress):
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"host", host.encode())],
                        "client": client, "server": ("10.0.0.5", 8003), "shuffle.ingress": ingress,
                        "query_string": b""})

    lan_request = request("lan")
    assert owner_auth.direct_local(lan_request) is False
    assert owner_auth.loopback_request(lan_request) is False
    assert owner_auth.owner_session(lan_request, "any") is False
    # The same request on the local listener does count, so the check above is the ingress itself.
    local_request = request("local")
    assert owner_auth.direct_local(local_request) or owner_auth.loopback_request(local_request)
