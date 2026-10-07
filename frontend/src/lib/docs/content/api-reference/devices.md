<!--
title: Devices & Network
description: Worker network, installs, paired phones and browsers, and the Shuffle account link
order: 14
-->

# Devices & Network API

Endpoints behind the **Devices** settings: the network your workers share, the install list the runner picker reads, one-time pairing codes for phones and browsers, and this worker's Shuffle account link.

Endpoints marked **Owner only** reject API-user keys (`aih_...`) with `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

## Network status

Every worker has its own device identity and belongs to at most one network. Workers in a network sync settings, profiles and provider keys, and each session runs on one worker.

### GET /api/v1/network/status

This worker's identity and, when it is in a network, the network summary, members, pending join requests, sync counters, recovery key status and account link status.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/network/status \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** always `device` (`device_id`, `fingerprint`), `enrolled`, `platform`, `version`, `revision` (short running build SHA, or `null` when unknown), `instance_id`. Outside a network it adds `enrollment` (an in-flight join, or `null`). In a network it adds `network` (`network_id`, `name`, `member_count`, `key_version`, ...), `members`, `pending_joins`, `sync` (`records`, `ops`, `holes`, `peers`, `conflicts`), `recovery` and `hub`.

### GET /api/v1/network/policy

What syncs between workers: each synced collection with its fields, which fields are encrypted and which stay local, plus the rules that decide whether a setting is local or shared.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/network/policy \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"collections": [{"collection", "table", "mode", "keys", "fields", "encrypted", "local_fields", "schema_version", "notes"}], "settings": [{"pattern": "...", "sharing": "local"}]}`.

### POST /api/v1/network/create

Start a new network with this worker as its first member.

**Auth:** Owner only

JSON body: `name` (optional, max 100), `device_name` (optional, max 100). Send `{}` to use defaults.

```bash
curl -X POST http://localhost:8000/api/v1/network/create \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Home", "device_name": "Office desktop"}'
```

Returns `201` with `{"network": {...}}`. `409` when this worker is already in a network.

## Members

### GET /api/v1/network/members

List the workers in the network (empty list when this worker is not in one).

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/network/members \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"members": [...]}`. Each member has `device_id`, `name`, `kind`, `platform`, `status` (`active` or `revoked`), `is_self`, `online`, `fingerprint`, `last_seen`, `endpoints`, `version`, `revision` (short running build SHA, or `null` when unknown).

### PATCH /api/v1/network/members/{device_id}

Rename a worker.

**Auth:** Owner only

JSON body: `name` (required, 1-100 chars).

```bash
curl -X PATCH http://localhost:8000/api/v1/network/members/DEVICE_ID \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Laptop"}'
```

Returns the updated member. `404` for an unknown device.

### DELETE /api/v1/network/members/{device_id}

Revoke a worker's access. The network key is rotated. The removed worker's copies of your provider API keys should be treated as compromised, so rotate those keys with each provider.

Sessions the removed worker owned stay here as read-only mirrors: turns, permission answers and stops for them are no longer sent to it (the chat reports the owner as unreachable and offers **Fork here**; see [fork-here](#post-apiv1networksessionssession_idfork-here)). Automations and connectors it owned stay inactive until you give them a new owner with [`POST /api/v1/network/owners`](#definition-owners).

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/network/members/DEVICE_ID \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"device_id": "...", "generation": 4, "key_version": 3, "rotate_provider_keys_recommended": true}`. `400` when a worker tries to remove itself, `404` for an unknown device or when this worker is not in a network.

## Sync conflicts

When two workers change the same record, one change wins and the other is kept here for review until it expires.

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `GET` | `/api/v1/network/conflicts` | none | Owner only. `{"conflicts": [...]}` with `id`, `collection`, `record_id`, `losing_device_id`, `losing_record`, `winning_device_id`, `created_at`, `expires_at`. In `losing_record`, columns that are encrypted at rest (API keys, tokens and other secrets) read `<secret>` |
| `DELETE` | `/api/v1/network/conflicts/{conflict_id}` | none | Owner only. Dismiss one conflict (integer id). `204`, or `404` |

