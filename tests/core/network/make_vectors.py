"""Regenerate vectors.json. Run: venv/bin/python -m tests.core.network.make_vectors

Keys inside are throwaway test material and must never be used anywhere else.
"""
import json
from pathlib import Path

from app.network import crypto


def main():
    signing_private = bytes(range(1, 33))
    signing_public = crypto.signing_public_key(signing_private)
    messages = [
        {"collection": "user_preferences", "op": "upsert", "clock": 7, "record_id": "admin:layout",
         "origin_device_id": "dev_a", "origin_seq": 1, "network_id": "net_test", "key_version": 1,
         "schema_version": 1, "content_hash": "00" * 32},
        {"a": [1, 2, 3], "text": "héllo \"quoted\" \\ back\nline", "nested": {"z": None, "y": True}},
    ]
    signing_cases = []
    for message in messages:
        canonical = crypto.canonical_json(message)
        signing_cases.append({
            "message": message,
            "canonical": canonical.decode("utf-8"),
            "signature": crypto.b64u_encode(crypto.sign(signing_private, canonical)),
        })

    network_key = bytes(range(100, 132))
    payload_cases = []
    for plaintext, aad in ((b'{"sidebarWidth":320}', "hdr-1"), ("ünïcödé".encode("utf-8"), "hdr-2"), (b"", "hdr-3")):
        payload_cases.append({
            "plaintext": plaintext.decode("utf-8"),
            "aad": aad,
            "blob": crypto.encrypt_payload(network_key, plaintext, aad.encode("utf-8")),
        })

    recipient_private = bytes(range(200, 232))
    recipient_public = crypto.encryption_public_key(recipient_private)
    wrapped = crypto.wrap_key(recipient_public, network_key)

    recovery_raw = bytes(range(40, 56))
    recovery_text = crypto.format_recovery_key(recovery_raw)
    recovery_wrapped = crypto.wrap_key_with_recovery(recovery_raw, "net_test", network_key)

    vectors = {
        "version": crypto.PROTOCOL_VERSION,
        "signing": {
            "private": crypto.b64u_encode(signing_private),
            "public": crypto.b64u_encode(signing_public),
            "fingerprint": crypto.fingerprint(signing_public),
            "cases": signing_cases,
        },
        "payload": {"network_key": crypto.b64u_encode(network_key), "cases": payload_cases},
        "wrap": {
            "recipient_private": crypto.b64u_encode(recipient_private),
            "recipient_public": crypto.b64u_encode(recipient_public),
            "key": crypto.b64u_encode(network_key),
            "wrapped": wrapped,
        },
        "recovery": {
            "raw": crypto.b64u_encode(recovery_raw),
            "text": recovery_text,
            "network_id": "net_test",
            "key": crypto.b64u_encode(network_key),
            "wrapped": recovery_wrapped,
            "check": crypto.recovery_key_check(recovery_raw, "net_test"),
        },
    }
    Path(__file__).with_name("vectors.json").write_text(json.dumps(vectors, indent=2, ensure_ascii=False) + "\n", "utf-8")


if __name__ == "__main__":
    main()
