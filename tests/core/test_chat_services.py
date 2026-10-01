"""HTTPS service grants: the credential is consumed by the broker, never shown to the model."""

import asyncio
import base64
import datetime
import ipaddress
import json
import ssl
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import chat_access, chat_files, chat_services
from app.core.chat_access import AccessDenied, Broker, GrantInput, GrantSubmission
from app.core.chat_services import Origin

SECRET = "svc-test-only-token-0123456789/+="


def _certificate(subject, issuer_name, key, issuer_key, *, ca):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("Test CA", "Test CA", ca_key, ca_key, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _certificate("127.0.0.1", "Test CA", leaf_key, ca_key, ca=False)
    folder = tmp_path_factory.mktemp("pki")
    (folder / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (folder / "leaf.key").write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca.public_bytes(serialization.Encoding.PEM).decode(), folder


@pytest.fixture
def trust(pki, monkeypatch):
    """Trust only the test CA, as the system store trusts public CAs in production."""
    ca_pem, _ = pki
    monkeypatch.setattr(
        chat_services, "tls_context", lambda: ssl.create_default_context(cadata=ca_pem)
    )


class Service:
    def __init__(self):
        self.requests = []
        self.connections = 0
        self.held = asyncio.Event()
        self.dripping = asyncio.Event()
        self.drip_closed = asyncio.Event()
        self.port = 0

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "body": body.decode(),
            }
        )
        extra = {}
        status = "200 OK"
        if target.startswith("/api/echo"):
            payload = json.dumps(
                {
                    "headers": headers,
                    "basic": base64.b64encode(SECRET.encode()).decode(),
                    "escaped": json.dumps(SECRET),
                }
            ).encode()
        elif target.startswith("/api/redirect"):
            status = "302 Found"
            extra = {
                "Location": f"https://elsewhere.invalid/next?token={SECRET}",
                "Set-Cookie": f"session={SECRET}",
                "X-Debug": SECRET,
            }
            payload = b""
        elif target.startswith("/api/big"):
            # The secret straddles the 64 KiB boundary and repeats before it.
            payload = (
                (SECRET * 10).encode()
                + b"x" * (chat_files.LIMIT - 10 * len(SECRET) - 5)
                + SECRET.encode() * 4
            )
        elif target.startswith("/api/hold"):
            self.held.set()
            await asyncio.sleep(60)
            payload = b""
        elif target.startswith("/api/drip"):
            # Each byte arrives well inside the per-read timeout, so only an overall
            # deadline ends the request. The body would take far longer than any test.
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 100000\r\nConnection: close\r\n\r\n"
            )
            self.dripping.set()
            try:
                for _ in range(100000):
                    writer.write(b".")
                    await writer.drain()
                    if reader.at_eof():
                        break
                    await asyncio.sleep(0.02)
            except (ConnectionError, ssl.SSLError):
                pass
            finally:
                self.drip_closed.set()
                writer.close()
            return
        else:
            payload = json.dumps({"method": method, "target": target}).encode()
        response = [
            f"HTTP/1.1 {status}",
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Connection: close",
            *(f"{k}: {v}" for k, v in extra.items()),
        ]
        writer.write(("\r\n".join(response) + "\r\n\r\n").encode() + payload)
        try:
            await writer.drain()
        finally:
            writer.close()


@pytest_asyncio.fixture
async def service(pki):
    _, folder = pki
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(folder / "leaf.pem", folder / "leaf.key")
    target = Service()
    server = await asyncio.start_server(target.handle, "127.0.0.1", 0, ssl=context)
    target.port = server.sockets[0].getsockname()[1]
    try:
        yield target
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(
        chat_access, "session_owner", AsyncMock(return_value=("local", "worker"))
    )
    from app.core import chat_vault

    monkeypatch.setattr(chat_vault, "catalog", lambda owner, **kwargs: [])
    return Broker()


def spec(service, *, writable=False, prefix="/api", **overrides):
    return GrantInput(
        kind="service",
        label="Test API",
        url=f"https://127.0.0.1:{service.port}{prefix}",
        writable=writable,
        secret=SECRET,
        **overrides,
    )


