"""The operation envelope every synced record travels in.

An envelope is what leaves a worker: a small signed header in the clear and
the record itself as AES-GCM ciphertext under the network key. Everything a
receiver needs to order, deduplicate, verify and route an operation is in the
header; nothing about the record's content is.

Header (all fields signed; the AAD for the ciphertext is the header minus
``ciphertext``, ``signature`` and ``written_at``):

    v                   protocol version (1)
    network_id          the network this operation belongs to
    collection          logical collection name (see ``policy``)
    record_id           record key inside the collection
    op                  "upsert" or "delete"
    clock               Lamport clock of the writing device at write time
    origin_device_id    the device that wrote it
    origin_incarnation  bumps when a device is restored from a backup
    origin_seq          dense per-(device, incarnation) sequence number
    order_seq           owner-ordered collections only: the owner's own seq
    schema_version      record shape version for the collection
    key_version         which network key encrypted the payload
    content_hash        sha256 of the plaintext record (hex); "" for deletes
    written_at          wall clock for display only
    ciphertext          base64url(nonce || ciphertext || tag), "" for deletes
    signature           Ed25519 over canonical JSON of every other field
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.network import crypto

ENVELOPE_VERSION = 1
OP_UPSERT = "upsert"
OP_DELETE = "delete"
OPS = (OP_UPSERT, OP_DELETE)

_HEADER_FIELDS = (
    "v", "network_id", "collection", "record_id", "op", "clock", "origin_device_id",
    "origin_incarnation", "origin_seq", "order_seq", "schema_version", "key_version",
    "content_hash", "written_at", "ciphertext",
)
_AAD_EXCLUDED = {"ciphertext", "signature", "written_at"}
_ID_LIMIT = 512


class EnvelopeError(ValueError):
    """Structural problem with an envelope. Never carries plaintext."""


def record_bytes(record: Any) -> bytes:
    """Deterministic plaintext for a record. Tolerates floats and dates."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default).encode("utf-8")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return crypto.b64u_encode(bytes(value))
    return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Envelope:
    network_id: str
    collection: str
    record_id: str
    op: str
    clock: int
    origin_device_id: str
    origin_incarnation: int
    origin_seq: int
    schema_version: int
    key_version: int
    content_hash: str = ""
    ciphertext: str = ""
    order_seq: Optional[int] = None
    written_at: str = field(default_factory=now_iso)
    signature: str = ""
    v: int = ENVELOPE_VERSION

    # -- serialization -------------------------------------------------
    def header(self) -> dict:
        return {name: getattr(self, name) for name in _HEADER_FIELDS}

    def aad(self) -> bytes:
        return crypto.canonical_json({k: v for k, v in self.header().items() if k not in _AAD_EXCLUDED})

    def signing_bytes(self) -> bytes:
        return crypto.canonical_json(self.header())

    def to_dict(self) -> dict:
        data = self.header()
        data["signature"] = self.signature
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Envelope":
        if not isinstance(data, Mapping):
            raise EnvelopeError("Envelope must be an object")
        unknown = set(data) - set(_HEADER_FIELDS) - {"signature"}
        if unknown:
            raise EnvelopeError("Envelope has unknown fields")
        try:
            env = cls(
                v=_int(data.get("v"), "v"),
                network_id=_text(data.get("network_id"), "network_id"),
                collection=_text(data.get("collection"), "collection"),
                record_id=_text(data.get("record_id"), "record_id"),
                op=_text(data.get("op"), "op"),
                clock=_int(data.get("clock"), "clock"),
                origin_device_id=_text(data.get("origin_device_id"), "origin_device_id"),
                origin_incarnation=_int(data.get("origin_incarnation"), "origin_incarnation"),
                origin_seq=_int(data.get("origin_seq"), "origin_seq"),
                order_seq=None if data.get("order_seq") is None else _int(data.get("order_seq"), "order_seq"),
                schema_version=_int(data.get("schema_version"), "schema_version"),
                key_version=_int(data.get("key_version"), "key_version"),
                content_hash=_text(data.get("content_hash", ""), "content_hash", allow_empty=True),
                written_at=_text(data.get("written_at", ""), "written_at", allow_empty=True),
                ciphertext=_text(data.get("ciphertext", ""), "ciphertext", allow_empty=True),
                signature=_text(data.get("signature", ""), "signature", allow_empty=True),
            )
        except EnvelopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - structural guard
            raise EnvelopeError("Malformed envelope") from exc
        env.validate_shape()
        return env

    # -- checks ----------------------------------------------------------
    def validate_shape(self) -> None:
        if self.v != ENVELOPE_VERSION:
            raise EnvelopeError("Unsupported envelope version")
        if self.op not in OPS:
            raise EnvelopeError("Unsupported operation")
        if self.clock < 0 or self.origin_seq < 1 or self.origin_incarnation < 1:
            raise EnvelopeError("Envelope counters out of range")
        if self.schema_version < 1 or self.key_version < 1:
            raise EnvelopeError("Envelope versions out of range")
        if self.op == OP_UPSERT and (not self.ciphertext or not self.content_hash):
            raise EnvelopeError("Upsert without payload")
        if self.op == OP_DELETE and self.ciphertext:
            raise EnvelopeError("Delete carries a payload")
        if self.order_seq is not None and self.order_seq < 0:
            raise EnvelopeError("Envelope counters out of range")

    def identity(self) -> tuple:
        return (self.origin_device_id, self.origin_incarnation, self.origin_seq)

    # -- crypto ----------------------------------------------------------
    def sign(self, signing_private: bytes) -> "Envelope":
        self.signature = crypto.b64u_encode(crypto.sign(signing_private, self.signing_bytes()))
        return self

    def verify_signature(self, signing_public: bytes) -> bool:
        try:
            return crypto.verify(signing_public, self.signing_bytes(), crypto.b64u_decode(self.signature))
        except crypto.CryptoError:
            return False

    def seal(self, network_key: bytes, record: Any) -> "Envelope":
        plaintext = record_bytes(record)
        self.content_hash = crypto.sha256_hex(plaintext)
        self.ciphertext = crypto.encrypt_payload(network_key, plaintext, self.aad())
        return self

    def open(self, network_key: bytes) -> Any:
        if self.op == OP_DELETE:
            return None
        plaintext = crypto.decrypt_payload(network_key, self.ciphertext, self.aad())
        if crypto.sha256_hex(plaintext) != self.content_hash:
            raise EnvelopeError("Record hash mismatch")
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EnvelopeError("Record is not JSON") from exc