```bash
curl http://localhost:8000/api/v1/network/conflicts \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Runners

### GET /api/v1/network/runners

The workers that can run sessions, for choosing where a session runs.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/network/runners \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"runners": [...], "self_device_id": "...", "network_id": "...", "enrolled": true}`. Each runner has `id`, `device_id`, `device_name`, `platform`, `version`, `online`, `is_self`. Outside a network the list is empty and `enrolled` is `false`.

### POST /api/v1/network/sessions/{session_id}/fork-here

Continue a session whose worker is offline or was removed from the network: creates a new session on this worker with the synced transcript as context (the **Fork here** button in chat). The original session is not changed.

**Auth:** Required. API users can fork only sessions in their scope.

```bash
curl -X POST http://localhost:8000/api/v1/network/sessions/SESSION_ID/fork-here \
  -H "Authorization: Bearer aih_your_key"
```

**Response (`201`):** `{"session_id": "...", "parent_session_id": "...", "context_chars": 18234}`. `404` for an unknown session, `403` outside the API user's scope.

### POST /api/v1/network/runners/{device_id}/sessions/{session_id}/hydrate

Ask the worker that owns a session to publish a transcript older than 30 days, so this worker's mirror can show it.

**Auth:** Required. API users can hydrate only sessions in their scope.

```bash
curl -X POST http://localhost:8000/api/v1/network/runners/DEVICE_ID/sessions/SESSION_ID/hydrate \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"published": 42}`. `503` when the owning worker is offline or was removed from the network, `409` when it refuses (for example, it does not own the session) or this worker is not in a network.

## Definition owners

Each automation and connector runs on one worker, its owner. A definition with no owner is claimed by one online worker; one owned by another device stays inactive on every other worker.

### POST /api/v1/network/owners

Give an automation or connector to a worker, or leave it with no owner.

**Auth:** Owner only