async def pending(broker):
    async with asyncio.timeout(3):
        while not broker.actions:
            await asyncio.sleep(0.001)
    return next(iter(broker.actions.values()))


# --- Pure validation -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pw@example.com",
        "https://example.com/?q=1",
        "https://example.com/#x",
        "https://example.com:99999",
        "https://example.com:0",
        "https://example.com:00",
        "https://example.com:-1",
        "https://example.com:+443",
        "https://169.254.169.254",
        "https://0.0.0.0",
        "https://exa mple.com",
        "https://-bad-.com",
        "ftp://example.com",
        "https://example.com/a/../b",
        "https://example.com/a/..;/b",
        "https://example.com/a%2Fb",
        "https:\\\\example.com",
        "",
    ],
)
def test_origin_refuses_non_https_credentials_and_ambiguous_addresses(url):
    with pytest.raises(AccessDenied):
        chat_services.parse_origin(url)


def test_origin_normalises_host_port_and_prefix():
    assert chat_services.parse_origin("https://API.Example.com/v1/") == Origin(
        "https://api.example.com/v1", "api.example.com", 443, "/v1"
    )
    assert chat_services.parse_origin("https://10.0.0.5:8443") == Origin(
        "https://10.0.0.5:8443", "10.0.0.5", 8443, ""
    )
    assert (
        chat_services.parse_origin("https://[::1]:9443/x").url == "https://[::1]:9443/x"
    )


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/../x",
        "/v1/../../admin",
        "/%2e%2e/admin",
        "/a/%2E%2E/b",
        "/a%2fb",
        "/a%5cb",
        "/a\\b",
        "//evil.example/x",
        "/a//b",
        "/x#frag",
        "/x y",
        "/a?b?c",
        "/a/./b",
        "/%00",
        "",
        # Path parameters: servlet containers strip ";..." and then resolve dots.
        "/..;/admin",
        "/x/..;jsessionid=1/..;/admin",
        "/%2e%2e;/admin",
        "/..%3b/admin",
        "/.;/x",
        "/;x/admin",
    ],
)
def test_request_paths_cannot_leave_the_granted_prefix(path):
    origin = chat_services.parse_origin("https://api.example.com/v1")
    with pytest.raises(AccessDenied):
        chat_services.request_url(origin, path)


def test_request_paths_stay_on_origin_under_prefix():
    origin = chat_services.parse_origin("https://api.example.com:8443/v1")
    assert chat_services.request_url(origin, "/") == "https://api.example.com:8443/v1/"
    assert (
        chat_services.request_url(origin, "/items/7?expand=1&q=a%20b")
        == "https://api.example.com:8443/v1/items/7?expand=1&q=a%20b"
    )
    assert (
        chat_services.request_url(origin, "/items/")
        == "https://api.example.com:8443/v1/items/"
    )
    assert (
        chat_services.request_url(origin, "/@evil.example/x")
        == "https://api.example.com:8443/v1/@evil.example/x"
    )


@pytest.mark.parametrize(
    "name, scheme, secret",
    [
        ("Host", "", SECRET),
        ("Content-Length", "", SECRET),
        ("Proxy-Authorization", "", SECRET),
        ("Bad Header", "", SECRET),
        ("Authorization", "Two words", SECRET),
        ("Authorization", "Bearer", "short"),
        ("Authorization", "Bearer", "line\r\nX-Injected: 1"),
        ("Authorization", "Bearer", "has space inside"),
    ],
)
def test_credential_header_refuses_injection_and_routing_headers(name, scheme, secret):
    with pytest.raises(AccessDenied):
        chat_services.credential_header(name, scheme, secret)


def test_credential_header_forms():
    assert chat_services.credential_header("Authorization", "Bearer", SECRET) == (
        "Authorization",
        f"Bearer {SECRET}",
    )
    assert chat_services.credential_header("X-API-Key", "", SECRET) == (
        "X-API-Key",
        SECRET,
    )


