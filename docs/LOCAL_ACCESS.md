# Local setup and optional remote access

Start Shuffle with its normal desktop launcher or Docker entrypoint. First-time
setup asks how to start (clean install, sign in to a Shuffle account, or join
another worker with its invite), then for the workspace and, optionally, model
providers and GitHub. It does not create a username or password and does not
require a Shuffle account.

The local listener (port 8000 by default) grants owner access to direct local/LAN
requests. Tailscale counts as local: peers in 100.64.0.0/10 and MagicDNS names
under `ts.net` are trusted like LAN addresses. Everyone allowed to reach that
trusted interface on your LAN or tailnet can operate the installation. Existing
Devices and scoped integration API keys continue to work; supplying an
integration credential retains its restricted role. Public hostnames, public
source addresses, forwarded proxy requests, and cross-site browser requests do
not receive automatic local authority.

Shuffle is not hosted on the internet and never asks its owner to sign in: there
is no login page, password, or browser account sign-in. For phone access, open
**Devices → Remote access**, name the worker, and link it to your Shuffle account
from the Android app. Shuffle configures its managed tunnel automatically. The
remote listener accepts only devices paired that way; browsers must use the
local listener.

## Listener boundary

The built-in server runs one application and one set of background services with
two sockets:

| Socket | Purpose |
| --- | --- |
| Configured local port, normally 8000 | Direct local/LAN owner access |
| 127.0.0.1:8001 | Authenticated managed remote ingress |

The remote listener is loopback-only, including inside the Docker container.
It is not an additional published Docker port. The connector runs in that same
network namespace. Never forward a public proxy or tunnel to the local owner
listener. Public reverse proxies must use the authenticated remote listener.

The application identifies ingress using the actual server socket, with proxy
header rewriting disabled. Headers cannot grant local authority. Use the normal
desktop or Docker managed launcher so this boundary is installed;
launching the bare ASGI app with a custom server does not grant local access.

## Agent-browser preview access

An agent session's browser tools cannot always present themselves as the local
owner: they may run on another host, and a branch's Vite dev server adds
forwarded headers that the local listener rejects on purpose. For that one
case the harness mints a session-scoped preview token (`app/core/preview_access.py`)
and names it in the session's "# Workspace facts" card. Opening
`/api/v1/auth/preview-access?token=...&next=/` once sets an HttpOnly cookie
that the rest of the visit rides on, including through the Vite proxy.

The cookie is honoured only on the local listener from a private-network
peer, only while the bound session exists, and expires on its own. It never
grants the mutations that change ownership or reach: auth, pairing,
credentials, settings, network, plugins and the internal control plane refuse
unsafe methods on it. The remote listener ignores it entirely. It is not a
login: the local owner boundary above is unchanged.

### Obsolete session cookies in an isolated preview

Browsers scope cookies to the host, not the port, so an isolated branch
preview opened in the agent browser often also receives the `session` cookie
of a different installation on the same host (or one left over from an
earlier preview). That cookie is not valid here, and it does not stop the
preview cookie from working: every surface (`/api/v1/auth/status`, protected
API reads and writes, the session's event stream and the WebSocket) honours a
valid preview grant even when such an obsolete cookie is present, so status
and the protected reads agree. The server leaves the obsolete cookie alone
rather than deleting it, because on a shared host it can be another
installation's live login.

What the obsolete cookie never does:

- It never falls back to passwordless local access. Only a valid preview
  grant is honoured behind it; without one the request is refused as before.
- It never lifts the preview limits. The mutation ceiling above and the
  same-origin requirement for unsafe methods still apply.
- A failed `Authorization: Bearer` credential is explicit, so it is never
  replaced by the preview grant: the request is refused.

To recover a preview that still shows "Not authenticated":

1. Open `/api/v1/auth/status` on the preview origin. If `preview_access` is
   `false`, the preview cookie itself is missing, expired, or bound to a
   session that does not exist in this preview's database, or the browser
   is not reaching the local listener from a private network. Open the
   session's preview URL (`/api/v1/auth/preview-access?token=...`) again, or
   mint a new one for a session in this preview's data directory.
2. If an `Authorization` header is being sent (for example by a test
   harness), remove it; an invalid Bearer is refused on purpose.
3. Clearing the obsolete `session` cookie for the preview host is safe but
   no longer required.

## Upgrading an existing installation

1. Adopt the credential key first; see
   [credential key recovery](credential-key-recovery.md#upgrade-an-existing-installation).
   Run that first start with `SHUFFLE_LOCAL_ACCESS=false`.
2. The connector verifies that its protected local listener is live, requests
   the hub's monotonic upgrade to that origin, and verifies the public route
   reaches the same worker through protected ingress. Existing tunnel identity
   and device authorization are retained.
3. Verify existing phone access, then enable local access.

Keep the old backup/configuration matched to its old data generation. Do not
launch an old application image against the migrated live database to test
rollback. Test rollback and fresh installation with isolated copies first.

Local access is the only way a browser operates the worker: with
`SHUFFLE_LOCAL_ACCESS=false` the web UI is unavailable until it is enabled
again. Credential-key recovery is separate and is described in the restore
guide.
