# Mobile web session reservations

An Android connection can be interrupted while its HTTP request keeps running on
the worker. A later connection must not lose its cookie when that older request
eventually finishes. Upgraded clients reserve a web mint before requesting the
session token.

1. `POST /api/v1/mobile/web-session/reservations` with the existing mobile bearer
   key returns `reservation`, `expires_at`, and `expires_in: 120`. It does not set
   a cookie or invalidate any current web or native session.
2. If the same account, saved worker, and connection attempt are still current,
   send the reservation in `X-Shuffle-Web-Session-Reservation` on the existing
   `POST /api/v1/mobile/web-session` request with `{"purpose":"web"}`.
3. Only the newest unexpired reservation for that paired client and exact mobile
   credential can commit. The worker consumes it atomically with the usual web
   session rotation. An expired, superseded, or consumed reservation returns
   HTTP 409 without a cookie. A client may reserve and retry once while its
   account and connection attempt remain current.

Reservations are random, stored only as hashes, and require the mobile key for
both requests. Reserving late can supersede another pending reservation but
cannot invalidate an already installed cookie. Token responses use `no-store`.
Clients use `expires_in` with a local monotonic deadline, avoiding device/server
clock differences. Native voice does not use reservations.

Revocation, deletion, credential repair, and matching-current web logout retire
pending reservations. Successful legacy web rotation also retires them. Native
session rotation and stale-token logout leave pending web reservations intact.
Current mobile identity, key, expiry and scope are checked again under the same
client-first transaction lock before reserving or committing.

Older clients can still mint without the header. Updated Android clients may use
that path only when the worker explicitly lacks the reservation endpoint; a
timeout, HTTP 503, malformed response, or superseded reservation must not trigger
that fallback. Older workers do not provide the server-side ordering guarantee
until updated. No extra web session tokens remain valid after rotation.
