"""Cryptographic primitives for the worker network.

Every choice here has a WebCrypto twin so the account hub (Cloudflare
Workers) and a future phone client can verify and unwrap the same bytes:

- Signatures: Ed25519 over the canonical JSON of an envelope.
- Payloads: AES-256-GCM, 12-byte random nonce, envelope header as AAD.
- Key wrapping: X25519 ephemeral-static agreement, HKDF-SHA256, AES-256-GCM.
- Recovery wrap: HKDF-SHA256 over the 128-bit recovery key, AES-256-GCM.
- Fingerprints: SHA-256 of the raw Ed25519 public key, shown in groups.

Keys are raw 32-byte values, carried as unpadded base64url text. The test
vectors in ``tests/core/network`` are checked by both the Python suite and
``tools/scripts/verify-network-vectors.mjs`` (Node WebCrypto).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32
WRAP_INFO = b"shuffle-network-key-wrap-v1"
RECOVERY_INFO = b"shuffle-network-recovery-v1"
TOKEN_INFO = b"shuffle-device-token-v1"


class CryptoError(ValueError):
    """Any verification or decryption failure. Carries no key material."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or any(ch in text for ch in "+/=\n\r "):
        raise CryptoError("Invalid base64url text")
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise CryptoError("Invalid base64url text") from exc


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON both sides can reproduce.

    Keys are sorted; there is no whitespace; non-ASCII text is kept as UTF-8
    (as ``JSON.stringify`` does). Only ASCII keys are allowed so the Python
    code-point sort and the JavaScript UTF-16 sort agree, and floats are
    rejected so no formatting ambiguity exists.
    """
    _check_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _check_canonical(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) >= 2 ** 53:
            raise CryptoError("Integer exceeds the safe JavaScript range")
        return
    if isinstance(value, float):
        raise CryptoError("Floats are not allowed in signed structures")
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_canonical(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise CryptoError("Signed structures need ASCII keys")
            _check_canonical(item)
        return
    raise CryptoError("Unsupported value in signed structure: " + type(value).__name__)


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Ed25519 signing
# ---------------------------------------------------------------------------

def generate_signing_key() -> bytes:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def signing_public_key(private: bytes) -> bytes:
    return _signing_private(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _signing_private(private: bytes) -> ed25519.Ed25519PrivateKey:
    if not isinstance(private, (bytes, bytearray)) or len(private) != KEY_BYTES:
        raise CryptoError("Invalid signing key")
    return ed25519.Ed25519PrivateKey.from_private_bytes(bytes(private))


def sign(private: bytes, message: bytes) -> bytes:
    return _signing_private(private).sign(message)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if not isinstance(public, (bytes, bytearray)) or len(public) != KEY_BYTES:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        return False
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes(public)).verify(bytes(signature), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def fingerprint(public_signing_key: bytes) -> str:
    """Human-comparable fingerprint: 8 groups of 4 hex characters (128 bits)."""
    digest = hashlib.sha256(public_signing_key).hexdigest()[:32]
    return "-".join(digest[i:i + 4] for i in range(0, 32, 4))


def fingerprint_matches(public_signing_key: bytes, shown: str) -> bool:
    expected = fingerprint(public_signing_key).replace("-", "").lower()
    given = "".join(ch for ch in (shown or "").lower() if ch in "0123456789abcdef")
    return bool(given) and hmac.compare_digest(expected, given)


# ---------------------------------------------------------------------------
# X25519 agreement and key wrapping
# ---------------------------------------------------------------------------

def generate_encryption_key() -> bytes:
    return x25519.X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def encryption_public_key(private: bytes) -> bytes:
    return _encryption_private(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _encryption_private(private: bytes) -> x25519.X25519PrivateKey:
    if not isinstance(private, (bytes, bytearray)) or len(private) != KEY_BYTES:
        raise CryptoError("Invalid encryption key")
    return x25519.X25519PrivateKey.from_private_bytes(bytes(private))


def _encryption_public(public: bytes) -> x25519.X25519PublicKey:
    if not isinstance(public, (bytes, bytearray)) or len(public) != KEY_BYTES:
        raise CryptoError("Invalid encryption public key")
    return x25519.X25519PublicKey.from_public_bytes(bytes(public))


def _hkdf(secret: bytes, info: bytes, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(secret)


def wrap_key(recipient_public: bytes, key: bytes) -> dict:
    """Wrap ``key`` so only the holder of the X25519 private key can unwrap it.

    An ephemeral X25519 key agrees with the recipient's static key; the
    shared secret goes through HKDF-SHA256 with both public keys in ``info``
    so a wrap is bound to exactly one recipient. Output fields are base64url.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise CryptoError("Only 32-byte keys can be wrapped")
    recipient = _encryption_public(recipient_public)
    ephemeral = x25519.X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(recipient)
    kek = _hkdf(shared, WRAP_INFO + ephemeral_public + bytes(recipient_public))
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(kek).encrypt(nonce, bytes(key), ephemeral_public)
    return {
        "v": PROTOCOL_VERSION,
        "alg": "X25519-HKDF-SHA256-A256GCM",
        "epk": b64u_encode(ephemeral_public),
        "nonce": b64u_encode(nonce),
        "ct": b64u_encode(ciphertext),
    }


