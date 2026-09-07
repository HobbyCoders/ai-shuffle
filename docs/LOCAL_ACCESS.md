# Local setup and optional remote access

Start Shuffle with its normal desktop launcher or Docker entrypoint. First-time
setup asks for the workspace and development tools/model providers. It does not
create a username or password and does not require a Shuffle account.

The local listener (port 8000 by default) grants owner access to direct local/LAN
requests. Everyone allowed to reach that trusted interface on your LAN can
operate the installation. Existing Devices and scoped integration API keys
continue to work; supplying an integration credential retains its restricted
role. Public hostnames, public source addresses, forwarded proxy requests, and
cross-site browser requests do not receive automatic local authority.

For remote access, open **Devices → Remote access**, name the worker, and link it
to your Shuffle account in the browser or Android app. Shuffle configures its
managed tunnel automatically. Remote users must prove that their account owns
the worker. A browser opens the account service in another tab and accepts the
completion proof only from that tab and service. If browser isolation blocks
that handoff, paste the one-use completion code into the original worker tab.

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
launcher (`python -m app.main` underneath) so this boundary is installed;
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
4. Verify remote sign-in and existing phone access. Enable local access, remove
   the legacy password from deployment configuration, and verify a fresh-process
   restart reads the stored credentials through its persistent key.
5. Retain a matched recovery set. See [restore and key recovery](RESTORE.md).

Keep the old backup/configuration matched to its old data generation. Do not
launch an old application image against the migrated live database to test
rollback. Test rollback and fresh installation with isolated copies first.

## Local access recovery

If local access is deliberately disabled and account access needs repair, run
`python -m app.core.owner_auth recovery` in the worker's local console. Open
`/login` through localhost (or an SSH forward terminating inside the same
container network namespace), expand **Local recovery**, and enter the code.
It expires after ten minutes and can be used once, giving fifteen minutes of
local access. This option is unavailable through the public tunnel or LAN.
Credential-key recovery is separate and is described in the restore guide.
