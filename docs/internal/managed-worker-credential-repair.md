# Reconnecting one revoked account worker on Android

A worker can be Online while rejecting an old phone credential. Reachability checks use a public identity endpoint; opening chat requires the saved device key. A revoked key must stay denied.

The `scoped-v1` account repair flow replaces only one worker's key. It preserves the phone's Shuffle account identity, the worker's account ownership, and every other saved worker. It supports worker-scoped account enrollments; missing or legacy ownership fails closed and is never converted to a fresh direct registration.

1. From the affected worker's trusted local administration, unregister that phone's internal worker-scoped mobile device using `DELETE /api/v1/mobile/devices/{device_id}`. The retained account authorization and revoked paired client identify the permitted replacement. Merely holding an account session or revoked key cannot do this.
2. Create a new controller pairing code on that same worker. Its grant records the issuing installation. A code issued by another worker sharing the database cannot authorize the repair.
3. In Android, open the affected worker and choose **Reconnect this worker**. Enter that worker's one-time code. The form fixes the saved HTTPS origin and checks its worker identity and versioned repair capability.

Android submits `/api/v1/mobile/register` with `account_repair: "scoped-v1"`, the pairing code, and the internal UUIDv5 derived from the worker ID and public account phone ID. It sends neither the old device key nor account credentials. The server checks live account ownership, retained scoped identity, the revoked paired client, and the absence of the old mobile registration. It then locks and rechecks ownership, binding, and client while consuming the same-worker grant and replacing the mobile key in one transaction. Generic mobile or browser registration cannot reactivate a retained account identity.

The response contains a newly generated key and the internal device ID. Android validates the response and current account/worker snapshot before replacing only the saved `apiKey`; its public account phone ID stays unchanged. Old device keys and web sessions remain invalid. A failed or interrupted exchange keeps Android's previous saved entry; if the code was consumed before its response was lost, local administration must prepare another explicit repair rather than silently retrying or resetting other workers.

The public `/api/v1/mobile/hub/endpoint` advertises `account_repair: "scoped-v1"` without exposing an account or device identity. Older workers require an update before Android can offer this flow. Public controller grant exchanges also require the grant's issuing installation; runner enrollment keeps its separate target semantics.

Enrollment and direct pairing serialize on a PostgreSQL transaction advisory lock for the device identity before reading ownership, including when no row exists yet. Browser pairing cannot race account enrollment and inherit its authority. Session persistence locks the paired client and rechecks the exact current mobile credential, expiry and scope; an old-key request already in flight cannot mint either a web or native session after repair. Repair clears prior native tokens as well as the web token.

Validation: 118 focused SQLite/API cases, including old-key requests paused before both awaited mint operations for both session purposes, and 11 disposable PostgreSQL 17 contracts. The latter force both acquisition orders for account enrollment racing browser/mobile direct exchange and verify that the second transaction waits before checking identity. Production PostgreSQL is excluded from these tests.