def test_redaction_counts_positions_in_the_input_so_partial_secrets_never_slide_into_view():
    needles = chat_services._needles(SECRET, f"Bearer {SECRET}")
    data = SECRET.encode() * 20 + SECRET.encode()[:10]
    limit = 20 * len(SECRET)
    redacted = chat_services.redact(data, needles, limit)
    assert redacted == b"[redacted]" * 20
    for needle in needles:
        assert needle not in redacted
    basic = base64.b64encode(b"admin:correct-horse-battery").decode()
    needles = chat_services._needles(basic, f"Basic {basic}")
    echoed = b"user=admin:correct-horse-battery pw=correct-horse-battery"
    assert b"correct-horse" not in chat_services.redact(echoed, needles)


# --- Broker, real TLS service --------------------------------------------------


async def test_fresh_session_has_no_service_access_and_grant_returns_only_a_handle(
    broker, service, trust
):
    assert (await broker.snapshot("chat"))["grants"] == []
    with pytest.raises(AccessDenied):
        await broker.run("chat", "fabricated", "http", {"method": "GET", "path": "/"})
    request = asyncio.create_task(
        broker.request("chat", "service", "Read the inventory API")
    )
    await asyncio.sleep(0)
    pending_request = next(iter(broker.requests.values()))
    await broker.fulfill(
        "chat", pending_request.id, GrantSubmission(connection=spec(service))
    )
    handle = await request
    assert (
        set(handle) == {"id", "kind", "expires_at", "writable"}
        and handle["kind"] == "service"
    )
    assert SECRET not in json.dumps(await broker.snapshot("chat"))
    # The grant verified TLS only; no HTTP request or credential was sent.
    assert service.requests == [] and service.connections == 1
    broker.revoke("chat", handle["id"])


async def test_get_attaches_credential_and_redacts_echoes(
    broker, service, trust, monkeypatch
):
    # Worker proxy settings are not inherited by the consumer.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    grant = await broker.create("chat", spec(service))
    result = await broker.run(
        "chat", grant["id"], "http", {"method": "get", "path": "/echo?x=1"}
    )
    sent = service.requests[0]
    assert sent["method"] == "GET" and sent["target"] == "/api/echo?x=1"
    assert sent["headers"]["authorization"] == f"Bearer {SECRET}"
    assert "cookie" not in sent["headers"]
    assert result["status"] == 200 and not broker.actions
    encoded = json.dumps(result)
    assert (
        SECRET not in encoded
        and base64.b64encode(SECRET.encode()).decode() not in encoded
    )
    assert json.dumps(SECRET)[1:-1] not in encoded
    assert "[redacted]" in result["body"]
    broker.revoke("chat", grant["id"])


async def test_custom_header_without_scheme(broker, service, trust):
    grant = await broker.create(
        "chat", spec(service, auth_header="X-API-Key", auth_scheme="")
    )
    await broker.run("chat", grant["id"], "http", {"method": "HEAD", "path": "/status"})
    headers = service.requests[0]["headers"]
    assert headers["x-api-key"] == SECRET and "authorization" not in headers
    broker.revoke("chat", grant["id"])


async def test_redirects_are_returned_not_followed_and_headers_are_filtered(
    broker, service, trust
):
    grant = await broker.create("chat", spec(service))
    result = await broker.run(
        "chat", grant["id"], "http", {"method": "GET", "path": "/redirect"}
    )
    assert len(service.requests) == 1
    assert result["status"] == 302
    assert set(result["headers"]) <= set(chat_services.RESPONSE_HEADERS)
    assert (
        result["headers"]["location"]
        == "https://elsewhere.invalid/next?token=[redacted]"
    )
    assert SECRET not in json.dumps(result)
    broker.revoke("chat", grant["id"])


async def test_large_bodies_are_bounded_and_redacted_across_the_limit(
    broker, service, trust
):
    grant = await broker.create("chat", spec(service))
    result = await broker.run(
        "chat", grant["id"], "http", {"method": "GET", "path": "/big"}
    )
    assert result["truncated"] is True
    assert SECRET[:6] not in result["body"]
    assert result["body"].startswith("[redacted]" * 10)
    broker.revoke("chat", grant["id"])


