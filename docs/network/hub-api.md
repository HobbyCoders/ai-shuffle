# Account hub: network API contract

The account hub is the Cloudflare Worker at `MOBILE_ACCOUNT_HUB_URL`. Its
production source is `android/hub`: D1 stores accounts, linked servers, network
admission state, opaque recovery wraps, and the append-only encrypted feed.
Migration `0007_network_feed.sql` creates the network tables; the existing `DB`
D1 binding and deployment migration step apply them. This file is the contract
the worker-side client in `app/network/hub.py` implements and the in-memory
reference implementation used by the tests (`tests/core/network/fake_hub.py`)
follows.

The hub is zero-knowledge for payloads. It stores ciphertext envelopes,
cursors, device descriptions (public keys, names, platforms) and the public
recovery wrap. It never sees a network key. Sizes, timing, device ids and
network ids are visible to it.

## Authentication

Every route below is under `/v1/servers/{server_id}/network...` and is
authenticated exactly like the existing server routes: `Authorization: Bearer
<server secret>` (the secret issued at worker linking). The server must be
active and not revoked; its immutable account binding selects that account's
single network. A secret cannot address another server or account. Summary and
approval pickup are available to the linked server for bootstrap; pending-list,
admission, recovery writes, feed push, and feed pull additionally require that
server's network device to be `active`.

Responses are JSON objects. Errors use HTTP status codes; bodies are never
echoed back by the worker.

## Data model (D1)

```
networks         id, account_id (unique), network_id, founder_device_id,
                 generation, recovery_json, cursor_head, stored_ops,
                 stored_bytes, daily_upload_day, daily_upload_bytes
network_devices  network_hub_id, device_id, server_id, signing_public,
                 encryption_public, name, platform, kind, incarnation, version,
                 status ('pending'|'approved'|'active'|'rejected'|'revoked'),
                 approval_json (one-time join response)
network_feed     network_hub_id, cursor, origin_device_id, origin_incarnation,
                 origin_seq, envelope (opaque JSON ciphertext), bytes
```

Feed storage is D1-backed and append-only. Cursors are dense and scoped to one
network; envelopes are deduplicated by `(origin_device_id, origin_incarnation,
origin_seq)` and returned in committed cursor order. D1 currently retains the
entire feed until a quota is reached, so `snapshot_required` is always `false`.
No Durable Object or R2 binding is required by this implementation.

## Routes

### `POST .../network` — register this device

Body:
```json
{
  "device": {"device_id": "dev_…", "signing_public": "b64u", "encryption_public": "b64u",
             "incarnation": 1, "name": "Desk", "platform": "desktop", "kind": "worker", "version": "0.8.6"},
  "network_id": "net_…",            // present when the worker already belongs to a network
  "founder_device_id": "dev_…",     // idem
  "generation": 3,                  // idem
  "recovery": { …public recovery wrap… }   // optional
}
```

Rules:
- Account has no network and the body names one: create it; the device is
  `active`. Reply `{"status": "active", "network_hub_id": "…", "quota": {…}}`.
- Account has no network and the body names none (a fresh worker): record the
  device as `pending`; reply `{"status": "pending"}`. It becomes the network's
  founder only if no other worker creates the network first; the client keeps
  polling `approval`.
- Body names the account's network: mark the device `active`, bump the
  generation to the maximum seen; reply `active`.
- Body names no network but the account has one: the device is `pending`
  until an online member admits it (`devices/{id}/approve`; members do this
  on their own in the sync loop, the account sign-in being the
  authorization) or it unlocks with the recovery key. Reply
  `{"status": "pending"}` (or `approved` once an approval waits for pickup).
- Body names a network that is not the account's: nothing is stored; reply
  `{"status": "conflict", "network_id": "<the account's network>"}`. The
  worker's owner chooses to adopt the account's network (the worker leaves
  its own and registers again without one) or to keep it apart.

### `GET .../network` — network summary

```json
{"network_id": "net_…", "name": null, "founder_device_id": "dev_…", "generation": 3,
 "recovery": {…} | null, "devices": [ {device description + status} ], "cursor_head": 1234,
 "quota": {"ops": 1234, "limit": 200000}}
```

### `GET .../network/pending` — devices waiting for approval

`{"devices": [ {device description, status: "pending"} ]}`

### `POST .../network/devices/{device_id}/approve`

Body `{"response": <join response>}` where the join response is what
`engine.join_response` produces: network id, founder, generation, key version,
`keyring` (every key version wrapped to the device's X25519 key),
`certificate`, `membership_records`, `members`, `legacy_instance_map`,
`issuer`. The hub stores it for one pickup and marks the device `approved`.

### `POST .../network/devices/{device_id}/reject`

Marks the device `rejected`.

### `GET .../network/approval` — the calling device's own approval

`{"status": "pending"|"approved"|"rejected"|"unknown", "response": {…}}`. The
response is returned once and deleted; the device becomes `active`.

### `POST .../network/ops` — push envelopes

Body `{"ops": [envelope, …]}` (at most 200 per call, 8 MiB). Reply
`{"accepted": n, "cursor": head, "quota": {…}}`, or `{"quota_exceeded": true, …}`
when storage or the daily upload limit is exhausted. Duplicates are skipped
silently. Ops are appended in the order received; a worker pushes in its own
ingest order, so the feed is causally ordered per origin.

### `GET .../network/feed?cursor=N&limit=500` — pull envelopes

`{"ops": [envelope, …], "cursor": N', "more": bool, "snapshot_required": bool}`.
Replaying from cursor 0 rebuilds a network: the founder's self-signed
certificate is the first membership operation, so a device that unlocked with
the recovery key can verify the whole chain from nothing.

### `POST .../network/recovery`

Body `{"recovery": <public recovery wrap>}`: `{v, network_id, key_version,
wrapped: {alg, nonce, ct}, check, created_at, created_by}`. Stored on the
network; returned in the summary so a fresh device can unlock offline of every
other worker. An active device may explicitly clear stale recovery material with
`{"recovery": null}`; subsequent summaries return `recovery: null`.

### `POST .../network/blobs` and `GET .../network/blobs/{hash}` (later)

Two-phase publish of content-addressed bytes: request an upload URL, upload,
then commit the reference. Not needed for the current milestones; blobs are
served worker to worker.

## Quotas

Per network: 200,000 stored envelopes, 512 MiB stored ciphertext JSON, and
64 MiB of newly accepted ciphertext JSON per UTC day. The D1 quota trigger
checks an upload atomically: if any new envelope would exceed a quota, it rolls
back the whole batch and the hub answers `quota_exceeded` with `accepted: 0`.
Duplicates do not consume quota. Workers keep syncing with each other and show
the cloud backlog in Settings > Devices.

## Failure cases the worker handles

| Situation | Worker behaviour |
|---|---|
| Hub unreachable | Retries on the loop cadence; LAN sync unaffected; `last_error` shown. |
| 401/403 | Binding treated as revoked by the existing account code; loop idles. |
| Quota exceeded | Backlog counted from `pushed_ingest_id`; nothing is dropped. |
| `snapshot_required` | Reserved for a future retention migration; the current D1 feed always returns `false`. |
| Approval response for another device id | Rejected by `enroll_from_response` (the certificate must admit this device). |
| Feed envelope that fails verification | Rejected and counted; never advances the origin's watermark past it. |
