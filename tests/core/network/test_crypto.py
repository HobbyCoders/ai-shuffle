"""Primitives round-trip, reject tampering, and match the shared vectors."""
import json
import os
from pathlib import Path

import pytest

from app.network import crypto

VECTORS = Path(__file__).with_name("vectors.json")


def test_base64url_round_trip_and_rejects_padding():
    raw = os.urandom(37)
    text = crypto.b64u_encode(raw)
    assert "=" not in text and "+" not in text and "/" not in text
    assert crypto.b64u_decode(text) == raw
    with pytest.raises(crypto.CryptoError):
        crypto.b64u_decode(text + "=")


def test_canonical_json_is_sorted_compact_and_utf8():
    value = {"b": 1, "a": {"z": [1, 2, {"y": "é"}], "x": None, "w": True}}
    assert crypto.canonical_json(value) == '{"a":{"w":true,"x":null,"z":[1,2,{"y":"é"}]},"b":1}'.encode("utf-8")
    with pytest.raises(crypto.CryptoError):
        crypto.canonical_json({"a": 1.5})
    with pytest.raises(crypto.CryptoError):
        crypto.canonical_json({"a": 2 ** 53})
    with pytest.raises(crypto.CryptoError):
        crypto.canonical_json({"é": 1})


def test_sign_and_verify():
    private = crypto.generate_signing_key()
    public = crypto.signing_public_key(private)
    message = b"hello network"
    signature = crypto.sign(private, message)
    assert crypto.verify(public, message, signature)
    assert not crypto.verify(public, message + b"!", signature)
    assert not crypto.verify(public, message, bytes(64))
    assert not crypto.verify(b"short", message, signature)


def test_fingerprint_format_and_comparison():
    public = crypto.signing_public_key(crypto.generate_signing_key())
    shown = crypto.fingerprint(public)
    assert len(shown) == 39 and shown.count("-") == 7
    assert crypto.fingerprint_matches(public, shown)
    assert crypto.fingerprint_matches(public, shown.replace("-", " ").upper())
    assert not crypto.fingerprint_matches(public, shown[:-1] + ("0" if shown[-1] != "0" else "1"))
    assert not crypto.fingerprint_matches(public, "")


def test_wrap_and_unwrap_network_key():
    device = crypto.generate_encryption_key()
    other = crypto.generate_encryption_key()
    network_key = crypto.generate_network_key()
    wrapped = crypto.wrap_key(crypto.encryption_public_key(device), network_key)
    assert crypto.unwrap_key(device, wrapped) == network_key
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(other, wrapped)
    tampered = dict(wrapped, ct=crypto.b64u_encode(bytes(len(crypto.b64u_decode(wrapped["ct"])))))
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(device, tampered)
    with pytest.raises(crypto.CryptoError):
        crypto.wrap_key(crypto.encryption_public_key(device), b"too short")


def test_payload_encryption_binds_aad():
    key = crypto.generate_network_key()
    blob = crypto.encrypt_payload(key, b'{"sidebarWidth":320}', b"header")
    assert crypto.decrypt_payload(key, blob, b"header") == b'{"sidebarWidth":320}'
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_payload(key, blob, b"other header")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_payload(crypto.generate_network_key(), blob, b"header")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_payload(key, crypto.b64u_encode(b"tiny"), b"header")


def test_recovery_key_text_round_trip():
    raw = crypto.generate_recovery_key()
    text = crypto.format_recovery_key(raw)
    assert text.startswith("A1-") and len(text.replace("-", "")) == 28
    assert crypto.parse_recovery_key(text) == raw
    assert crypto.parse_recovery_key(text.lower().replace("-", " ")) == raw
    # Crockford confusables are tolerated.
    assert crypto.parse_recovery_key(text.replace("0", "O")) == raw
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recovery_key("B1-" + text[3:])
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recovery_key(text[:-1])
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recovery_key(text[:-1] + "U")


def test_recovery_wrap_is_bound_to_the_network():
    recovery = crypto.generate_recovery_key()
    network_key = crypto.generate_network_key()
    wrapped = crypto.wrap_key_with_recovery(recovery, "net_a", network_key)
    assert crypto.unwrap_key_with_recovery(recovery, "net_a", wrapped) == network_key
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key_with_recovery(recovery, "net_b", wrapped)
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key_with_recovery(crypto.generate_recovery_key(), "net_a", wrapped)
    assert crypto.recovery_key_check(recovery, "net_a") != crypto.recovery_key_check(recovery, "net_b")


def test_shared_vectors_verify_in_python():
    """The same file is checked by tools/scripts/verify-network-vectors.mjs."""
    vectors = json.loads(VECTORS.read_text("utf-8"))
    assert vectors["version"] == crypto.PROTOCOL_VERSION

    signing = vectors["signing"]
    private = crypto.b64u_decode(signing["private"])
    public = crypto.b64u_decode(signing["public"])
    assert crypto.signing_public_key(private) == public
    assert crypto.fingerprint(public) == signing["fingerprint"]
    for case in signing["cases"]:
        message = crypto.canonical_json(case["message"])
        assert message == case["canonical"].encode("utf-8")
        assert crypto.b64u_encode(crypto.sign(private, message)) == case["signature"]
        assert crypto.verify(public, message, crypto.b64u_decode(case["signature"]))

    payload = vectors["payload"]
    key = crypto.b64u_decode(payload["network_key"])
    for case in payload["cases"]:
        plaintext = crypto.decrypt_payload(key, case["blob"], case["aad"].encode("utf-8"))
        assert plaintext == case["plaintext"].encode("utf-8")

    wrap = vectors["wrap"]
    device_private = crypto.b64u_decode(wrap["recipient_private"])
    assert crypto.encryption_public_key(device_private) == crypto.b64u_decode(wrap["recipient_public"])
    assert crypto.unwrap_key(device_private, wrap["wrapped"]) == crypto.b64u_decode(wrap["key"])

    recovery = vectors["recovery"]
    raw = crypto.parse_recovery_key(recovery["text"])
    assert crypto.b64u_encode(raw) == recovery["raw"]
    assert crypto.format_recovery_key(raw) == recovery["text"]
    assert crypto.unwrap_key_with_recovery(raw, recovery["network_id"], recovery["wrapped"]) == \
        crypto.b64u_decode(recovery["key"])
    assert crypto.recovery_key_check(raw, recovery["network_id"]) == recovery["check"]