async def test_read_only_grant_refuses_changes_before_reaching_the_service(
    broker, service, trust
):
    grant = await broker.create("chat", spec(service))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(AccessDenied, match="read-only"):
            await broker.run(
                "chat",
                grant["id"],
                "http",
                {"method": method, "path": "/items", "body": "{}"},
            )
    with pytest.raises(AccessDenied):
        await broker.run(
            "chat",
            grant["id"],
            "http",
            {"method": "GET", "path": "/items", "body": "{}"},
        )
    with pytest.raises(AccessDenied):
        await broker.run(
            "chat", grant["id"], "http", {"method": "TRACE", "path": "/items"}
        )
    with pytest.raises(AccessDenied):
        await broker.run(
            "chat", grant["id"], "http", {"method": "GET", "path": "/../admin"}
        )
    assert service.requests == [] and not broker.actions
    broker.revoke("chat", grant["id"])


@pytest.mark.parametrize(
    "content_type",
    ["text/plain;\r\nX-Injected: 1", "text/plain;\r\n charset=utf-8", "a b", "json"],
)
async def test_content_type_cannot_inject_headers(broker, service, trust, content_type):
    grant = await broker.create("chat", spec(service, writable=True))
    with pytest.raises(AccessDenied, match="content type"):
        await broker.run(
            "chat",
            grant["id"],
            "http",
            {"method": "POST", "path": "/x", "body": "x", "content_type": content_type},
        )
    assert service.requests == [] and not broker.actions
    broker.revoke("chat", grant["id"])


async def test_writable_grant_needs_exact_one_shot_approval(broker, service, trust):
    grant = await broker.create("chat", spec(service, writable=True))
    denied = asyncio.create_task(
        broker.run(
            "chat",
            grant["id"],
            "http",
            {
                "method": "POST",
                "path": "/items",
                "body": '{"n": 1}',
                "content_type": "",
            },
        )
    )
    action = await pending(broker)
    assert action.public()["arguments"] == {
        "method": "POST",
        "path": "/items",
        "body": '{"n": 1}',
        "content_type": "application/json",
    }
    assert SECRET not in json.dumps(action.public())
    await broker.decide("chat", action.id, False)
    with pytest.raises(AccessDenied, match="denied"):
        await denied
    assert service.requests == []
    allowed = asyncio.create_task(
        broker.run(
            "chat",
            grant["id"],
            "http",
            {
                "method": "PUT",
                "path": "/items/1",
                "body": "name=x",
                "content_type": "text/plain; charset=utf-8",
            },
        )
    )
    action = await pending(broker)
    await broker.decide("chat", action.id, True)
    assert (await allowed)["status"] == 200
    assert [(r["method"], r["target"], r["body"]) for r in service.requests] == [
        ("PUT", "/api/items/1", "name=x")
    ]
    assert service.requests[0]["headers"]["content-type"] == "text/plain; charset=utf-8"
    with pytest.raises(AccessDenied):
        await broker.decide("chat", action.id, True)
    broker.revoke("chat", grant["id"])


async def test_bypass_skips_confirmation_but_not_scope(
    broker, service, trust, monkeypatch
):
    monkeypatch.setattr(
        chat_access, "session_bypasses_confirmation", AsyncMock(return_value=True)
    )
    grant = await broker.create("chat", spec(service))
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run(
            "chat", grant["id"], "http", {"method": "DELETE", "path": "/items/1"}
        )
    broker.revoke("chat", grant["id"])
    grant = await broker.create("chat", spec(service, writable=True))
    result = await broker.run(
        "chat", grant["id"], "http", {"method": "DELETE", "path": "/items/1"}
    )
    assert result["status"] == 200 and not broker.actions
    with pytest.raises(AccessDenied):
        await broker.run(
            "chat", grant["id"], "http", {"method": "DELETE", "path": "/../../root"}
        )
    assert len(service.requests) == 1
    broker.revoke("chat", grant["id"])


async def test_untrusted_certificate_never_creates_a_grant_or_sends_the_credential(
    broker, service
):
    # The default trust store does not include the test CA.
    with pytest.raises(AccessDenied, match="certificate"):
        await broker.create("chat", spec(service))
    assert not broker.grants and service.requests == []


