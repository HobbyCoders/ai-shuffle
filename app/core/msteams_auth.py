"""Public-cloud Bot Framework channel authentication (never emulator/skill tokens)."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
CLOCK_SKEW = 60


class InvalidBotToken(ValueError):
    """Authentication failed; do not expose token or key details to callers."""


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


class BotFrameworkAuth:
    def __init__(self, ttl: float = 3600):
        self.ttl = ttl
        self._keys = {}
        self._expires = 0.0
        self._unknown_refresh = 0.0
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> dict:
        async with self._lock:
            now = time.monotonic()
            expired = now >= self._expires
            unknown = kid not in self._keys
            # Unknown key IDs trigger refresh, but untrusted tokens cannot force
            # an unbounded stream of requests to Microsoft's metadata service.
            if expired or (unknown and now >= self._unknown_refresh):
                if unknown and not expired:
                    self._unknown_refresh = now + 30
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.get(OPENID_URL)
                    response.raise_for_status()
                    metadata = response.json()
                    uri = urlsplit(metadata["jwks_uri"])
                    if (metadata.get("issuer") != ISSUER or uri.scheme != "https"
                            or uri.hostname != "login.botframework.com"
                            or uri.username or uri.password or uri.port not in (None, 443)
                            or uri.query or uri.fragment):
                        raise InvalidBotToken("Invalid trust metadata")
                    response = await client.get(metadata["jwks_uri"])
                    response.raise_for_status()
                    keys = response.json()["keys"]
                self._keys = {key["kid"]: key for key in keys if key.get("kty") == "RSA"}
                self._expires = now + self.ttl
            if kid not in self._keys:
                raise InvalidBotToken("Unknown signing key")
            return self._keys[kid]

    async def validate(self, authorization: str, activity: dict, app_id: str,
                       tenant_id: str = "botframework.com") -> None:
        try:
            scheme, token = authorization.split(" ", 1)
            if scheme.lower() != "bearer" or len(token) > 32768 or not app_id:
                raise ValueError("Invalid bearer")
            header_part, payload_part, signature = token.split(".")
            header = json.loads(_decode(header_part))
            claims = json.loads(_decode(payload_part))
            if header.get("alg") != "RS256" or header.get("crit"):
                raise ValueError("Invalid algorithm")
            kid = header["kid"]
            if not isinstance(kid, str) or not kid:
                raise ValueError("Missing key id")
            key = await self._key(kid)
            if key.get("use", "sig") != "sig" or key.get("alg", "RS256") != "RS256":
                raise ValueError("Invalid key")
            numbers = rsa.RSAPublicNumbers(
                int.from_bytes(_decode(key["e"]), "big"),
                int.from_bytes(_decode(key["n"]), "big"),
            )
            numbers.public_key().verify(_decode(signature),
                f"{header_part}.{payload_part}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
            now = time.time()
            for name in ("exp", "nbf"):
                if type(claims[name]) not in (int, float) or not math.isfinite(claims[name]):
                    raise ValueError("Invalid time")
            if (claims.get("iss") != ISSUER or claims.get("aud") != app_id
                    or claims["exp"] <= now - CLOCK_SKEW or claims["nbf"] > now + CLOCK_SKEW):
                raise ValueError("Invalid claims")
            endorsements = key.get("endorsements")
            if (activity.get("channelId") != "msteams" or claims.get("channelid", "msteams") != "msteams"
                    or not isinstance(endorsements, list) or "msteams" not in endorsements):
                raise ValueError("Channel not endorsed")
            from app.core.connectors.msteams import validate_service_url
            service_url = activity["serviceUrl"]
            validate_service_url(service_url)
            if claims.get("serviceurl") != service_url:
                raise ValueError("Service URL mismatch")
            if tenant_id != "botframework.com":
                # Channel JWTs have no tenant claim; the authenticated activity
                # carries the tenant. Require both Teams representations to agree.
                tenant = activity.get("channelData", {}).get("tenant", {}).get("id")
                conversation_tenant = activity.get("conversation", {}).get("tenantId", tenant)
                if tenant != tenant_id or conversation_tenant != tenant_id:
                    raise ValueError("Wrong Teams tenant")
        except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature) as exc:
            raise InvalidBotToken("Invalid Bot Framework authentication") from exc


bot_framework_auth = BotFrameworkAuth()
