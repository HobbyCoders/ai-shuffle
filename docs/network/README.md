# Worker network

Status: implemented 2026-09-08. Supersedes the shared-database cluster. This
file describes what was built, where it lives, and where it deviates from the
original plan.

## What a worker is now

- A worker runs by itself on any device with its **own PostgreSQL**. The desktop
  app bundles and supervises PostgreSQL (`desktop/src/main/postgres.ts`); the
  Docker image runs its own private PostgreSQL 16 inside the container
  (`docker/container_runtime.py`). External or shared databases are not
  supported.
- Every worker has a **device identity**: an Ed25519 signing key and an X25519
  encryption key under `<data_dir>/.credential-keys/device-identity.key`
  (`app/network/identity.py`, same custody as the credential key: owner-only
  file, DPAPI on Windows). The device id is `dev_` + 24 hex of the signing key's
  digest, so it is self-certifying. Its fingerprint (8 groups of 4 hex) travels
  in invite links so the joiner can check it is talking to the intended inviter.
- Workers connected into one **network** converge on the same settings, keys,
  profiles, preferences, UI state, notes (each keeping the API key that owns
  it, `notes.api_user_id`), plugins, memory facts, skills and session catalog. Each session executes on exactly one **owner device**;
  every worker can see it and drive it.

## Code map

| Module | Role |
|---|---|
| `app/network/crypto.py` | Ed25519, X25519+HKDF+AES-GCM key wrap, AES-256-GCM payloads, recovery key format. WebCrypto-compatible; vectors in `tests/core/network/vectors.json` are checked by `tools/scripts/verify-network-vectors.mjs`. |
| `app/network/envelope.py` | The signed, encrypted operation envelope. |
| `app/network/membership.py` | Certificates, revocations, key rotations, recovery certificates. |
| `app/network/policy.py` | The sharing policy: every synced collection, its columns, secrets, mode; `is_shared_setting_key()` for `system_settings`. |
| `app/db/network.py` | Local tables: `network_state`, `network_members`, `sync_ops`, `sync_records`, `sync_watermarks`, `sync_pending`, `sync_conflicts`, `sync_peers`, `network_joins`, `network_presence`, `network_catalog_cursors`, `sync_blobs`, plus the record tables for membership, plugins and memory. |
| `app/network/engine.py` | Outbox (`record`, `scan_collection`), inbox (`apply_envelope`, `apply_batch`), snapshots, membership actions (`admit_device`, `revoke_device`), presence. |
| `app/network/transport.py` | Peer delivery over the worker API with device tokens; sync, scan and presence loops; restore-from-backup detection. |
| `app/network/tokens.py` | Challenge-signed, short-lived device tokens; `require_device` dependency. |
| `app/network/enrollment.py` | The `network:join` pairing purpose, both sides. |
| `app/network/hub.py` | Account hub client: registration, admission and approvals, revocation notices, recovery-wrap upload and bootstrap. Worker data never passes through the hub. Contract in `hub-api.md`. |
| `app/network/recovery.py` | Recovery key creation and unlock. |
| `app/network/runners.py` | The runner directory that replaces `installs`. |
| `app/network/catalog.py` | Owner-ordered session catalog, tags, prompts and transcript mirrors. |
| `app/network/control.py` | Driving a session on another device: turns, session creation, interrupt, prompt answers, hydrate; fork on offline. |
| `app/network/ownership.py` | Which device fires an automation or holds a connector. |
| `app/network/usage_budget.py` | Shared provider usage request budgets coordinated by the founder; see [usage-budgets.md](usage-budgets.md) for outage and upgrade behavior. |
| `app/network/memory_records.py` | Facts and skills as records. |
| `app/network/prose_skills.py` | Prose skill folders (`~/.fluid/skills/<name>/SKILL.md` plus small text files) as `prose_skill` records in the memory collection. |
| `app/network/plugins_sync.py` | Desired plugin state. |
| `app/network/blobs.py` | Content-addressed bytes for artifacts and note attachments. |
| `app/network/peer_files.py` | Signed, encrypted 2 MiB file parts at `/peer/blob/{hash}/parts/{index}`. |
| `app/network/peer_snapshot.py` | Signed, paged state transfer for a peer the operation log cannot serve (see [peer-first.md](peer-first.md)). |
| `app/network/startup_sync.py` | Bounded desktop startup exchange (60-second budget). |
| `app/network/handoff.py` | Leased checkout and transcript handoff between workers. |
| `app/network/session_runtime.py`, `session_active.py` | Owner-published session display state and Active/Settled changes. |
| `app/network/keyring.py` | Retained network keys across concurrent rotations. |
| `app/network/endpoints.py` | Validation of the addresses members advertise to each other. |
| `app/network/startup.py` | Boot wiring, single-install migration, resource hooks. |
| `app/api/network.py` | `/api/v1/network/*`: status, members, joins, enrollment, recovery, conflicts, runners, owners, peer plane, control plane, blobs. |
| `app/api/network_usage.py` | `/peer/usage-budget`: peer calls to the usage budget authority. |
| `scripts/network_migrate.py` | Moves an install's session state out of a shared database. See `migration.md`. |