async def test_grants_are_session_and_kind_scoped(broker, service, trust, tmp_path):
    grant = await broker.create("chat", spec(service, writable=True))
    with pytest.raises(AccessDenied):
        await broker.run("other", grant["id"], "http", {"method": "GET", "path": "/"})
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "command", {"command": "id"})
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "read", {"path": "x"})
    if chat_files.available():
        folder = await broker.create(
            "chat", GrantInput(kind="folder", label="F", path=str(tmp_path))
        )
        with pytest.raises(AccessDenied):
            await broker.run(
                "chat", folder["id"], "http", {"method": "GET", "path": "/"}
            )
        broker.revoke("chat", folder["id"])
    assert service.requests == []
    broker.revoke("chat", grant["id"])
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "http", {"method": "GET", "path": "/"})
    assert service.requests == []


async def test_revoke_cancels_in_flight_request_and_clears_credential(
    broker, service, trust
):
    grant = await broker.create("chat", spec(service))
    task = asyncio.create_task(
        broker.run("chat", grant["id"], "http", {"method": "GET", "path": "/hold"})
    )
    async with asyncio.timeout(5):
        await service.held.wait()
    held = broker.grants[grant["id"]]
    broker.revoke("chat", grant["id"])
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 3)
    assert held.header is None and held.secret == ""
    assert not broker.grants and len(service.requests) == 1


async def test_slow_drip_response_is_bounded_by_an_overall_deadline(
    broker, service, trust, monkeypatch
):
    monkeypatch.setattr(chat_services, "REQUEST_DEADLINE", 0.5)
    grant = await broker.create("chat", spec(service))
    async with asyncio.timeout(10):
        with pytest.raises(AccessDenied, match="within 0.5 seconds") as denied:
            await broker.run(
                "chat", grant["id"], "http", {"method": "GET", "path": "/drip"}
            )
        # Cancelling at the deadline closed the stream; the service sees it go away.
        await service.drip_closed.wait()
    assert service.dripping.is_set() and len(service.requests) == 1
    message = str(denied.value)
    assert SECRET not in message and "Bearer" not in message
    assert denied.value.__cause__ is None and denied.value.__suppress_context__
    # The grant stays usable after a deadline; nothing is left in flight.
    assert not broker.grants[grant["id"]].tasks
    result = await broker.run(
        "chat", grant["id"], "http", {"method": "GET", "path": "/status"}
    )
    assert result["status"] == 200
    broker.revoke("chat", grant["id"])


async def test_expiry_stops_new_requests(broker, service, trust):
    grant = await broker.create("chat", spec(service))
    broker.grants[grant["id"]].expires_at = 0
    with pytest.raises(AccessDenied, match="expired"):
        await broker.run("chat", grant["id"], "http", {"method": "GET", "path": "/"})
    assert not broker.grants and service.requests == []


