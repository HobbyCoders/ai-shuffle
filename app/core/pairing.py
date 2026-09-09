"""Pairing-code generation, hashing, endpoint selection, and QR rendering."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from app.core import cluster
from app.core.config import settings
from app.db import pairing as pairing_db


PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PAIRING_CODE_LENGTH = 12
PAIRING_TTL_MINUTES = 5
PAIRING_MAX_ATTEMPTS = 10
PAIRED_SESSION_PREFIX = "shc_"
DEFAULT_CONTROLLER_SCOPES = ["controller"]
RUNNER_ENROLL_SCOPES = ["runner:enroll"]
NETWORK_JOIN_SCOPES = ["network:join"]
PAIRING_PURPOSES = {"controller", "runner", "network:join"}
DEFAULT_MOBILE_SCOPES = [
    "controller",
    "mobile:notifications",
    "mobile:actions",
    "web:session",
]


def generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def normalize_pairing_code(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "", (value or "").upper())
    if len(normalized) != PAIRING_CODE_LENGTH:
        return ""
    if any(char not in PAIRING_ALPHABET for char in normalized):
        return ""
    return normalized


def format_pairing_code(value: str) -> str:
    normalized = normalize_pairing_code(value)
    if not normalized:
        return value
    return "-".join(normalized[index:index + 4] for index in range(0, 12, 4))


PAIRING_KEY_SETTING = "network_pairing_key"


def pairing_key() -> str:
    """The local key pairing codes are hashed under.

    Generated once per database (``network_pairing_key`` in system_settings,
    a local-only key the sync policy never shares). It replaces the
    shared-database cluster secret, so rotating or retiring that secret no
    longer invalidates outstanding pairing codes.
    """
    from app.db import database
    return database.get_or_create_system_setting(PAIRING_KEY_SETTING, lambda: secrets.token_urlsafe(32))


def hash_pairing_code(value: str) -> str:
    normalized = normalize_pairing_code(value)
    if not normalized:
        # Hashing a fixed invalid marker keeps the exchange path uniform without
        # risking an accidental match against a real grant.
        normalized = "INVALID-PAIRING-CODE"
    return hmac.new(
        pairing_key().encode("utf-8"),
        f"shuffle-pairing-v1:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_controller_session() -> str:
    return f"{PAIRED_SESSION_PREFIX}{secrets.token_urlsafe(32)}"


def hash_controller_session(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sanitize_endpoint_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Pairing endpoint must be an http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("Pairing endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Pairing endpoint must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def classify_endpoint(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    loopback = host in {"localhost", "localhost.localdomain"}
    private = False
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
        private = address.is_private and not loopback
    except ValueError:
        pass

    if loopback:
        kind = "loopback"
        label = "This device only"
    elif host.endswith(".ts.net"):
        kind = "tailscale"
        label = "Tailscale"
    elif private or host.endswith(".local"):
        kind = "lan"
        label = "Local network"
    elif parsed.scheme == "https":
        kind = "https"
        label = "HTTPS"
    else:
        kind = "custom"
        label = "Custom address"
    return {
        "url": url.rstrip("/"),
        "kind": kind,
        "label": label,
        "secure": parsed.scheme == "https",
        "loopback": loopback,
        "qr_eligible": not loopback,
    }


def pairing_endpoints(request_base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    candidates: List[tuple[str, str]] = []
    if request_base_url:
        try:
            candidates.append((sanitize_endpoint_url(request_base_url) or "", "current"))
        except ValueError:
            pass
    advertised, _ = cluster.advertise_url()
    if advertised:
        try:
            candidates.append((sanitize_endpoint_url(advertised) or "", "advertised"))
        except ValueError:
            pass

    endpoints: List[Dict[str, Any]] = []
    seen = set()
    for url, source in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        item = classify_endpoint(url)
        item["source"] = source
        endpoints.append(item)
    endpoints.sort(key=lambda item: (item["loopback"], not item["secure"]))
    return endpoints


def build_pairing_url(endpoint_url: Optional[str], code: str) -> Optional[str]:
    endpoint = sanitize_endpoint_url(endpoint_url)
    if not endpoint:
        return None
    return f"{endpoint}/pair#token={normalize_pairing_code(code)}"


def render_qr_svg(value: str) -> str:
    """Render a local SVG QR; no token is sent to an external service."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    return image.to_string(encoding="unicode")


def terminal_qr(value: str) -> str:
    """Return a compact two-row-per-character QR suitable for a terminal."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    lines = []
    for row in range(0, len(matrix), 2):
        line = []
        for top, bottom in zip(matrix[row], matrix[row + 1]):
            line.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        lines.append("".join(line))
    return "\n".join(lines)


def create_grant(
    *,
    endpoint_url: Optional[str],
    device_label: Optional[str] = None,
    created_by: str = "admin",
    scopes: Optional[List[str]] = None,
    purpose: str = "controller",
) -> Dict[str, Any]:
    if purpose not in PAIRING_PURPOSES:
        raise ValueError("Unsupported pairing purpose")
    endpoint = sanitize_endpoint_url(endpoint_url) if purpose in ("controller", "network:join") else None
    code = generate_pairing_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=PAIRING_TTL_MINUTES)
    grant = pairing_db.create_pairing_grant(
        token_hash=hash_pairing_code(code),
        purpose=purpose,
        scopes=scopes or (
            RUNNER_ENROLL_SCOPES
            if purpose == "runner"
            else NETWORK_JOIN_SCOPES
            if purpose == "network:join"
            else DEFAULT_CONTROLLER_SCOPES + DEFAULT_MOBILE_SCOPES[1:]
        ),
        target_install_id=settings.effective_instance_id,
        endpoint_url=endpoint,
        device_label=(device_label or "").strip() or None,
        created_by=created_by,
        expires_at=expires_at,
        max_attempts=PAIRING_MAX_ATTEMPTS,
    )
    url = build_pairing_url(endpoint, code) if purpose == "controller" else None
    join_link = None
    if purpose == "network:join":
        from app.network.enrollment import build_join_link
        join_link = build_join_link(endpoint, code)
        url = join_link["url"] if join_link else None
    return {
        "grant": grant,
        "code": code,
        "display_code": format_pairing_code(code),
        "pairing_url": url,
        "join": join_link,
        "qr_svg": render_qr_svg(url) if url and (purpose == "network:join" or not classify_endpoint(endpoint or "")["loopback"]) else None,
    }
