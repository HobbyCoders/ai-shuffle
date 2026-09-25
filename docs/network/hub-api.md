# Account hub: network API contract

The account hub is the Cloudflare Worker at `MOBILE_ACCOUNT_HUB_URL`. Its
production source is `android/hub`: D1 stores accounts, linked servers, network
admission state and opaque recovery wraps. Worker data never passes through the
hub: workers sync directly with each other ([peer-first.md](peer-first.md)).
Migration `0007_network_feed.sql` created the network tables. This file is the contract
the worker-side client in `app/network/hub.py` implements and the in-memory
reference implementation used by the tests (`tests/core/network/fake_hub.py`)
follows.

The hub stores device descriptions (public keys, names, platforms), one-time
join responses whose keys are wrapped to the joining device, and the public
recovery wrap. It never sees a network key or any synced data. Timing, device
ids and network ids are visible to it.

The encrypted operation feed (`/ops`, `/feed`) and the cloud object backup
(`/objects/*`) were retired in #1264 and return 404. Migration 0017 drops their
D1 tables (`network_feed`, `network_objects`, `network_object_streams`) once
the purge in [hub-legacy-purge.md](hub-legacy-purge.md) is done. See
[object-sync.md](object-sync.md) for their history.

## Authentication

Every route below is under `/v1/servers/{server_id}/network...` and is
authenticated exactly like the existing server routes: `Authorization: Bearer
<server secret>` (the secret issued at worker linking). The server must be
active and not revoked; its immutable account binding selects that account's
single network. A secret cannot address another server or account. Summary and
approval pickup are available to the linked server for bootstrap; pending-list,
admission and recovery writes additionally require that server's network device
to be `active`. Network routes take no query string.

Responses are JSON objects. Errors use HTTP status codes. Bodies are never
echoed back by the worker.

## Data model (D1)

```
networks         id, account_id (unique), network_id, founder_device_id,
                 generation, name, recovery_json
                 (cursor_head, stored_ops, stored_bytes and daily_upload_* are
                 unused leftovers of the retired feed)
network_devices  network_hub_id, device_id, server_id, signing_public,
                 encryption_public, name, platform, kind, incarnation, version,
                 legacy_instance_id, proof,
                 status ('pending'|'approved'|'active'|'rejected'|'revoked'),
                 approval_json (one-time join response)
```

No Durable Object or R2 binding is used.

## Routes

### `POST .../network` — register this device

Body:
```json
{
  "device": {"device_id": "dev_…", "signing_public": "b64u", "encryption_public": "b64u",
             "incarnation": 1, "name": "Desk", "platform": "desktop", "kind": "worker", "version": "0.8.6",
             "proof": "b64u"},   // Ed25519 signature by the device's own signing key over
                                // device_id, signing_public, encryption_public and kind
                                // (app.network.membership.sign_device_description); stored
                                // and returned verbatim, never checked by the hub
  "network_id": "net_…",            // present when the worker already belongs to a network
  "founder_device_id": "dev_…",     // idem
  "generation": 3,                  // idem
  "recovery": { …public recovery wrap… }   // optional
}
```

Rules:
- Account has no network and the body names one: create it; the device is
  `active`. Reply `{"status": "active", "network_hub_id": "…"}`.
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
  account always wins: the worker leaves its own network and registers again
  without one (becoming `pending` until a member admits it).

### `GET .../network` — network summary

```json
{"network_id": "net_…", "name": null, "founder_device_id": "dev_…", "generation": 3,
 "recovery": {…} | null,
 "devices": [ {device description, "status": "…", "endpoints": ["https://…"]} ]}
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

Body `{}` rejects a `pending` device: it is marked `rejected`, and any other
status answers 404. A member posts `{"reason": "revoked"}` for a device it
removed from the network (`engine.revoke_device`). That notice marks a
pending, approved or active device `rejected`, is acknowledged for a device
already rejected or purged, and answers 409 when a device names itself. A
worker that could not reach the hub at the time retries from
`revocations_due` in its hub state on later passes.

Both unattended and manual admission require a valid description `proof`.
A description without one (an older worker, or a relay that dropped the field)
remains pending until the worker updates and re-registers. Comparing the
signing-key fingerprint cannot substitute for proof binding both public keys.

### `GET .../network/approval` — the calling device's own approval

`{"status": "pending"|"approved"|"rejected"|"unknown", "response": {…}}`. The
response is returned once and deleted; the device becomes `active`.

### `POST .../network/recovery`

Body `{"recovery": <public recovery wrap>}`: `{v, network_id, key_version,
wrapped: {alg, nonce, ct}, check, created_at, created_by}`. Stored on the
network; returned in the summary so a fresh device can unlock offline of every
other worker. An active device may explicitly clear stale recovery material with
`{"recovery": null}`; subsequent summaries return `recovery: null`.

## Limits

The hub enforces these bounds so one account cannot fill the shared D1
database (`android/hub/src/network.ts`):

| Bound | Value | Error |
|---|---|---|
| Request body on any `.../network` route | 128 KiB | 413 `Body too large (limit 128 KiB)` |
| `response` in `devices/{id}/approve` (stored as `approval_json`) | 64 KiB of JSON | 413 `response is too large (limit 64 KiB)` |
| `recovery` in `POST .../network` or `POST .../network/recovery` | 16 KiB of JSON | 413 `recovery is too large (limit 16 KiB)` |
| Devices per network | 50 | 409 `Network device limit reached (50 devices); remove a worker first` |
| Pending or rejected device rows | Deleted 30 days after their last update | None; the hourly cron removes them |

The device cap counts rows that are not `rejected` and whose linked server is
not revoked, so removing a worker frees its slot. A device already on the list
can always register again (renames, restarts). A join response grows by about
1 KiB per member plus one certificate record per past admission, so a
50-device network fits in 64 KiB but a network with a long admission history
may not. A larger response fails admission with 413 rather than being
truncated. A pending device that keeps polling refreshes
its row on each registration, so only abandoned requests are purged; a purged
worker that comes back simply registers again as `pending`.

All `/v1/servers/{id}/...` routes also share the hub rate limit: 120 requests a
minute per worker (server id and secret), plus a looser 1,200 a minute per
client IP. Over the limit the hub answers 429 with `Retry-After`.

## Failure cases the worker handles

| Situation | Worker behaviour |
|---|---|
| Hub unreachable | Retries on the loop cadence; LAN sync unaffected; `last_error` shown. |
| 401/403 | Binding treated as revoked by the existing account code; loop idles. |
| 429 or 503 with `Retry-After` | Backs off for at least that long; a running connector stays up (see `android/hub/README.md`, Cost controls). |
| Approval response for another device id | Rejected by `enroll_from_response` (the certificate must admit this device). |
