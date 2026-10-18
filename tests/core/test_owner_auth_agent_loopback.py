"""An agent's process on the Docker worker never becomes the owner by its address.

Inside the container, every process that can open a socket to the worker is
the app itself or something it started for an agent (Bash, Monitor, hooks, MCP
servers, the delegated CLI). Plain loopback, IPv6 loopback, the container's
own address and spoofed forwarding headers must therefore confer nothing there,
for reads and for same-origin-looking writes. The owner's own credentials (a
password session or owner Bearer) and trusted LAN from another machine keep
working, and native installs keep loopback ownership.

The last test runs the real auth stack on real sockets and calls it from a
child process spawned the way agent shells are (``agent_runner.wrap`` over the
scrubbed child environment).
"""
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from fastapi import Depends, FastAPI, Request
from starlette.requests import Request as StarletteRequest

from app.api import auth, owner_login
from app.core import desktop_listener, owner_auth
from app.core.config import settings
from app.middleware.csrf import CSRFProtectionMiddleware
from app.serve import IngressApplication, bind_listener

CONTAINER_IP = "172.17.0.2"
TOKEN = "ab" * 32
_REAL_OWN_ADDRESSES = owner_auth.own_addresses


@pytest.fixture(autouse=True)
def _clean_listener_state(monkeypatch):
    desktop_listener.configure(None)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "own_addresses", lambda: frozenset())
    yield
    desktop_listener.configure(None)


def _request(*, peer="127.0.0.1", host="localhost:8000", server=("0.0.0.0", 8000), headers=None,
             method="GET"):
    raw = [(b"host", host.encode())]
    for name, value in (headers or {}).items():
        raw.append((name.lower().encode(), value.encode()))
    return StarletteRequest({
        "type": "http", "method": method, "scheme": "http", "path": "/", "query_string": b"",
        "server": server, "client": (peer, 50000), "headers": raw, "shuffle.ingress": "local",
    })


def _trust_lan(monkeypatch, enabled=True):
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: enabled)


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("peer,host", [
    ("127.0.0.1", "localhost:8000"),
    ("127.0.0.1", "127.0.0.1:8000"),
    ("127.8.9.10", "127.0.0.1:8000"),
    ("::1", "[::1]:8000"),
    ("::1", "localhost:8000"),
    ("::ffff:127.0.0.1", "localhost:8000"),
])
@pytest.mark.parametrize("lan", [False, True])
def test_loopback_is_not_the_owner_in_the_docker_image(docker_install, monkeypatch, peer, host, lan):
    _trust_lan(monkeypatch, lan)
    forged = {"origin": "http://" + host}
    assert owner_auth.local_access(_request(peer=peer, host=host)) is False
    assert owner_auth.local_access(_request(peer=peer, host=host, headers=forged, method="POST")) is False


def test_loopback_with_a_lan_host_header_does_not_fall_through_to_trusted_lan(docker_install, monkeypatch):
    """Asking for the LAN name from loopback must not read as a LAN device."""
    _trust_lan(monkeypatch)
    assert owner_auth.local_access(_request(peer="127.0.0.1", host="192.168.1.10:8000")) is False
    assert owner_auth.local_access(_request(peer="::1", host="192.168.1.10:8000")) is False


@pytest.mark.parametrize("install", ["docker", "native"])
def test_the_workers_own_address_is_never_the_trusted_lan(monkeypatch, install):
    if install == "docker":
        monkeypatch.setattr(owner_auth, "container_install", lambda: True)
    _trust_lan(monkeypatch)
    monkeypatch.setattr(owner_auth, "own_addresses", lambda: frozenset({ipaddress.ip_address(CONTAINER_IP)}))
    request = _request(peer=CONTAINER_IP, host=f"{CONTAINER_IP}:8000",
                       headers={"origin": f"http://{CONTAINER_IP}:8000"}, method="POST")
    assert owner_auth.local_access(request) is False
    # The listening address is recognized even when the interface list is unreadable.
    monkeypatch.setattr(owner_auth, "own_addresses", lambda: frozenset())
    assert owner_auth.local_access(_request(peer=CONTAINER_IP, host=f"{CONTAINER_IP}:8000",
                                            server=(CONTAINER_IP, 8000))) is False
    assert owner_auth.local_access(_request(peer="::ffff:" + CONTAINER_IP, host=f"{CONTAINER_IP}:8000",
                                            server=(CONTAINER_IP, 8000))) is False


