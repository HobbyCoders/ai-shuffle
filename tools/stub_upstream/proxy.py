"""Optional HTTPS MITM proxy in front of the stub. TEST / DEV TOOLING ONLY.

WHY THIS EXISTS
---------------
``app/core/translator_gateway/upstream.py`` hardcodes its upstream endpoints::

    CODEX_RESPONSES_URL  = "https://chatgpt.com/backend-api/codex/responses"
    OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
    ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

A model-provider row's ``base_url`` steers only the ``GET {base_url}/v1/models``
catalog fetch — it does NOT steer chat traffic. So there is no in-app knob that
points a running instance at a loopback stub.

The gateway's outbound client is ``httpx.AsyncClient(...)`` with ``trust_env``
left at its default of ``True`` (see ``app/core/http_client.py``), and httpx
0.27.2 honors both the standard proxy env vars and ``SSL_CERT_FILE`` (see
``httpx/_config.py::load_ssl_context_verify`` -> ``get_ca_bundle_from_env``).
That gives a zero-app-change interception point:

    HTTPS_PROXY=http://127.0.0.1:8798
    SSL_CERT_FILE=<ca-dir>/stub-upstream-ca.pem
    NO_PROXY=127.0.0.1,localhost

The proxy answers ``CONNECT``, terminates TLS with an on-the-fly leaf cert
signed by its own throwaway CA, and then serves the ordinary stub handler. It
NEVER dials out — a hostname it does not recognize still gets a stub response,
which is exactly the property that makes the whole harness zero-spend.

SAFETY: the CA is generated locally, lives only in ``--ca-dir``, and is trusted
only by processes you explicitly point at it with ``SSL_CERT_FILE``. Do not
install it into a system trust store.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools.stub_upstream.server import DEFAULT_PROXY_PORT, StubHandlerMixin, StubState

CA_CERT_NAME = "stub-upstream-ca.pem"
CA_KEY_NAME = "stub-upstream-ca.key"


# ─── Throwaway CA + on-demand leaf certs ─────────────────────────────────────

def ensure_ca(ca_dir: str) -> Tuple[str, str]:
    """Create (or reuse) the local CA in ``ca_dir``; return ``(cert, key)`` paths."""
    os.makedirs(ca_dir, exist_ok=True)
    cert_path = os.path.join(ca_dir, CA_CERT_NAME)
    key_path = os.path.join(ca_dir, CA_KEY_NAME)
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "AI Shuffle stub-upstream CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ai-shuffle-dev-only"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class CertFactory:
    """Mints and caches a leaf cert (+ SSLContext) per intercepted hostname."""

    def __init__(self, ca_dir: str) -> None:
        self.ca_dir = ca_dir
        self.cert_path, self.key_path = ensure_ca(ca_dir)
        with open(self.cert_path, "rb") as fh:
            self._ca_cert = x509.load_pem_x509_certificate(fh.read())
        with open(self.key_path, "rb") as fh:
            self._ca_key = serialization.load_pem_private_key(fh.read(), password=None)
        self._leaf_dir = os.path.join(ca_dir, "leaf")
        os.makedirs(self._leaf_dir, exist_ok=True)
        self._contexts: Dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    def context_for(self, hostname: str) -> ssl.SSLContext:
        with self._lock:
            ctx = self._contexts.get(hostname)
            if ctx is None:
                ctx = self._build_context(hostname)
                self._contexts[hostname] = ctx
            return ctx

    def _build_context(self, hostname: str) -> ssl.SSLContext:
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in hostname)
        cert_path = os.path.join(self._leaf_dir, f"{safe}.pem")
        key_path = os.path.join(self._leaf_dir, f"{safe}.key")
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            self._mint_leaf(hostname, cert_path, key_path)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return ctx

    def _mint_leaf(self, hostname: str, cert_path: str, key_path: str) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        try:
            san = x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(hostname))]
            )
        except ValueError:
            san = x509.SubjectAlternativeName([x509.DNSName(hostname)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])])
            )
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )
        with open(key_path, "wb") as fh:
            fh.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
        with open(cert_path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))


# ─── Proxy handler ───────────────────────────────────────────────────────────

class ProxyHandler(StubHandlerMixin, BaseHTTPRequestHandler):
    """Terminates ``CONNECT`` tunnels, then serves the ordinary stub routes."""

    @property
    def certs(self) -> CertFactory:
        return self.server.certs  # type: ignore[attr-defined]

    def do_CONNECT(self):  # noqa: N802
        host, _, port = self.path.partition(":")
        host = host.strip()
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        try:
            tls = self.certs.context_for(host).wrap_socket(
                self.connection, server_side=True
            )
        except (ssl.SSLError, OSError):
            return

        self.connection = tls
        self.rfile = tls.makefile("rb", self.rbufsize)
        self.wfile = tls.makefile("wb", 0)
        self.upstream_host = f"{host}:{port}" if port else host
        self.close_connection = False
        try:
            while not self.close_connection:
                self.handle_one_request()
        except (ssl.SSLError, OSError):
            pass
        finally:
            try:
                tls.close()
            except OSError:
                pass


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, state: StubState, certs: CertFactory):
        self.state = state
        self.certs = certs
        super().__init__(addr, ProxyHandler)


def serve_proxy(
    *,
    port: int = DEFAULT_PROXY_PORT,
    state: StubState,
    ca_dir: str,
    host: str = "127.0.0.1",
) -> Tuple[ProxyServer, threading.Thread, str]:
    """Start the MITM proxy on a background thread. Returns the CA cert path."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(f"refusing to bind {host!r}: the proxy is loopback-only")
    certs = CertFactory(ca_dir)
    httpd = ProxyServer((host, port), state, certs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, certs.cert_path


def find_ca(ca_dir: Optional[str] = None) -> Optional[str]:
    """Path to an existing CA cert, if one has been generated."""
    ca_dir = ca_dir or os.path.join(os.getcwd(), ".stub-upstream-ca")
    path = os.path.join(ca_dir, CA_CERT_NAME)
    return path if os.path.exists(path) else None
