"""A linked device as the browser is one: a P-256 key the worker never holds."""
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.network import crypto, terminal_device_links as links


class Device:
    def __init__(self, label="Office PC browser"):
        self.label = label
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.raw = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint)
        self.public_key = crypto.b64u_encode(self.raw)
        self.key_id = links.key_id_of(self.raw)
        self.code = links.link_code(self.raw)

    def sign(self, payload_text):
        r, s = decode_dss_signature(self.key.sign(payload_text.encode("utf-8"), ec.ECDSA(hashes.SHA256())))
        return crypto.b64u_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def proof(self, challenge, *, receiver="destination", via="source", action=links.ACTION_OPEN,
              session_id=None, cwd=None, shell=None, signer=None, **overrides):
        fields = {"purpose": links.PURPOSE, "receiver": receiver, "via": via, "challenge": challenge, "action": action,
                  "target": {"session_id": session_id, "cwd": cwd, "shell": shell}, **overrides}
        payload = json.dumps(fields, separators=(",", ":"))
        return {"key_id": self.key_id, "payload": payload, "signature": (signer or self).sign(payload)}