@pytest.mark.parametrize("peer,headers", [
    ("127.0.0.1", {"x-forwarded-for": "192.168.1.20"}),
    ("127.0.0.1", {"x-forwarded-for": "127.0.0.1"}),
    ("127.0.0.1", {"forwarded": "for=192.168.1.20"}),
    ("127.0.0.1", {"x-forwarded-host": "localhost:8000"}),
    ("192.168.1.20", {"x-forwarded-for": "127.0.0.1"}),
    (CONTAINER_IP, {"x-forwarded-for": "192.168.1.20"}),
])
@pytest.mark.parametrize("install", ["docker", "native"])
def test_forwarding_headers_confer_nothing(monkeypatch, peer, headers, install):
    monkeypatch.setattr(owner_auth, "container_install", lambda: install == "docker")
    _trust_lan(monkeypatch)
    monkeypatch.setattr(owner_auth, "own_addresses", lambda: frozenset({ipaddress.ip_address(CONTAINER_IP)}))
    assert owner_auth.local_access(_request(peer=peer, host="localhost:8000", headers=headers)) is False


def test_another_machine_on_a_trusted_lan_is_still_the_owner(docker_install, monkeypatch):
    monkeypatch.setattr(owner_auth, "own_addresses", lambda: frozenset({ipaddress.ip_address(CONTAINER_IP)}))
    request = _request(peer="192.168.1.50", host="192.168.1.10:8000", server=(CONTAINER_IP, 8000))
    _trust_lan(monkeypatch, False)
    assert owner_auth.local_access(request) is False
    _trust_lan(monkeypatch, True)
    assert owner_auth.local_access(request) is True


def test_native_install_keeps_loopback_ownership():
    assert owner_auth.container_install() is False  # the test default
    assert owner_auth.local_access(_request(peer="127.0.0.1", host="localhost:8000")) is True
    assert owner_auth.local_access(_request(peer="::1", host="[::1]:8000")) is True


@pytest.mark.parametrize("install", ["docker", "native"])
def test_desktop_loopback_needs_its_listener_token(monkeypatch, install):
    monkeypatch.setattr(owner_auth, "container_install", lambda: install == "docker")
    desktop_listener.configure(TOKEN)
    assert owner_auth.local_access(_request()) is False
    assert owner_auth.local_access(_request(headers={desktop_listener.HEADER: TOKEN})) is True


@pytest.mark.real_install_detection
def test_container_install_follows_the_deployment_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(owner_auth, "_CONTAINER_MARKERS", (str(tmp_path / "no-marker"),))
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    monkeypatch.setattr(settings, "local_mode", False)
    assert owner_auth.container_install() is True
    assert owner_auth.loopback_owner() is False
    desktop_listener.configure(TOKEN)
    assert owner_auth.loopback_owner() is True
    desktop_listener.configure(None)
    monkeypatch.setattr(settings, "local_mode", True)
    assert owner_auth.container_install() is False
    assert owner_auth.loopback_owner() is True


@pytest.mark.real_install_detection
@pytest.mark.parametrize("marker", ["dockerenv", "containerenv"])
def test_a_container_marker_outranks_local_mode_from_a_writable_env_file(monkeypatch, tmp_path, marker):
    """LOCAL_MODE=true in a .env an agent can write must not reopen loopback."""
    present = tmp_path / marker
    present.touch()
    monkeypatch.setattr(owner_auth, "_CONTAINER_MARKERS", (str(tmp_path / "absent"), str(present)))
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    monkeypatch.setattr(settings, "local_mode", True)
    assert owner_auth.container_install() is True
    assert owner_auth.loopback_owner() is False
    assert owner_auth.local_access(_request(peer="127.0.0.1", host="localhost:8000")) is False
    # Only a branch preview (its own process environment, never a .env file)
    # started in local mode keeps loopback: it is the agent's own instance.
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    assert owner_auth.container_install() is False
    monkeypatch.setattr(settings, "local_mode", False)
    assert owner_auth.container_install() is True
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "true")
    monkeypatch.setattr(settings, "local_mode", True)
    assert owner_auth.container_install() is True