def unwrap_key(recipient_private: bytes, wrapped: Mapping[str, Any]) -> bytes:
    try:
        if wrapped.get("v") != PROTOCOL_VERSION or wrapped.get("alg") != "X25519-HKDF-SHA256-A256GCM":
            raise CryptoError("Unsupported key wrap")
        ephemeral_public = b64u_decode(wrapped["epk"])
        nonce = b64u_decode(wrapped["nonce"])
        ciphertext = b64u_decode(wrapped["ct"])
    except (KeyError, AttributeError, TypeError) as exc:
        raise CryptoError("Malformed key wrap") from exc
    private = _encryption_private(recipient_private)
    recipient_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = private.exchange(_encryption_public(ephemeral_public))
    kek = _hkdf(shared, WRAP_INFO + ephemeral_public + recipient_public)
    try:
        key = AESGCM(kek).decrypt(nonce, ciphertext, ephemeral_public)
    except InvalidTag as exc:
        raise CryptoError("Key wrap does not open with this device key") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError("Unwrapped key has the wrong size")
    return key


# ---------------------------------------------------------------------------
# Symmetric payload encryption
# ---------------------------------------------------------------------------

def generate_network_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def encrypt_payload(network_key: bytes, plaintext: bytes, aad: bytes) -> str:
    """AES-256-GCM. Returns base64url of ``nonce || ciphertext || tag``."""
    if not isinstance(network_key, (bytes, bytearray)) or len(network_key) != KEY_BYTES:
        raise CryptoError("Invalid network key")
    nonce = os.urandom(NONCE_BYTES)
    return b64u_encode(nonce + AESGCM(bytes(network_key)).encrypt(nonce, plaintext, aad))


def decrypt_payload(network_key: bytes, blob: str, aad: bytes) -> bytes:
    if not isinstance(network_key, (bytes, bytearray)) or len(network_key) != KEY_BYTES:
        raise CryptoError("Invalid network key")
    raw = b64u_decode(blob)
    if len(raw) < NONCE_BYTES + 16:
        raise CryptoError("Ciphertext too short")
    try:
        return AESGCM(bytes(network_key)).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise CryptoError("Payload does not decrypt with this network key") from exc


# ---------------------------------------------------------------------------
# Recovery key
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_FIX = {"O": "0", "I": "1", "L": "1"}
RECOVERY_BYTES = 16  # 128 bits of entropy shown as 26 base32 characters


def generate_recovery_key() -> bytes:
    return secrets.token_bytes(RECOVERY_BYTES)


