"""Encrypted, bounded file parts exchanged directly between member devices."""
import os

from app.network import blobs, crypto, engine

PART_BYTES = 2 * 1024 * 1024
MAX_RESPONSE = 3 * 1024 * 1024


def part(digest: str, index: int, recipient: str) -> dict:
    ctx = engine.require_context()
    member = ctx.member(recipient)
    if member is None or member.status != "active" or index < 0:
        raise ValueError("Invalid file recipient or part")
    data = blobs.read_local(digest)
    if data is None:
        raise FileNotFoundError("File not held on this worker")
    offset = index * PART_BYTES
    if len(data) > blobs.MAX_BLOB_BYTES or offset >= max(1, len(data)):
        raise ValueError("Invalid file part")
    header = {"network_id": ctx.network_id, "publisher": ctx.device_id, "recipient": recipient,
              "hash": digest, "index": index, "size": len(data)}
    key = os.urandom(32)
    result = {**header, "key": crypto.wrap_key(member.encryption_public, key),
              "ciphertext": crypto.encrypt_payload(key, data[offset:offset + PART_BYTES], crypto.canonical_json(header))}
    result["signature"] = crypto.b64u_encode(crypto.sign(ctx.identity.signing_private, crypto.canonical_json(result)))
    return result


async def fetch(client, link, digest: str):
    """Returns verified file bytes, never ciphertext or an incomplete file."""
    ctx = engine.require_context()
    data = bytearray()
    index, total = 0, None
    while total is None or len(data) < total:
        await link.authenticate(client, ctx)
        raw = bytearray()
        async with client.stream("GET", link.base_url + f"/api/v1/network/peer/blob/{digest}/parts/{index}",
                                 headers={"Authorization": "Bearer " + link.token}, timeout=120) as response:
            if response.status_code != 200:
                return None
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError("Peer file part is too large")
        import json
        item = json.loads(raw)
        member = engine.require_context().member(link.device_id)
        unsigned = {k: v for k, v in item.items() if k != "signature"}
        if (member is None or member.status != "active"
                or not crypto.verify(member.signing_public, crypto.canonical_json(unsigned), crypto.b64u_decode(item["signature"]))):
            raise ValueError("Peer file signature is invalid")
        header = {k: item[k] for k in ("network_id", "publisher", "recipient", "hash", "index", "size")}
        if (header != {"network_id": ctx.network_id, "publisher": link.device_id, "recipient": ctx.device_id,
                       "hash": digest, "index": index, "size": item["size"]}
                or not 0 <= item["size"] <= blobs.MAX_BLOB_BYTES or total not in (None, item["size"])):
            raise ValueError("Peer file part does not match request")
        total = item["size"]
        key = crypto.unwrap_key(ctx.identity.encryption_private, item["key"])
        clear = crypto.decrypt_payload(key, item["ciphertext"], crypto.canonical_json(header))
        if len(clear) != min(PART_BYTES, total - len(data)):
            raise ValueError("Peer file part is truncated")
        data.extend(clear)
        index += 1
    if crypto.sha256_hex(bytes(data)) != digest:
        raise ValueError("Peer file hash did not verify")
    return bytes(data)