def test_own_addresses_lists_every_interface(monkeypatch):
    from types import SimpleNamespace as Addr

    import psutil
    monkeypatch.setattr(owner_auth, "own_addresses", _REAL_OWN_ADDRESSES)
    monkeypatch.setattr(owner_auth, "_own_addresses_cache", (float("-inf"), frozenset()))
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "lo": [Addr(family=socket.AF_INET, address="127.0.0.1")],
        "eth0": [Addr(family=socket.AF_INET, address=CONTAINER_IP),
                 Addr(family=socket.AF_INET6, address="fe80::42:acff:fe11:2%eth0"),
                 Addr(family=getattr(socket, "AF_PACKET", 17), address="02:42:ac:11:00:02")],
    })
    found = owner_auth.own_addresses()
    assert ipaddress.ip_address(CONTAINER_IP) in found
    assert ipaddress.ip_address("fe80::42:acff:fe11:2") in found
    assert owner_auth.worker_peer(_request(peer="fe80::42:acff:fe11:2%eth0", server=("::", 8000)))
    assert not owner_auth.worker_peer(_request(peer="192.168.1.50", server=("0.0.0.0", 8000)))


def test_unreadable_peer_counts_as_this_machine():
    request = _request()
    request.scope["client"] = ("testclient", 1)
    assert owner_auth.worker_peer(request) is True
    request.scope.pop("client")
    assert owner_auth.worker_peer(request) is True


# ---------------------------------------------------------------------------
# Real sockets, called from an agent-environment child process
# ---------------------------------------------------------------------------

CHILD = r"""
import http.client, json, sys
results = []
for case in json.loads(sys.argv[1]):
    conn = http.client.HTTPConnection(case["connect"], case["port"], timeout=15)
    body = None if case.get("body") is None else json.dumps(case["body"]).encode()
    conn.putrequest(case["method"], case["path"], skip_host=True, skip_accept_encoding=True)
    for name, value in case.get("headers", {}).items():
        conn.putheader(name, value)
    if body is not None:
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    results.append({"status": response.status, "body": response.read().decode("utf-8", "replace")})
    conn.close()
print(json.dumps(results))
"""


def _non_loopback_ipv4():
    try:
        import psutil
        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                if entry.family == socket.AF_INET and not ipaddress.ip_address(entry.address).is_loopback:
                    return entry.address
    except (ImportError, OSError):
        return None
    return None


