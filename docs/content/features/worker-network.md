<!--
title: Worker network
description: Connect independent workers so they share settings, keys and sessions
order: 11
-->

# Worker network

Every worker runs by itself: its own database, its own device identity, no
account required. Connecting workers into a **network** makes them feel like
one app. Set a sidebar width once, add a provider key once, and every worker
in the network has it. Every session shows up everywhere; it runs on the
worker that owns it, and any worker can drive it.

Shared data is encrypted with keys held by member devices. Workers sync directly
over your LAN or their managed remote connections. They keep changes locally
while disconnected and catch up when another worker returns. Cloud backup is
optional and off by default; enable it on a worker in Settings > Devices if you
want an encrypted cloud copy, subject to the backup allowances.

## Three ways to start a worker

On first run a worker offers three choices:

- **Clean install.** Fresh settings on this computer. You can sign in or
  connect other devices at any time from Settings > Devices.
- **Sign in to your Shuffle account.** Sign the worker into the same account
  as your other workers. The first worker on an account founds its network;
  every later worker is admitted by whichever of your workers is online, with
  nothing to approve. If none is online, it waits for one to return. An existing
  cloud backup can instead be restored with its recovery key. Phones signed
  in to the account see and control every worker on it.
- **Join with an invite.** On an existing worker open Settings > Devices >
  *Add device* > *Worker*. Scan the QR code or paste the link on the new
  worker. The one-time code is the authorization: the new worker joins as
  soon as it reaches the inviter, copies a snapshot and starts syncing.

Adding a phone or browser works the same way: *Add device* > *Phone or
browser* shows a QR code and link that connect it immediately, and a phone
signed in to your account needs no code at all.

Existing managed embedded installations join a network of one on upgrade;
external/shared-database installations are not automatically converted. Preserve
those deployments and backups until an explicit migration is approved.

## What syncs

Shared across the network: preferences and UI state, system settings that are
not bound to a host, provider keys and accounts, profiles, templates, tags,
permission rules, hooks, MCP definitions, webhooks, project identities, notes
and attachments, plugin state, memory facts and skills, automation and
connector definitions, the session catalog and transcripts.

Local to each worker: workspace paths, checkouts and worktrees, ports and
advertise URLs, credential custody, the account binding, runs and queues,
usage snapshots, logs.

Settings > Devices > *What syncs* lists the exact policy.

## Running sessions on another worker

The composer's runner chip lists every online worker in the network with its
capabilities. Pick one and the session is born there. From any other worker
the session is a live mirror: you can read it, send messages, answer
permission prompts and stop it; the owner executes. If the owner is offline
the mirror is read-only and offers **Fork here**, which starts a new session
on the current worker with the transcript as context. The original is never
changed.

Automations and connectors have one owner device too. A definition restored
on a new worker stays inactive until you assign it.

## Conflicts

Two workers editing the same setting while disconnected both keep the higher
write; the losing version is listed under Settings > Devices > Conflicts for
30 days so nothing is silently lost.

## Removing a device

Remove a worker, phone or browser from Settings > Devices. A removed worker
stops receiving your settings and its later writes are rejected; the
remaining workers switch to a fresh network key on their own. Any secret the
worker had already received should be treated as compromised; rotate provider
keys from Settings > Providers.

## The account always wins

A worker set up with a clean install that later signs in to an account which
already syncs other workers switches to the account's network on its own:
settings the account already has replace the worker's copies, and anything
only that worker has is added to the account. A worker signed in to an account
is always in that account's network; there is no separate state to keep.
*Join a different worker's network* under Settings > Devices > Advanced moves a
worker between networks with an invite in the same way.

## Existing shared-database installations

Each worker uses its own private managed PostgreSQL database. New installations
do not attach to or import an existing shared database. Keep the existing
deployment and its backups intact until a separately tested migration is
approved. See `docs/network/migration.md` for preservation requirements.
