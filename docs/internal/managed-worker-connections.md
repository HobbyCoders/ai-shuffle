# Managed worker connection credentials

Each installation has its own account binding and managed tunnel, keyed by
`effective_instance_id`. Sharing a database or approving a device on the LAN
does not sign that worker in to an account. Sign in on every worker you want
listed on mobile, then use the same Shuffle account on the phone.

**Sign out of this worker** stops its connector, clears its local account
credentials and browser sign-in requests, and revokes only its registration at
the hub. Existing account sessions lose access because their server binding is
no longer active. Direct LAN pairings and other workers keep their authorization.
When the hub cannot be reached, local sign-out still completes; the UI explains
that its remote listing must be removed in the account. A new sign-in creates a
fresh registration, so credentials from a previous account cannot regain access.

The new binding and phone-authorization tables are additive. An existing managed
binding is read only by its owning installation; another worker's older build
and credentials remain intact. Sign-out leaves a local tombstone to prevent a
legacy binding from being adopted again. Phone credentials use a separate local
device identity for each worker while preserving the account's phone identity
on the Android API. This permits one phone to enroll with workers that share a
database without replacing another worker's API key.

Automatic remote connections can restart before an administrator signs in.
When an owned managed binding first contacts the account hub, Shuffle creates
an installation credential encrypted with a random Fernet key. The key lives
under `.managed-connection` in the persistent data directory, separately from
the encrypted credential in the database. Linux uses a private directory and
an owner-only key file. Windows additionally protects the key with the current
OS user's DPAPI identity.

Key creation flushes a private temporary file before publishing it atomically
without replacing any existing key. The database copy is saved only after the
key's storage has been flushed. Retries reuse a complete previously published
key; interrupted writes cannot leave a partial final key.

The encrypted credential is bound to the exact hub origin, server ID and
installation owner. Another installation sharing the database cannot use it.
The original encrypted binding secret remains unchanged and participates in
credential-key adoption. Application credentials use the persistent data key;
the independent installation credential does not unlock provider keys or other
application settings. Local owner-password login is separate from these storage
keys; changing a login password does not adopt or replace an encryption key.

Existing managed bindings populate their installation credential during the
next hub request once application credential storage is ready. Before upgrading
a legacy installation, stop all writers and retain a matching database/data
backup. Adopt its original data key through the existing `ADMIN_PASSWORD` startup
setting or the private `python -m scripts.credential_keys adopt` prompt. Verify
`python -m scripts.credential_keys status` reports ready. No browser password
login unlocks that migration; use the storage-key adoption procedure instead. New bindings populate
their installation credential before the first connection. After successful
adoption and restart verification, remove `ADMIN_PASSWORD` from configuration.

Keep the persistent data directory when updating or recreating the container,
and include it with the database in protected backups. A missing, corrupt,
wrong-owner or unsafe-permission key fails closed; Shuffle does not silently
replace an existing installation credential. Windows keys are tied to the
original OS user. Back up both `.managed-connection` and `.credential-keys`,
including a portable credential-key export where needed. Missing or damaged
keys require explicit operator recovery; do not delete key metadata or depend
on account sign-in to recreate it. See [credential key recovery](../credential-key-recovery.md).

Local credential availability does not grant access by itself. Every connection
and account-device request still checks current authorization at the hub.
Revoking the worker stops the connector on its next authorization check.

## Local and remote owner access

Local setup and trusted local-network access require no account. To enable
remote access, open **Devices → Remote access** and approve the worker in the
browser or Android app. The managed tunnel uses the protected loopback listener
on port 8001. The local owner listener must not be exposed by a public proxy or
tunnel. During an existing deployment upgrade, keep `SHUFFLE_LOCAL_ACCESS=false`
until the protected listener and public route are verified; the hub updates the
same tunnel, hostname and DNS record in place. See [local access rollout](../LOCAL_ACCESS.md).

Each managed worker belongs to one hub account. Signing the worker and phone
into that account authorizes access without a separate worker approval. On the
first authenticated request, the worker verifies the active account/device
binding and records approval for that enrolled credential. This applies only to
`managed:<effective_instance_id>` bindings; controller-linked hubs still require
owner confirmation. Display names and extra hub fields claiming approval do not
grant authority.

The account service is an admin trust root for managed workers. Compromise of
the account or service can grant command execution on linked workers. This is
the accepted tradeoff for account sign-in alone; local denials, revocations,
credential expiry and worker/account isolation remain enforced.

Confirmation requires a signed-in owner on the local listener, the protected
local loopback/desktop path, or an already worker-confirmed owner device using
its live `shc_` credential. The optional trusted-LAN switch, preview cookies,
mobile enrollment keys and ordinary directly paired controllers cannot confirm
an account device. Check the device identity through a trusted channel, not just
its hub-supplied display name, before approving it.

The worker-local API is:

- `GET /api/v1/mobile/hub/devices/confirmations`: current worker's device IDs,
  paired-client IDs, mobile-record IDs, display metadata, expiry and decision.