JSON body: `table` (`automations` or `connectors`), `id` (the definition's id), `device_id` (an active worker in the network, or `null`).

```bash
curl -X POST http://localhost:8000/api/v1/network/owners \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"table": "connectors", "id": "conn-1a2b3c4d", "device_id": "DEVICE_ID"}'
```

**Response:** `{"table": "connectors", "id": "conn-1a2b3c4d", "owner_device_id": "DEVICE_ID"}`. `400` when `device_id` is a phone or browser, `404` for an unknown definition or a device that is not an active member, `409` when this worker is not in a network.

## Recovery key

A recovery key lets you restore the network onto a fresh worker.

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `GET` | `/api/v1/network/recovery` | none | Owner only. `{"configured": true, "stale": false, "created_at": "...", "key_version": 3}` |
| `POST` | `/api/v1/network/recovery` | none | Owner only. Create or replace the key. `201` with `recovery_key`, `created_at`, `key_version`; `409` when this worker is not in a network, or when this device does not hold the current network key (for example, before it has received the key after a rotation) |

The `recovery_key` in the `POST` response is shown once and cannot be read back. A key becomes `stale` after the network key rotates; create a new one.

```bash
curl -X POST http://localhost:8000/api/v1/network/recovery \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Inviting a worker

To invite another worker, create a one-time code with `POST /api/v1/pairing/grants` and `"purpose": "network:join"` (see [Pairing codes](#pairing-codes)). The response's `join.url` is the invite link. A valid code admits the joining worker as soon as it arrives, so no approval is needed; the endpoints below handle requests that are still waiting.

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `GET` | `/api/v1/network/joins` | none | `{"joins": [...]}` with `id`, `status`, `device_id`, `name`, `platform`, `fingerprint`, `created_at`, `expires_at` |
| `POST` | `/api/v1/network/joins/{join_id}/approve` | `{}` | Admit a waiting worker. `{"join_id", "status", "device_id"}`. `400` if the request can no longer be approved |
| `POST` | `/api/v1/network/joins/{join_id}/reject` | none | `{"join_id": "...", "status": "rejected"}`. `404` for an unknown request |

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/network/joins/JOIN_ID/approve \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

## Joining a network

These run on the worker that is joining.

### POST /api/v1/network/enroll

Join another worker's network with its invite. Send either the invite `link`, or `endpoint_url` plus `code`.

**Auth:** Owner only

JSON body:

- `link` (max 4096): the invite link. Fills in the address, code and fingerprint.
- `endpoint_url` (max 2048) and `code` (12 characters): use instead of `link`. Both are required when `link` is absent.
- `inviter_fingerprint` (optional, max 64): the inviting worker's fingerprint. When given, the inviter must match it.
- `device_name` (optional, max 100): the name this worker takes in the network.
- `replace` (default `false`): set `true` to leave this worker's current network and join the invited one. The joined network's records replace local copies.

```bash
curl -X POST http://localhost:8000/api/v1/network/enroll \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"link": "INVITE_LINK", "device_name": "Laptop"}'
```

**Response (`202`):** `{"join_id": "...", "status": "pending", "inviter": {"device_id", "fingerprint", "name", "network_name"}, "your_fingerprint": "..."}`. The join finishes in the background; poll `GET /api/v1/network/enroll`. `400` for a bad link or code, an unreachable inviter or a fingerprint mismatch. `409` when this worker is already in a network and `replace` is not `true`.

### GET /api/v1/network/enroll

Progress of the current join.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/network/enroll \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"enrollment": {"id", "status", "endpoint_url", "inviter_fingerprint", "fingerprint", "error", ...} | null, "enrolled": false}`. `status` is `pending`, `complete`, `failed`, `rejected` or `expired`.

### DELETE /api/v1/network/enroll

Cancel the current join. Returns `204`.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/network/enroll \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Account workers

Workers signed in to the same [Shuffle account](#shuffle-account) share one network. A worker that registers through the account is admitted automatically by any online member; these endpoints are the manual fallback.

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `GET` | `/api/v1/network/hub/devices` | none | Workers waiting to be admitted: `{"devices": [{"device_id", "name", "platform", "created_at"}]}`. `503` when the account service cannot be reached |
| `POST` | `/api/v1/network/hub/devices/{device_id}/approve` | `{}` | `{"device_id": "...", "status": "approved"}`. `400` if it cannot be admitted, `409` if this worker is not in a network |
| `POST` | `/api/v1/network/hub/devices/{device_id}/reject` | none | `{"device_id": "...", "status": "rejected"}` |
| `POST` | `/api/v1/network/hub/adopt` | `device_name` (optional, max 100); send at least `{}` | Leave this worker's own network and join the account's; the account's records win. `{"adopted": true, "status": "..."}`. `409` on failure |

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/network/hub/adopt \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"device_name": "Laptop"}'
```

## Installs

The install list feeds the runner picker and the project picker for remote sessions. When this worker is in a network, `GET /api/v1/cluster/installs` returns the same workers as `GET /api/v1/network/runners`, and workers in the network are managed with the [member endpoints](#members) instead (the rename, approval and delete endpoints below answer `409` for them).

### GET /api/v1/cluster/installs

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/cluster/installs \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"installs": [...], "self_instance_id": "..."}`. Each install has `id`, `device_name`, `platform`, `version`, `last_seen`, `online`, `approved`, `is_self`.

### PATCH /api/v1/cluster/installs/{install_id}

Rename an install. JSON body: `device_name` (required, not blank).

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/cluster/installs/INSTALL_ID \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"device_name": "Build box"}'
```

Returns the updated install. `400` for a blank name, `404` for an unknown install.

### PUT /api/v1/cluster/installs/{install_id}/approval

Allow a new install to run tasks, or revoke one. JSON body: `approved` (required boolean).

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/cluster/installs/INSTALL_ID/approval \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'
```

Returns the updated install plus `restart_required` (`true` when an online install was just approved; it starts taking work after a restart). `400` when revoking the install that serves the request, `404` for an unknown install.

### DELETE /api/v1/cluster/installs/{install_id}

Remove an install that is gone for good. An install that is still running registers again on its next check-in.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/cluster/installs/INSTALL_ID \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"ok": true, "deleted": "INSTALL_ID"}`. `404` for an unknown install.

### GET /api/v1/cluster/projects

Projects available on at least one approved, online install, including projects this worker has no local copy of. API users see only their assigned project.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/cluster/projects \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "projects": [
    {
      "project_v2_id": "...",
      "origin": "github.com/acme/web",
      "name": "web",
      "local_project_id": null,
      "remote_only": true,
      "eligible": [{ "install_id": "...", "device_name": "Build box", "online": true, "is_self": false }]
    }
  ]
}
```

### GET /api/v1/cluster/git-state

For one project, which installs can run a worktree session (`eligible`) and what each has checked out.

**Auth:** Required. API users can query only their assigned project.

