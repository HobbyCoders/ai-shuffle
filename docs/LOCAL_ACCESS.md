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

## Upgrading an existing installation

1. Validate a matching database, configuration and durable-key backup. Stop old
   application writers sharing the database before the first key adoption.
2. Start the new build with `SHUFFLE_LOCAL_ACCESS=false`. Keep the legacy
   encryption password only for the first successful key adoption.
3. The connector verifies that its protected local listener is live, requests
   the hub's monotonic upgrade to that origin, and verifies the public route
   reaches the same worker through protected ingress. Existing tunnel identity
   and device authorization are retained.
4. Verify existing phone access. Enable local access, remove
   the legacy password from deployment configuration, and verify a fresh-process
   restart reads the stored credentials through its persistent key.
5. Retain a matched recovery set. See [restore and key recovery](RESTORE.md).

Keep the old backup/configuration matched to its old data generation. Do not
launch an old application image against the migrated live database to test
rollback. Test rollback and fresh installation with isolated copies first.

Local access is the only way a browser operates the worker: with
`SHUFFLE_LOCAL_ACCESS=false` the web UI is unavailable until it is enabled
again. Credential-key recovery is separate and is described in the restore
guide.