- `POST /api/v1/mobile/hub/devices/{device_id}/confirmation`: send the exact
  `paired_client_id`, `mobile_record_id`, and `approved: true` or `false`.
  A stale identity, expired credential or revoked device cannot be approved.
  Denial is durable: neither a hub ticket nor an old approval retry reverses it.

The device keeps its device-bound `shc_` session and account-scoped chat/tool
identity. Every request checks the recorded decision, immutable ownership links,
credential expiry/revocation and the hub verdict. Bounded outage grace never
bypasses a local denial, revocation or expiry. Admin authority additionally
requires a managed binding for this exact installation.

Existing pending account devices can retry with their saved credentials after
upgrade. Enrollment now records the mobile-record ID alongside its pending
decision. For older rows without a decision, automatic authorization requires
the mobile and paired-client creation times to match the original enrollment;
key deadlines are unchanged. Repairing a credential creates a new mobile record
and still requires owner confirmation. An old decision cannot approve the
replacement, even if a LAN-trusted client obtained a pairing code. Denied
identities require enrollment under a new device identity.

### Device key lifetime and renewal

New direct-paired and hub-enrolled `aim_` keys last **90 days**. Their derived
web/native sessions cannot outlive the mobile credential. Existing explicit
expiry dates are preserved; legacy rows without an expiry retain exactly
`created_at + 365 days`, not 365 days from upgrade or first use. This avoids
silently expiring legitimate saved keys during rollout. A successful renewal
moves either legacy format onto the 90-day policy.

`POST /api/v1/mobile/key/renew`, authenticated by the device's existing `aim_`
Bearer, returns `api_key`, `device_id`, `expires_at` and `rotated`. Clients should
call before session bootstrap and periodically while active, persist the result
atomically before using it, and never log the response. Outside the final 30 days
it returns the current key without extending its deadline. Within that window it
rotates to a new 90-day key. Pending, denied, revoked and expired credentials
cannot renew; hub-only proof cannot renew any device key.

The previous key immediately stops authenticating ordinary API calls. A local
encrypted receipt lets a lost-response retry recover the *same* replacement,
only through `/key/renew` and only until the previous key's original expiry.
Retries never extend that deadline. Receipts are bound to the mobile record and
current key: deletion, repair, denial and revocation cannot be undone by retrying.
If renewal fails before commit, the old key/expiry remain unchanged. Once expired,
use explicit local pairing/repair and (for hub devices) confirmation again.

Android now checks renewal before web/native session bootstrap, device-tool
requests, push registration and explicit linking. Successful checks are cached
for at most six hours during active use and reset on process restart. Each
worker has one in-flight renewal; transient failures get exactly one retry.
Denied, pending and expired credentials are never retried as if approved.
Replacement keys are committed to encrypted preferences before any consumer
uses them. A failed save or interrupted response preserves the bounded retry
path; stale account-registry snapshots cannot overwrite a rotated key at the
same endpoint. Removal, account changes and endpoint changes fence in-flight
updates. Pending and denied API responses have distinct, fixed client messages.

A hub registry refresh proves membership, **not permission to move a credential**.
Once a phone has saved a worker key, the saved HTTPS origin is pinned: registry
hostname/port changes retain the known endpoint, key, authorization epoch and
origin-bound Access credentials. Equivalent HTTPS spelling (case, default 443,
trailing slash) is normalized, not treated as a new trust grant. Invalid schemes
are rejected. At commit time the list is reconciled against current storage so
an in-flight refresh cannot undo renewal or explicit repair, restore a removed
key, or roll an independently saved connection back to an older endpoint.

A legitimate endpoint move may therefore leave the pinned connection offline.
Obtain the new address and a fresh pairing grant independently from the worker's
owner and establish a fresh connection; never transfer the saved account key or
Access credentials based on the hub URL. The existing account-device repair
flow also remains pinned to its saved worker endpoint. This change cannot undo
credentials that were exposed before the upgrade; review already-saved worker
addresses if the hub may previously have been compromised.

Native HTTP session clients do not follow redirects. Asset/download requests
may follow a bounded redirect chain only within the exact pinned HTTPS origin;
origin-changing redirects never receive the worker Bearer, cookie or Access
headers. The shared browser frontend's redirect policy is a separate verification
surface and is not proved by the native transport tests.

The browser/desktop frontend and bundled mobile UI do not store `aim_` keys.
Android's native host owns them and supplies independently minted `shc_` sessions
to its WebView/native voice consumers. Local browser owner login remains the
worker's own authentication path, not a hub assertion.

**Rollout gate:** owner-facing confirmation controls are not yet integrated.
The native networking core has focused JVM coverage, but the complete Android
build, encrypted-store integration and real pending/confirm/renew journey still
need Android SDK/device and isolated preview verification. Do not ship the
shorter policy as a complete client experience until those gates are satisfied.

Direct pairing and the older controller-initiated `/hub/link` flow do not prove
that delegation and retain their existing restrictions. API users remain
scoped integration credentials; they do not become administrator accounts.
Sensitive settings still require owner authorization and available persistent
credential storage. Local owner-password sessions remain a recovery and
confirmation path; a hub account cannot replace them.