Query: exactly one of `project_id` or `project_v2_id` (`400` for both or neither).

```bash
curl "http://localhost:8000/api/v1/cluster/git-state?project_id=my-web-app" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** `{"project_v2_id": "..." | null, "installs": [...], "recent_ops": [...]}`. Each install row adds `eligible`, `checkouts` (`branch`, `path`, `head_sha`, `dirty`, `status`, `updated_at`) and, when its state could not be read, `availability_error`. A project that is not a Git repository returns `project_v2_id: null` with every install ineligible.

## Pairing codes

One-time codes that pair a phone or browser with this worker, or invite another worker into its network. A code is valid for 5 minutes and allows 10 attempts.

**Auth:** Owner only for every endpoint in this section.

### GET /api/v1/pairing/endpoints

Addresses this worker can be reached at (the address of the current request and the advertised address), best first.

```bash
curl http://localhost:8000/api/v1/pairing/endpoints \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"endpoints": [{"url": "http://192.168.1.20:8000", "kind": "lan", "label": "Local network", "secure": false, "loopback": false, "source": "current"}]}`. `kind` is `loopback`, `lan`, `tailscale`, `https` or `custom`.

### POST /api/v1/pairing/grants

Create a code.

JSON body:

- `purpose`: `controller` (default; a phone or browser), `network:join` (another worker) or `runner`.
- `endpoint_url` (optional, max 2048): the address the code points to, usually one from `GET /api/v1/pairing/endpoints`. Must be `http://` or `https://` with no credentials, query or fragment.
- `device_label` (optional, max 100).

```bash
curl -X POST http://localhost:8000/api/v1/pairing/grants \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"purpose": "controller", "endpoint_url": "http://192.168.1.20:8000"}'
```

**Response (`201`):** `grant` (`id`, `purpose`, `expires_at`, `max_attempts`, ...), `code`, `display_code`, `pairing_url`, `qr_svg` (an SVG QR code of the link, or `null`), and for `network:join` a `join` object (`url`, `endpoint`, `code`, `fingerprint`, `device_name`) whose `url` is the invite link. `400` for an invalid `endpoint_url`.

### GET /api/v1/pairing/grants

List codes that are still active: `{"grants": [...]}`. The code itself is never returned again.

```bash
curl http://localhost:8000/api/v1/pairing/grants \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### DELETE /api/v1/pairing/grants/{grant_id}

Revoke an unused code. Returns `204`, or `404` if no active code has that id.

```bash
curl -X DELETE http://localhost:8000/api/v1/pairing/grants/GRANT_ID \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Paired phones and browsers

**Auth:** Owner only

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `GET` | `/api/v1/pairing/clients` | none | Active paired devices: `{"clients": [{"id", "device_id", "device_name", "platform", "client_type", "scopes", "paired_at", "last_seen", "session_expires_at"}]}` |
| `PATCH` | `/api/v1/pairing/clients/{client_id}` | `device_name` (required, 1-100) | Rename. Returns the updated device. `404` if unknown |
| `DELETE` | `/api/v1/pairing/clients/{client_id}` | none | Revoke the device's access. `204`, or `404` if no active device has that id |
| `DELETE` | `/api/v1/mobile/devices/{device_id}` | none | Remove a registered mobile device and revoke its pairing, with its pending and trusted actions. `204`, or `404` if unknown |