def format_recovery_key(raw: bytes) -> str:
    """``A3-KQ7X4-...`` style text: version prefix plus 26 Crockford base32 chars."""
    if len(raw) != RECOVERY_BYTES:
        raise CryptoError("Invalid recovery key")
    bits = int.from_bytes(raw, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[bits & 31])
        bits >>= 5
    text = "".join(reversed(chars))
    groups = [text[i:i + 5] for i in range(0, 25, 5)] + [text[25:]]
    return "A1-" + "-".join(groups)


def parse_recovery_key(text: str) -> bytes:
    cleaned = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if not cleaned.startswith("A1"):
        raise CryptoError("Unrecognized recovery key version")
    body = cleaned[2:]
    if len(body) != 26:
        raise CryptoError("Recovery key must have 26 characters")
    bits = 0
    for ch in body:
        ch = _CROCKFORD_FIX.get(ch, ch)
        index = _CROCKFORD.find(ch)
        if index < 0:
            raise CryptoError("Recovery key contains an invalid character")
        bits = (bits << 5) | index
    if bits >> (RECOVERY_BYTES * 8):
        raise CryptoError("Recovery key is out of range")
    return bits.to_bytes(RECOVERY_BYTES, "big")


def wrap_key_with_recovery(recovery_key: bytes, network_id: str, key: bytes) -> dict:
    """Wrap the network key under a key derived from the recovery key.

    128 bits of true entropy need no slow KDF; HKDF-SHA256 keeps this
    reproducible in WebCrypto. The network id is the salt so one recovery key
    can never open another network's wrap.
    """
    if len(recovery_key) != RECOVERY_BYTES or len(key) != KEY_BYTES:
        raise CryptoError("Invalid key sizes for recovery wrap")
    kek = _hkdf(recovery_key, RECOVERY_INFO, salt=network_id.encode("utf-8"))
    nonce = os.urandom(NONCE_BYTES)
    return {
        "v": PROTOCOL_VERSION,
        "alg": "HKDF-SHA256-A256GCM",
        "nonce": b64u_encode(nonce),
        "ct": b64u_encode(AESGCM(kek).encrypt(nonce, key, network_id.encode("utf-8"))),
    }


def unwrap_key_with_recovery(recovery_key: bytes, network_id: str, wrapped: Mapping[str, Any]) -> bytes:
    if len(recovery_key) != RECOVERY_BYTES:
        raise CryptoError("Invalid recovery key")
    try:
        if wrapped.get("v") != PROTOCOL_VERSION or wrapped.get("alg") != "HKDF-SHA256-A256GCM":
            raise CryptoError("Unsupported recovery wrap")
        nonce = b64u_decode(wrapped["nonce"])
        ciphertext = b64u_decode(wrapped["ct"])
    except (KeyError, AttributeError, TypeError) as exc:
        raise CryptoError("Malformed recovery wrap") from exc
    kek = _hkdf(recovery_key, RECOVERY_INFO, salt=network_id.encode("utf-8"))
    try:
        key = AESGCM(kek).decrypt(nonce, ciphertext, network_id.encode("utf-8"))
    except InvalidTag as exc:
        raise CryptoError("Recovery key does not open this network") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError("Unwrapped key has the wrong size")
    return key


def recovery_key_check(recovery_key: bytes, network_id: str) -> str:
    """Public check value the hub may store to reject a mistyped key early."""
    return sha256_hex(_hkdf(recovery_key, RECOVERY_INFO + b"-check", salt=network_id.encode("utf-8")))


# ---------------------------------------------------------------------------
# Device tokens (HMAC over a challenge, keyed from a shared secret)
# ---------------------------------------------------------------------------

def derive_token_key(shared_secret: bytes, salt: bytes) -> bytes:
    return _hkdf(shared_secret, TOKEN_INFO, salt=salt)


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))
