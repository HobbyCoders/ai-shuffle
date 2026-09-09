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

Everything the network shares is encrypted with one network key that only the
member devices hold. The Shuffle account service keeps an encrypted copy so a
new device can restore when every worker is off; it cannot read anything.

## Three ways to start a worker

On first run a worker offers three choices:

- **Start a new worker.** Creates a network of one. Save the recovery key it
  shows; it is displayed once.
- **Join from another worker.** On an existing worker open Settings > Devices
  and choose *Invite another worker*. Scan the QR or type the code and the
  endpoint on the new worker, then compare the **fingerprint** shown on both
  screens. Approve on the existing worker. The new worker copies a snapshot
  and starts syncing.
- **Sign in to Shuffle.** Sign the worker into the same account as your other
  workers. An online worker shows the new device with its fingerprint and
  approves it. If none is online, enter the recovery key.

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

Remove a device from Settings > Devices. The network key rotates for the
remaining members and the removed device's later writes are rejected. Any
secret it had already received should be treated as compromised; rotate
provider keys from Settings > Providers.

## Migrating a shared database

Each worker uses its own private managed PostgreSQL database. New installations
do not attach to or import an existing shared database. Keep the existing
deployment and its backups intact until a separately tested migration is approved.
See `docs/network/migration.md` for preservation requirements.