Both `DELETE` routes close every socket the device holds (chat, session stream, browser panel, voice, streaming transcription) with `4001` at once, and so does the device logging out (`POST /api/v1/auth/logout`). A device revoked another way or one whose session token is replaced loses its sockets within about 30 seconds. See [WebSocket authentication](/docs/api-reference/websocket#connection-parameters).

```bash
curl -X PATCH http://localhost:8000/api/v1/pairing/clients/CLIENT_ID \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"device_name": "My phone"}'
```

## Shuffle account

Signing a worker in to a Shuffle account turns on remote access, lets the Shuffle app reach it, and puts it in the account's network. Local and LAN use never need an account.

Errors from the endpoints in this section and the next come back as `{"error": "..."}` rather than `{"detail": "..."}`, and a malformed body gets `422` with `{"error": "Invalid request"}`.

### GET /api/v1/mobile/hub/connection

The account link state of this worker.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/mobile/hub/connection \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "signed_in": false,
  "profile_ready": false,
  "server_id": null,
  "status": "waiting",
  "url": null,
  "code": "ABCDEFGH2345",
  "expires_at": 1790000000.0,
  "authorization_url": "https://...",
  "signup_url": "https://...",
  "qr_svg": "<svg ...>",
  "error": null,
  "refusal": null
}
```

`status` is one of `disconnected`, `waiting` (a sign-in code is open), `connecting`, `connected`, `limited` (the account already uses all the remote connections it may; `refusal` explains), `locked` (stored credentials cannot be decrypted) or `unavailable` (the remote access connector is not installed). `url` is this worker's public address once remote access is up.

### POST /api/v1/mobile/hub/connection

Start signing this worker in, or reopen the open offer. The owner finishes by opening `authorization_url` (or `signup_url` to create an account), or by entering `code` or scanning `qr_svg` with **Add worker** in the Shuffle app. The code expires after ten minutes and links one worker.

**Auth:** Owner only

JSON body: `name` (required, 1-80 chars): the worker name shown on the account.

```bash
curl -X POST http://localhost:8000/api/v1/mobile/hub/connection \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Office desktop"}'
```

Returns the same shape as `GET`. `400` for an invalid name, `409` when the worker is signed in to an account linked by someone else, `503` when credential storage is locked or the remote access connector is missing.

### DELETE /api/v1/mobile/hub/connection

Sign this worker out of its account. Other workers and phones stay signed in. Returns the new connection state.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/mobile/hub/connection \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### GET /api/v1/mobile/hub/account

The signed-in account: its username, workers, and phones and browsers. Only names and platforms are returned, never credentials.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/mobile/hub/account \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "account": { "id": "...", "username": "sam", "profile_complete": true },
  "servers": [{ "id": "...", "name": "Office desktop", "url": "https://...", "current": true }],
  "devices": [{ "id": "...", "name": "Pixel 9", "platform": "android", "local_device_id": "..." }]
}
```

`devices` is absent when the account service is too old to report it. `409` when this worker is not signed in, `503` while the link is still being activated.

### PATCH /api/v1/mobile/hub/account

Set the account username. JSON body: `username` (required). It is lowercased and must be 3-64 letters, numbers, underscores, periods or hyphens, starting with a letter or number (`400` otherwise).

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/mobile/hub/account \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username": "sam"}'
```

Returns the account in the same shape as `GET`.

### DELETE /api/v1/mobile/hub/account/{kind}/{registration_id}

Remove a worker (`kind` = `servers`) or a phone or browser (`kind` = `devices`) from the account. This does not touch local pairings or network membership.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/mobile/hub/account/devices/REGISTRATION_ID \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"ok": true, "cleanup_pending": false}`. `404` for another `kind`, `400` for a malformed id, `409` when removing this worker itself (sign out with `DELETE /api/v1/mobile/hub/connection` instead), `503` when the account service does not confirm the removal.

## Account device approval

Signing in to the Shuffle account on a phone or browser does not by itself give it access to this worker: the owner approves each device here.

**Auth:** An owner session on this worker (owner token, the owner's signed-in browser, or local access from the worker's own machine), or an account device the owner already approved here. API-user keys and other paired devices get `403`.

### GET /api/v1/mobile/hub/devices/confirmations

Devices from the account that have asked for access to this worker. Empty when the worker is not signed in.

```bash
curl http://localhost:8000/api/v1/mobile/hub/devices/confirmations \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** an array of `{"device_id", "hub_device_id", "paired_client_id", "mobile_record_id", "device_name", "platform", "confirmation", "revoked_at", "api_key_expires_at"}`. `confirmation` is `pending`, `approved` or `denied`.

### POST /api/v1/mobile/hub/devices/{device_id}/confirmation

Approve or deny a device. Copy `paired_client_id` and `mobile_record_id` from the list entry. Denial is permanent: a denied device must pair again with a new identity.

JSON body: `paired_client_id` (required, max 128), `mobile_record_id` (required, max 128), `approved` (required boolean).

```bash
curl -X POST http://localhost:8000/api/v1/mobile/hub/devices/DEVICE_ID/confirmation \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paired_client_id": "...", "mobile_record_id": "...", "approved": true}'
```

**Response:** `{"confirmation": "approved"}`. `409` when the device's authorization changed since it was listed, it was already denied, or its credential expired.