Frontend: `frontend/src/lib/api/network.ts` and `account.ts`; under Settings >
Providers > Devices `DevicesSection.svelte` composes `AccountPanel` (sign in, create
account, username, the account's workers, sign out), `AddDevicePanel` (worker
invite or phone/browser code), `WorkerDeviceConfirmations` and
`JoinNetworkPanel`; the first-run choices in `routes/setup` (clean install,
sign in, join with an invite); and the runner picker, which lists network
runners (`GET /api/v1/cluster/installs` returns them when the worker is
enrolled). Recovery keys, conflicts and advertised addresses have API routes
(`/api/v1/network/recovery`, `/api/v1/network/conflicts` (owner only),
`/api/v1/cluster/advertise`) but no Settings UI.

## How sync works

1. **Writing.** Domain code writes rows as before. `engine.scan_collection`
   compares each shared row (its content hash over the policy's columns) with
   `sync_records` and appends an envelope for every change; hot paths can call
   `engine.record()` in their own transaction. The scanner runs at most every
   two seconds. Quiet passes back off to 60 seconds, a registered resource
   mutation wakes it at once, and a full reconciliation runs at least every 60
   seconds.
2. **Envelope.** Header in the clear (network id, collection, record id, op,
   Lamport clock, origin device, incarnation, origin sequence, order sequence,
   schema version, key version, content hash, display timestamp), payload
   AES-256-GCM under the network key with the header as associated data,
   Ed25519 signature over the canonical header.
3. **Delivery.** Each worker keeps `sync_ops` (its own and everything it
   received, so any member can forward any other's operations unchanged) and a
   watermark per (origin, incarnation). A pass with a peer is one `exchange`
   (send my watermarks, get what I lack, learn theirs) followed by pushes of
   what the peer lacks. The same operation arriving over two peer routes (for
   example LAN and tunnel, or forwarded by a third member) applies once. Idle
   cadence is 10 s; after a local write the pass runs at once, so two workers on
   a LAN converge within about two seconds.
4. **Applying.** Verify signature against the members table, reject a revoked
   origin past its `last_seq_accepted`, order per stream (holes wait in
   `sync_pending`), decrypt, last-writer-wins on (clock, device id), materialize
   into the domain table, keep the loser in `sync_conflicts` for 30 days
   (columns encrypted at rest are stored as `<secret>`, never in plaintext),
   advance the watermark. All in one transaction per envelope.
5. **Bootstrap.** A joiner installs the join response (keys wrapped to it, the
   membership chain), copies signed snapshot chunks from the inviter, adopts the
   inviter's watermarks, then flips from `syncing` to `ready`. Its outbox is
   disabled until then, so an empty worker never publishes its absence of
   records as deletions.
6. **Owner-ordered collections.** Sessions, tags, prompts and events are
   published by the owner device only, events with their dense `seq` as
   `order_seq`. Mirrors are read-only; the UI drives the owner through
   `control`. Transcripts older than 30 days are published on demand
   (`POST /api/v1/network/runners/{device}/sessions/{id}/hydrate`). A session
   an API key places on another worker keeps that key's `api_user_id` on the
   owner (API users replicate; a key the owner does not know is dropped), and
   a mirrored copy that names no key never clears the one a worker holds, so
   the key's scope applies on every device.
7. **Blobs.** Artifacts and note attachments travel as records naming a hash;
   bytes are fetched in encrypted 2 MiB parts from any online holder at
   `/peer/blob/{hash}/parts/{index}` and stored locally after a content-hash
   check. An artifact download on a mirror fetches on demand.
8. **Plugins.** `network_plugins_desired` carries each plugin's id, version,
   enabled flag and config; binaries never travel. A worker's scan publishes a
   row only for a plugin the table does not have yet or when its enabled flag
   differs. Versions are not enforced across workers: `apply_desired` never
   reinstalls to match one, so publishing on a version difference made two
   workers on different versions overwrite the row on every scan and send
   each other a new operation forever.
9. **Assistant.** The owner has one assistant across workers. Its
   configuration and the id of its one home conversation travel as the
   `assistant` collection (`assistant_account`, a single row). The worker that
   owns the home session runs it and is the only one that runs background
   research; the others open the home as a remote session and turn research off
   in their own runner row. When two workers each hold a home, the older
   session wins everywhere (ties go to the lower session id) and the other is
   kept as an ordinary conversation. Research runs, events and source
   watermarks stay on the worker that runs research. See
   `app/core/assistant_service.py`.

## Trust

- **Pairing.** A member creates a `network:join` grant; the QR/link carries the
  endpoint, the code and the inviter's fingerprint. The joiner posts its public
  keys with the code and (when it came from a link) the inviter's fingerprint;
  the inviter refuses a wrong fingerprint before consuming the code. The
  consumed one-time code is the authorization: the inviter issues the
  certificate and wraps the keys at once, and the joiner picks the response up
  once through its poll secret. Nobody compares fingerprints or approves on a
  second screen (`approve_join` remains only for a join that could not be
  admitted on contact). Pairing codes are hashed under a local pairing key
  (`network_pairing_key`), no longer the cluster secret.
- **Account.** A signed-in worker registers its device with the hub. The first
  worker on an account founds the network (`hub.found_account_network`);
  every later one is admitted by whichever member is online
  (`hub.admit_pending_devices` runs in the sync loop; the hub only lists
  devices whose server secret is bound to the same account) or unlocks with
  the recovery key when every worker is off. A worker that already has its
  own network is answered `conflict`; the account always wins, so the loop
  adopts the account's network at once (`hub.adopt_account_network`:
  `engine.leave_network` then a fresh registration; the account's records
  win, local-only rows are merged in). A signed-in worker is always in its
  account's network.
- **Proof of key possession.** A device description (the join request, the
  hub registration) carries a `proof`: an Ed25519 signature by the device's own
  signing key over its id, both public keys and kind. Join-code admission and
  all account-hub approvals require it, including manual approvals, so a relay
  cannot substitute its encryption key and receive network keys in the device's
  name. Older workers must update and re-register: the displayed signing-key
  fingerprint does not bind their encryption key. An active member's keys are never
  rebound by a later description.
- **Device tokens.** Peers prove key possession with a signed challenge
  (issued to one named active member, spendable only by that member's key,
  bounded per member and per client address) and get a five-minute HMAC
  token bound to the network and its membership generation; every
  authenticated request re-checks membership, so revocation takes effect
  within one token lifetime, and any membership change invalidates every
  outstanding token at once. Any active member can hold a token, consumer
  devices included. The sync plane (`/peer/exchange`, `/ops`, `/snapshot`,
  `/state`, `/blob`, `/ping`) admits any active member (`require_device`).
  Routes that act with the owner's authority on this worker admit only
  another active worker of the account (`require_worker_device`):
  `/peer/control/*`, `/peer/browser/*`, `/peer/workspace/*`,
  `/api/files/peer/*` and the generated-media `/peer/*` routes. The
  usage-budget routes check the worker kind themselves.
- **Revocation.** Publishes a signed revocation carrying the last accepted
  incarnation and sequence of the removed device and rotates the network key
  for the rest (wrapped to each remaining member, sealed under the previous
  key so members can read it). A revoked device cannot open a newer stream
  (its later incarnations are refused outright), a revoked founder cannot
  re-establish itself with another self-signed certificate, certificates it
  issued past the honoured point are withdrawn when the revocation applies,
  and the account hub is told to retire its registration. Sessions the
  removed device owned stay read-only mirrors on the other members: no
  control request (turn, stop, answer) is sent to it any more, and the UI
  offers to fork the transcript instead. The UI recommends rotating provider
  keys.
- **Recovery.** A 128-bit recovery key (`A1-XXXXX-...`) wraps the network key
  through HKDF-SHA256 + AES-GCM; the wrap is public data kept locally and at the
  hub. Unlocking self-issues a certificate whose proof is an AES-GCM tag under
  the network key, which only a key holder can make; members accept it from the
  device's first operation.
- **Restore from backup.** An envelope this device signed that it does not hold
  proves its sequence went backwards: a peer that claims to hold more of our
  stream is asked for the first op past our outbox and only a verified one
  counts. The device then bumps its incarnation and starts a fresh stream, and
  re-imports its earlier incarnation's ops from peers. Until the first
  comparison with a peer after start, the outbox is held so a restored worker
  never reuses sequence numbers peers already hold.
- **Apply semantics.** An op that cannot be applied *yet* (a key or
  certificate from another stream still on its way, credential storage
  locked) stays in place for a retry; one that can never apply (a record the
  database refuses, a membership record that does not verify) moves the
  stream past it and keeps the record in the conflicts list. Buffered
  out-of-order ops are bounded per stream and dropped for revoked origins;
  the op log is pruned after 30 days (peers behind the pruned prefix take a
  state copy instead).

## Deviations from the plan

- **Change capture is a scanner, not per-function hooks.** The plan wanted
  every domain write to call `sync.record` in its own transaction. With 1240 SQL
  call sites the hook approach was the riskiest change in the tree; the scanner
  reaches the same outcome (crash-safe, no SQL on the wire, sub-second latency
  after a write thanks to the resource-invalidation kick) with zero edits to
  domain code. `engine.record()` exists for hot paths.
- **Existing preference keys keep their names.** UI state already flowed through
  `user_preferences` before this change; renaming 29 keys to a `ui:` prefix
  would have forced a data migration for nothing. New UI state goes through the
  `persisted()` helper (`frontend/src/lib/stores/persisted.ts`), which prefixes
  `ui:` when asked.
- **The journal stays local.** Facts (`memories.json`) and skills sync; the
  episodic journal is each worker's synthesizer input, and syncing it would make
  every worker consolidate the same episodes twice.
- **Prose skills sync from `~/.fluid/skills`, not the data dir.** That is the
  only global directory fluid's skill discovery and the delegated leg's
  "# More skills" list read, so it is the source of truth; a data-dir copy
  would need mirroring back to be seen. (Docker keeps it on the
  `fluid-settings` volume.) Each `<name>/` folder is one `prose-skill:<name>`
  record and the folder is the unit of last-writer-wins. A record carries
  SKILL.md and UTF-8 text files with safe names up to three levels deep, at
  most 64 KiB each, 32 files and 256 KiB per folder. Binary or oversized
  extras stay local and are never touched by a remote change. A folder whose
  SKILL.md or totals break a limit is held: neither published nor deleted.
  A local manifest (`<data_dir>/fluid/prose_skills_sync.json`) records each
  folder's state at its last publish or apply, so a peer's record that has
  reached the database but not the disk is never reverted or deleted, an
  unpublished local edit is never overwritten (it publishes next and wins),
  and a missing root is not a mass deletion. Older builds ignore the
  `prose_skill` kind.
- **Remote turns are forwarded, not streamed back.** The owner enqueues the turn
  and its events return through the transcript mirror rather than a proxied SSE
  stream; the effect for the user is the same and no proxying of long-lived
  streams was needed.
- **Mirrors keep no coordination rows.** Runs, turns and wakeups never leave the
  owner, so a mirror never claims a turn.
- **Attachments sent to a session owned elsewhere are not forwarded yet.** The
  upload lift used the shared `upload_blobs` table; a turn with attachments for
  a remote-owned session is refused with a clear message until uploads travel as
  blobs (next milestone of this work).
- **The mobile UI leaves the workers.** After this change the worker serves the
  desktop web UI only; the Android app bundles its own build of the mobile UI
  (`mobile/`) and talks to workers through their APIs, so worker version skew
  never changes what the phone shows. See `docs/mobile-desktop-ui-split.md`.

## Transition window

`app/core/cluster.py`, the `installs` table and the cluster secret remain for
installs that have not joined a network. When a worker is enrolled,
`get_installs()` returns the network runner directory and the cluster secret is
only used by the legacy artifact redirect. Remove them once every deployment
reports Ready in Settings > Providers > Devices.

## Test scenarios (as automated)

`tests/core/network/` runs several independent workers in one process, each with
its own database, data directory and identity:

| Scenario | Test |
|---|---|
| Fresh install, no network | `test_collections.py::test_fresh_install_waits_for_the_first_run_choice`, existing install migration next to it |
| A sets a value, B has it after one exchange | `test_engine.py::test_join_snapshot_and_tail_converge` |
| Partitioned edits, higher clock wins, loser listed on both | `test_engine.py::test_partitioned_edits_resolve_by_clock_with_conflicts_on_both` |
| Key added on A, A off, C joins from B | `test_engine.py::test_secret_added_on_a_is_usable_on_c_joined_from_b` |
| Same op over two routes applies once | `test_engine.py::test_same_operation_over_two_routes_applies_once` |
| Out-of-order delivery held | `test_engine.py::test_out_of_order_delivery_is_held_until_the_hole_fills` |
| Device removed while offline, its writes rejected, new key unreadable | `test_engine.py::test_revoked_device_is_rejected_and_cannot_read_new_records` |
| Recovery certificate with every worker off | `test_engine.py::test_recovery_certificate_admits_a_device_when_everyone_is_off` |
| Restore from backup bumps incarnation | `test_transport.py::test_peer_link_exchanges_both_ways_and_detects_rollback` |
| Join over HTTP with fingerprint check, then sync | `test_transport.py::test_enrollment_over_http_then_sync` |
| Hub registration, founding the account network, device approve/reject | `test_hub.py` |
| Session on A mirrored to B, driven from B, forked when offline | `test_control.py` |
| Two claims on one session get one owner; control reaches only the owner; revoked device's late, buffered and new-incarnation ops refused | `test_ownership_revocation.py` |
| Generation bumps invalidate tokens, stale snapshot pages, recovery wrap round trip, offline catch-up, leave/create/join transition lock | `test_membership_lifecycle.py` |
| Malformed, forged, oversized and replayed peer batches | `test_peer_plane_errors.py` |
| Certificate, revocation, key-wrap and device-token refusals (a challenge is spendable only by its device) | `test_verification_refusals.py` |
| A session an API key places on another worker keeps its owner | `test_remote_session_api_owner.py` |
| Facts, skills, plugins, blobs, owners | `test_collections.py` |
| Prose skill folders | `test_prose_skills_sync.py` |
| Shared-database cluster migration | `test_migrate.py` |
