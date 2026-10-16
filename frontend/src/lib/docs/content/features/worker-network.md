<!--
title: Worker network
description: Connect your workers so they share settings, keys and chats
order: 11
-->

# Worker network

A worker is a machine running AI Shuffle: a Docker install or the desktop app. Each worker can run on its own. Connect your workers into a **network** and they act like one app:

- Change a setting or add a provider key once, and every worker has it.
- Every chat shows up on every worker. It runs on the worker that owns it, and you can use it from any of them.

Everything the network shares is encrypted. HobbyCoders keeps an encrypted backup copy so a new worker can catch up even when your other workers are off. It cannot read it.

## Connect a worker

Open **Settings → Devices** (owner only). A worker not yet in a network shows **Set up this worker**. Pick one:

- **Sign in to your HobbyCoders account.** Use **Sign in** at the top of the section, the same account as your other workers. The first worker starts the network, and later workers join it with nothing to approve. Phones signed in to the account can see and use every worker on it.
- **Join with an invite.** On a worker already in the network, open **Settings → Devices → Add device** and set **Kind** to **Worker**. Scan the QR code or paste the link on the new worker. It joins at once and starts syncing.
- **Keep separate settings.** The worker stays on its own. You can connect it later.

To add a phone or browser, use **Add device → Phone or browser**. A phone signed in to your account needs no code.

Invites are meant for your home network or a VPN such as Tailscale. Workers signed in to your account always reach each other through your secure remote access connection.

## What syncs

**Shared:** preferences, settings, provider keys and accounts, agents, templates, tags, permission rules, hooks, MCP servers, webhooks, projects, notes, plugins, memory and skills, automations, connectors, and your chats.

**Kept on each worker:** folders and project checkouts, ports and addresses, logs, usage numbers, and the work currently running there.

## Use a chat on another worker

The composer's worker picker lists every online worker. Pick one, and the chat runs there.

From any other worker you can read that chat, send messages, answer prompts and stop it. Attachments are passed along too. Git buttons (Commit, Push, Pull, Create PR, Finish and Ship It) act on the owning worker's copy of the project.

If the owning worker is offline, the chat is read-only and offers **Fork here**, which starts a new chat on your current worker with the conversation so far. The original is never changed.

An agent can also run a subagent on another worker while the chat stays here. See [Running a subagent on another worker](/docs/features/subagents#running-a-subagent-on-another-worker).

Automations and connectors run on one worker each. Your other workers show them but do not run them.

## Conflicts

If two workers change the same setting while they cannot reach each other, the later change wins. The other version is kept for 30 days, so nothing is lost without a trace.

## Remove a device

Remove a worker, phone or browser from **Settings → Devices**. A removed worker stops receiving your settings. Its chats stay on your other workers as read-only copies, and **Fork here** continues them.

If you remove a worker you no longer trust, also change the provider keys it had, in **Settings → Providers**.

## Device-token upgrade protection

This compatibility release sends both the legacy device-key proof and a versioned
endpoint-bound proof on every token request; rejection never retries legacy-only.
The bound transcript is the canonical JSON array
`["shuffle-device-token-bound-v1", challenge, server_device_id, canonical_base_endpoint]`.
The server checks the endpoint against its current locally advertised tunnel and
LAN addresses, never against HTTP Host or forwarded headers. Tunnel rotation
accepts the new advertisement immediately and rejects the removed address,
including challenges issued before rotation; peers must refresh discovery.
A valid proof for an unadvertised address receives HTTP 403 with
`detail.code="endpoint_not_advertised"`. The response reveals no other server
addresses. The dialing worker records an actionable message in its peer status,
shows it in the worker's Devices detail and logs a warning at most once per five
minutes per peer. Invalid signatures still receive the generic binding refusal.

Before upgrading, make sure the URL peers actually dial is advertised. An invite,
manual address or cached URL may differ from the worker's advertised address
(Docker port mappings, reverse proxies and hostname/IP aliases are common examples).
New clients send a bound proof even before pinning, so an unadvertised alias is
refused even with `NETWORK_REQUIRE_TOKEN_BINDING=false`.

On the worker being dialed, open **Settings → Devices → This worker → Network →
Advertised addresses** and enter the additional base URLs, separated by commas.
These are explicit owner-approved aliases, including public reverse proxies,
hostnames and mapped ports. They remain advertised alongside a managed tunnel and
do not depend on the LAN-direct switch. Configure only URLs you control that
actually reach this worker; they are shared with your network, not secrets.
HTTPS is recommended: adding an HTTP alias does not encrypt bearer tokens.

At most **two additional URLs**, each at most **512 characters**, are accepted.
Use `http://` or `https://`, ASCII (punycode) hostnames, and an optional base path;
credentials, queries, fragments, Unicode hosts and ambiguous paths are refused.
Saving replaces the alias list, immediately updates the binding allowlist, and
publishes the same signed presence used for primary addresses. Removing an alias
makes new token requests to it fail; existing short-lived tokens are not revoked.
The configuration is local to this worker and persists across restarts.

The owner-only `GET /api/v1/network/advertised-addresses` API returns
`addresses` (the additional URLs) and `advertised` (the complete dial list).
`PUT` with `{"addresses":["https://proxy.example/worker"]}` replaces the extras;
`{"addresses":[]}` clears them. Peer device tokens cannot read or change this
setting. Previous-release peers still receive the ordinary endpoint list, bounded
to their eight-address protocol limit; there is no membership migration.

For direct workers without a managed tunnel, the existing `CLUSTER_ADVERTISE_URL`
or owner-only `PUT /api/v1/cluster/advertise` API can also set the primary address.
The separate **Extra LAN addresses** control accepts private literal HTTP LAN
addresses while LAN-direct is on; use **Advertised addresses** for public aliases.

Canonical bases use lowercase HTTP(S) schemes and ASCII hosts, compressed bracketed
IPv6, omitted default ports (80/443), and no trailing slashes. International hostnames
must be supplied as ASCII A-labels (punycode); Unicode hosts are rejected in both
proofs and advertisements, never re-encoded with a different IDNA version. Path case and interior
slashes remain significant; unreserved percent escapes are decoded and other
escapes use uppercase hex. A DNS trailing dot remains significant. Credentials,
queries, fragments, scoped IPv6, whitespace, backslashes, dot path segments and
non-ASCII paths are rejected rather than guessed.

A successful bound proof, or a device-key-signed binding-support statement in an
authenticated exchange, permanently pins that peer to binding on the receiving
worker. Mutable version/capability fields cannot set or clear this local database
pin. It survives restarts, membership generation changes and re-admission of the
same identity. A new signing identity (new device id), a fresh network or recovery
onto a fresh database starts without those local pins. Downgrading a pinned peer
to an old release will fail token authentication; upgrade it again rather than
clearing security evidence.

Old peers still work **before pinning**. During this rollout an intermediary that
can redirect a peer's first connection can strip both the new proof and capability
statement and relay legacy authentication. Old servers have the same exposure.
Once all peers are upgraded, set `NETWORK_REQUIRE_TOKEN_BINDING=true` on each
worker to reject even unpinned legacy requests (default: `false`). The follow-up
protocol release must remove legacy acceptance after the rolling-upgrade window;
this compatibility release does not claim complete first-contact relay protection.
Binding also does not encrypt plain-HTTP bearer tokens: use HTTPS or the sealed
LAN transport, and keep advertised endpoint provenance protected.

## Your account's network wins

A worker with separate settings that later signs in to your account joins your account's network. Settings your account already has replace the worker's own, and anything only that worker had is added.

To manage the network from your own code, see the [Devices and Network API](/docs/api-reference/devices).