async def test_saved_service_needs_a_new_request_and_keeps_its_scope(
    broker, service, trust, monkeypatch
):
    from app.core import chat_vault

    saved = {}

    def save(owner, value):
        saved["d" * 32] = value
        return "d" * 32

    monkeypatch.setattr(chat_vault, "save", save)
    monkeypatch.setattr(chat_vault, "load", lambda owner, saved_id: saved[saved_id])
    first = asyncio.create_task(broker.request("chat", "service", "Initial API access"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    await broker.fulfill(
        "chat",
        request.id,
        GrantSubmission(connection=spec(service), save_connection=True),
    )
    handle = await first
    assert saved["d" * 32].secret.get_secret_value() == SECRET
    broker.revoke("chat", handle["id"])
    # A saved credential is never usable without the owner answering a new request.
    with pytest.raises(AccessDenied):
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/"})
    second = asyncio.create_task(broker.request("chat", "service", "Reuse the API"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    await broker.fulfill(
        "chat", request.id, GrantSubmission(saved_id="d" * 32, minutes=15)
    )
    reused = await second
    assert reused["id"] != handle["id"] and reused["writable"] is False
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run(
            "chat", reused["id"], "http", {"method": "POST", "path": "/x", "body": "{}"}
        )
    result = await broker.run(
        "chat", reused["id"], "http", {"method": "GET", "path": "/echo"}
    )
    assert result["status"] == 200 and SECRET not in json.dumps(result)
    assert service.requests[0]["headers"]["authorization"] == f"Bearer {SECRET}"
    broker.revoke("chat", reused["id"])


# --- Owner API, SDK tool and tool surface -------------------------------------------


async def test_owner_api_grants_service_without_echoing_the_secret(
    broker, service, trust, monkeypatch
):
    from app.api import chat_access as api

    monkeypatch.setattr(api, "broker", broker)
    monkeypatch.setattr(api, "checked", AsyncMock())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        requested = asyncio.create_task(
            broker.request("chat", "service", "Query the API")
        )
        await asyncio.sleep(0)
        endpoint = f"/api/v1/chat-access/chat/requests/{next(iter(broker.requests))}"
        body = {
            "kind": "service",
            "label": "API",
            "url": "http://insecure.example",
            "secret": SECRET,
        }
        bad = await client.post(endpoint, json={"connection": body})
        assert bad.status_code == 400 and SECRET not in bad.text
        bad = await client.post(
            endpoint, json={"connection": {**body, "unknown": SECRET}}
        )
        assert bad.status_code == 422 and SECRET not in bad.text
        body["url"] = f"https://127.0.0.1:{service.port}/api"
        response = await client.post(endpoint, json={"connection": body})
        assert response.status_code == 200, response.text
        handle = await requested
        snapshot = await client.get("/api/v1/chat-access/chat")
        assert SECRET not in snapshot.text
        assert (
            snapshot.json()["grants"][0]["target"]
            == f"https://127.0.0.1:{service.port}/api"
        )
        response = await client.delete(
            f"/api/v1/chat-access/chat/grants/{handle['id']}"
        )
        assert response.status_code == 200


async def test_sdk_tool_returns_redacted_result_and_errors_without_secret(
    broker, service, trust, monkeypatch
):
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp import chat_access_sdk as sdk

    monkeypatch.setattr(sdk, "broker", broker)
    tools = {
        t.name: t
        for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))
    }
    grant = await broker.create("chat", spec(service))
    response = await tools["ChatHttpRequest"].handler(
        {
            "grant_id": grant["id"],
            "method": "GET",
            "path": "/echo",
            "body": "",
            "content_type": "",
        }
    )
    assert not response.get("isError") and SECRET not in json.dumps(response)
    assert json.loads(response["content"][0]["text"])["status"] == 200
    response = await tools["ChatHttpRequest"].handler(
        {
            "grant_id": grant["id"],
            "method": "POST",
            "path": "/echo",
            "body": "{}",
            "content_type": "",
        }
    )
    assert response["isError"] and "read-only" in response["content"][0]["text"]
    resources = await tools["ChatResources"].handler({})
    assert SECRET not in json.dumps(resources) and str(service.port) not in json.dumps(
        resources
    )
    broker.revoke("chat", grant["id"])


def test_service_tool_is_on_every_scoped_surface_and_denied_to_background_work():
    from app.core.provider.fluid_tool_bridge import SERVER_RISK_MAPS
    from app.core.session_modes import (
        CHAT_MODE_RESOURCE_TOOLS,
        CHAT_MODE_TOOL_SURFACE,
        PROACTIVE_DENIED_TOOLS,
    )
    from app.mcp.chat_access_sdk import TOOL_NAMES

    assert "mcp__chat-access__ChatHttpRequest" in CHAT_MODE_RESOURCE_TOOLS
    assert (
        set(TOOL_NAMES) == set(CHAT_MODE_RESOURCE_TOOLS) <= set(CHAT_MODE_TOOL_SURFACE)
    )
    assert "ChatHttpRequest" in PROACTIVE_DENIED_TOOLS
    # The broker's one-shot card is the approval; the generic gate would ask twice.
    assert SERVER_RISK_MAPS["chat-access"]["ChatHttpRequest"] == "safe"