@pytest.fixture
def owner_stack(monkeypatch):
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda *args: False)
    monkeypatch.setattr(auth.auth_service, "is_setup_required", lambda: False)
    monkeypatch.setattr(auth.auth_service, "get_admin_username", lambda: "Owner")
    from app.core import cluster
    monkeypatch.setattr(cluster, "observe_local_advertise_url", lambda request: None)
    app = FastAPI()
    app.add_middleware(CSRFProtectionMiddleware)
    app.include_router(auth.router)
    app.include_router(owner_login.router)

    @app.get("/owner-probe")
    def owner_read(request: Request, principal=Depends(auth.require_admin)):
        return {"principal": principal}

    @app.post("/owner-probe")
    def owner_write(request: Request, principal=Depends(auth.require_admin)):
        return {"principal": principal}

    @app.post("/auth-probe")
    def any_auth_write(request: Request, principal=Depends(auth.require_auth)):
        return {"principal": principal}

    sockets = [bind_listener("127.0.0.1", 0)]
    port = sockets[0].getsockname()[1]
    addresses = {"ipv4": "127.0.0.1"}
    for name, address in (("ipv6", "::1"), ("own", _non_loopback_ipv4())):
        if not address:
            continue
        try:
            sockets.append(bind_listener(address, port))
            addresses[name] = address
        except OSError:
            pass
    server = uvicorn.Server(uvicorn.Config(IngressApplication(app, port), proxy_headers=False,
                                           log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield port, addresses
    finally:
        server.should_exit = True
        thread.join(15)
        for sock in sockets:
            sock.close()


def _as_agent(cases):
    """Run the cases from a child spawned with an agent's environment."""
    from fluid.tools import agent_runner
    from fluid.tools.child_env import scrub_child_env
    argv, cwd, env = agent_runner.wrap([sys.executable, "-I", "-c", CHILD, json.dumps(cases)],
                                       None, scrub_child_env())
    done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _host(address, port):
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _attack_cases(port, addresses):
    cases = []
    for name, address in addresses.items():
        hosts = [_host(address, port)]
        if name == "ipv4":
            hosts.append(f"localhost:{port}")
        for host in hosts:
            same_origin = {"Host": host, "Origin": f"http://{host}"}
            spoofed = {**same_origin, "X-Forwarded-For": "192.168.1.20"}
            for headers in (same_origin, spoofed):
                base = {"connect": address, "port": port, "headers": headers}
                cases += [
                    {**base, "label": f"{name} {host} status", "method": "GET", "path": "/api/v1/auth/status"},
                    {**base, "label": f"{name} {host} owner read", "method": "GET", "path": "/owner-probe"},
                    {**base, "label": f"{name} {host} owner write", "method": "POST", "path": "/owner-probe"},
                    {**base, "label": f"{name} {host} auth write", "method": "POST", "path": "/auth-probe"},
                    {**base, "label": f"{name} {host} trusted lan", "method": "PUT",
                     "path": "/api/v1/owner/trusted-lan", "body": {"enabled": True}},
                ]
    return cases


def test_agent_process_gets_no_owner_access_over_the_worker_sockets(owner_stack, docker_install, monkeypatch):
    port, addresses = owner_stack
    cases = _attack_cases(port, addresses)
    results = _as_agent(cases)
    for case, result in zip(cases, results):
        if case["path"] == "/api/v1/auth/status":
            assert result["status"] == 200, case["label"]
            status = json.loads(result["body"])
            assert status["is_admin"] is False and status["authenticated"] is False, case["label"]
            assert status["local_access"] is False, case["label"]
        else:
            assert result["status"] in (401, 403), (case["label"], result)
    assert owner_auth.trusted_lan() is False

    # Trusted LAN on: the container's own address is still not "the LAN".
    _trust_lan(monkeypatch)
    lan_cases = [c for c in cases if c["path"] in ("/owner-probe", "/auth-probe")]
    for case, result in zip(lan_cases, _as_agent(lan_cases)):
        assert result["status"] in (401, 403), (case["label"], result)


def test_owner_credentials_and_native_loopback_still_work_over_the_same_sockets(
        owner_stack, docker_install, monkeypatch):
    port, _ = owner_stack
    host = f"127.0.0.1:{port}"
    token, _ = owner_auth.issue_session()
    with_bearer = {"Host": host, "Authorization": "Bearer " + token}
    plain = {"Host": host, "Origin": f"http://{host}"}
    cases = [
        {"connect": "127.0.0.1", "port": port, "method": "GET", "path": "/owner-probe", "headers": with_bearer},
        {"connect": "127.0.0.1", "port": port, "method": "POST", "path": "/owner-probe", "headers": with_bearer},
        {"connect": "127.0.0.1", "port": port, "method": "GET", "path": "/api/v1/auth/status", "headers": with_bearer},
    ]
    read, write, status = _as_agent(cases)
    assert read["status"] == 200 and write["status"] == 200
    assert json.loads(status["body"])["is_admin"] is True

    # Control: the same plain request IS the owner on a native install, so the
    # refusals above come from the Docker rule, not from a broken harness.
    monkeypatch.setattr(owner_auth, "container_install", lambda: False)
    native = _as_agent([{"connect": "127.0.0.1", "port": port, "method": "POST", "path": "/owner-probe",
                         "headers": plain}])
    assert native[0]["status"] == 200

