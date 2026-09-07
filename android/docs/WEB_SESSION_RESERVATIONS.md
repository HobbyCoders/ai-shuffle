# Reserved WebView sessions

Opening or refreshing chat first authenticates an empty JSON POST to
`/api/v1/mobile/web-session/reservations` using the selected worker key. The
response contains an opaque `wsm_` reservation, ISO `expires_at`, and integer
`expires_in` (1–120 seconds). Android budgets that lifetime from the monotonic
request start; phone/server wall clocks need not agree.

The reservation does not rotate the active WEB cookie. Android commits with
`POST /api/v1/mobile/web-session`, JSON `{"purpose":"web"}`, and
`X-Shuffle-Web-Session-Reservation`. The worker atomically rejects an expired,
consumed, or superseded reservation with 409 without changing the active cookie.
Only one 409 retry is allowed, while the same screen/worker/account remains
current. NATIVE voice requests retain their independent purpose and send no
reservation.

Stopping chat cancels its attempt. Returning to an interrupted opening starts a
new attempt after RESUMED. The saved worker identity, HTTPS origin, device/account
ownership, worker/Cloudflare credentials, and account credential epoch are checked
before reserve, commit, cookie write, and navigation. Display-name and push metadata
refreshes preserve the chat authorization. Process-local
origin fences cover replacement Activity instances. Accepted CookieManager writes
are drained before newer writes because cancellation cannot undo an asynchronous
cookie operation.

All requests use the saved exact HTTPS origin, omit browser/account cookies, and
send optional Cloudflare service headers only to that origin. Redirects are not
followed. Mobile HTTP calls have a 45-second total limit; reservation bodies are
limited to 8 KiB. No keys, reservations, or session cookies are logged.

An old worker can be used only after an explicit 404 JSON `detail: "Not Found"`
or 405 JSON `detail: "Method Not Allowed"` from the reservation route. HTML,
transient errors, bad data, 401, or 409 never trigger a downgrade. Once an attempt
has received a reservation, its retry cannot downgrade. Legacy mints serialize
until each blocking call returns, including when its UI coroutine is canceled.
**Old workers cannot guarantee ordering after a transport timeout with an
ambiguous server commit. Update the worker for the reservation guarantee.**

`WebSessionReservationTest` checks request scoping, strict parsing, monotonic
lifetime, fallback, cross-Activity attempt fences, and delayed cookie callbacks.
`WebSessionReservationSmokeTest` runs the real ChatActivity, CookieManager, and
contained WebView against intercepted synthetic HTTPS responses. It checks late
commit rejection, late canceled reservations, valid-cookie reload, bounded retry,
legacy/error behavior, and account/worker changes during an awaited response.
The separate frozen pre-fix reproduction remains outside this source tree.
