# Reconnect one revoked managed worker

A revoked worker key is not repaired by logging out of Shuffle, removing a saved worker, or retrying account sync. Those actions can affect other workers or leave the phone without its saved key while same-device enrollment is refused.

When opening a managed worker returns HTTP 401, Android checks that worker's exact saved HTTPS origin. It offers **Reconnect this worker** only if `/api/v1/mobile/hub/endpoint` returns the saved worker ID, Boolean `ready: true`, Boolean `protected_ingress: true`, and `account_repair: "scoped-v1"`. Older workers show update guidance; no direct-enrollment fallback is attempted.

The worker's administrator must explicitly prepare the target phone's revoked authorization for repair and create a new one-time controller pairing code on that same worker. The repair form fixes the worker identity/address and accepts only the code. Preparation is a separate privileged action; Android never deletes or re-enables a worker credential itself.

The client sends `account_repair: "scoped-v1"`, `pairing_code`, and `device_info` to the existing `/api/v1/mobile/register`. The internal device ID is UUIDv5 with URL namespace and `shuffle-account-device:<worker ID>:<public phone ID>`, matching the backend. The backend must enforce its scoped repair contract: retained managed ownership, a revoked matching client, no current mobile row, and an unconsumed same-worker grant. It creates a new key and must not revive the old key.

Android sends no account token, old worker key or cookies. Optional Cloudflare Access headers stay on the exact saved HTTPS origin. Redirects and caching are disabled, requests are cancellable and bounded, and the endpoint capability is checked again before and after exchange. The response must contain the exact internal device ID, a fresh bounded `aim_` key different from the old key, a future expiry and `web:session` scope. Missing fields cannot silently default to the requested identity.

After rechecking the full saved worker and the current account session, Android replaces only that worker's `apiKey`. The account, public phone ID, worker IDs, origin, other saved fields and other workers remain unchanged. Stopping the form cancels requests and clears the entered code. Invalid, interrupted or stale results do not modify saved connections. A code consumed before a lost response requires a new explicit administrator preparation/code; the client never retries by unregistering or silently reviving authorization.

Tests use synthetic workers and transport interception, never a live grant or device mutation. They cover UUIDv5 parity, capability and response validation, credential-free requests, one-key-only replacement, old-worker update guidance, and canceled requests after account or worker changes.