def _text(value, name, *, allow_empty=False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > max(_ID_LIMIT, 1 << 22):
        raise EnvelopeError("Invalid envelope field: " + name)
    if name in ("network_id", "collection", "record_id", "origin_device_id", "op") and len(value) > _ID_LIMIT:
        raise EnvelopeError("Invalid envelope field: " + name)
    return value


def _int(value, name) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeError("Invalid envelope field: " + name)
    return value


def build(*, network_id: str, collection: str, record_id: str, op: str, clock: int,
          origin_device_id: str, origin_incarnation: int, origin_seq: int, schema_version: int,
          key_version: int, network_key: bytes, signing_private: bytes, record: Any = None,
          order_seq: Optional[int] = None) -> Envelope:
    """Seal and sign a record in one step (the writer's side of ``sync.record``)."""
    env = Envelope(network_id=network_id, collection=collection, record_id=record_id, op=op,
                   clock=clock, origin_device_id=origin_device_id, origin_incarnation=origin_incarnation,
                   origin_seq=origin_seq, schema_version=schema_version, key_version=key_version,
                   order_seq=order_seq)
    if op == OP_UPSERT:
        env.seal(network_key, record)
    env.validate_shape()
    return env.sign(signing_private)


def compare(clock_a: int, device_a: str, clock_b: int, device_b: str) -> int:
    """Last-writer-wins order: higher clock wins; equal clocks break ties on device id.

    Returns 1 when (a) wins, -1 when (b) wins, 0 when they are the same writer.
    """
    if clock_a != clock_b:
        return 1 if clock_a > clock_b else -1
    if device_a == device_b:
        return 0
    return 1 if device_a > device_b else -1
