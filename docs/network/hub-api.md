# Account hub: network API contract

The account hub is the Cloudflare Worker at `MOBILE_ACCOUNT_HUB_URL` (D1 for
accounts and devices, one Durable Object per network for the feed, R2 for
segments and blobs). Its source lives outside this repository; this file is
the contract the worker-side client in `app/network/hub.py` implements and
the in-memory reference implementation used by the tests
(`tests/core/network/fake_hub.py`) follows.

The hub is zero-knowledge for payloads. It stores ciphertext envelopes,
cursors, device descriptions (public keys, names, platforms) and the public
recovery wrap. It never sees a network key. Sizes, timing, device ids and
network ids are visible to it.

## Authentication

Every route below is under `/v1/servers/{server_id}/network...` and is
authenticated exactly like the existing server routes: `Authorization: Bearer
<server secret>` (the secret issued at worker linking). The `server_id` binds
the call to one signed-in worker; the hub maps the server to its account and
the account to its single network.

Responses are JSON objects. Errors use HTTP status codes; bodies are never
echoed back by the worker.

## Data model (D1)

```
networks         id, account_id, network_id, founder_device_id, generation,
                 name, recovery_wrap (JSON, public), created_at
network_devices  id, network_id, device_id, server_id, signing_public,
                 encryption_public, name, platform, kind, incarnation,
                 version, status ('pending'|'approved'|'active'|'rejected'|'revoked'),
                 approval (JSON join response, deleted after pickup), created_at
servers          + network_id
```

Feed storage lives in the network's Durable Object: an append-only list of
envelopes with a committed cursor (`cursor` = count of committed envelopes),
deduplicated on `(origin_device_id, origin_incarnation, origin_seq)`. Old
segments compact into R2; a `GET feed` from a cursor older than the retained
window answers `snapshot_required: true` and the worker re-bootstraps from a
member.

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
  until an online member approves it (`devices/{id}/approve`) or it unlocks
  with the recovery key. Reply `{"status": "pending"}` (or `approved` once an
  approval waits for pickup).

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
other worker.

### `POST .../network/blobs` and `GET .../network/blobs/{hash}` (later)

Two-phase publish of content-addressed bytes: request an upload URL, upload,
then commit the reference. Not needed for the current milestones; blobs are
served worker to worker.

## Quotas

Per network: total stored envelopes/bytes and daily upload bytes. Over quota
the hub answers `quota_exceeded`; workers keep syncing with each other and show
the cloud backlog in Settings > Devices.

## Failure cases the worker handles

| Situation | Worker behaviour |
|---|---|
| Hub unreachable | Retries on the loop cadence; LAN sync unaffected; `last_error` shown. |
| 401/403 | Binding treated as revoked by the existing account code; loop idles. |
| Quota exceeded | Backlog counted from `pushed_ingest_id`; nothing is dropped. |
| `snapshot_required` | Worker re-bootstraps from an online member and resumes from the new cursor head. |
| Approval response for another device id | Rejected by `enroll_from_response` (the certificate must admit this device). |
| Feed envelope that fails verification | Rejected and counted; never advances the origin's watermark past it. |
