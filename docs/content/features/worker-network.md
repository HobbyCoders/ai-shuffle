<!--
title: Worker network
description: Connect independent workers so they share settings, keys and sessions
order: 11
-->

# Worker network

Every worker runs by itself: its own database, its own device identity, no account required. Connecting workers into a **network** makes them feel like one app. Set a sidebar width once, add a provider key once, and every worker in the network has it. Every session shows up everywhere; it runs on the worker that owns it, and any worker can drive it.

Everything the network shares is encrypted with one network key that only the member devices hold. The Shuffle account service keeps an encrypted copy so a new device can restore when every worker is off; it cannot read anything.

## Connecting a worker

Worker connections are managed in Settings > Devices (owner only). Until a worker joins a network, that section shows **Set up this worker**. The choices are:

- **Sign in to your Shuffle account.** Use **Sign in** at the top of the section to sign the worker into the same account as your other workers (the setup wizard's *Account & GitHub* step does the same). The first worker on an account founds its network; every later worker is admitted by whichever of your workers is online, with nothing to approve. Phones signed in to the account see and control every worker on it.
- **Join with an invite.** On an existing worker open Settings > Devices > *Add device* and set *Kind* to *Worker*. Scan the QR code or paste the link on the new worker. The one-time code is the authorization: the new worker joins as soon as it reaches the inviter, copies a snapshot and starts syncing.
- **Keep separate settings.** The worker keeps its own settings. You can sign in or connect other devices later.

Adding a phone or browser works the same way: *Add device* > *Phone or browser* shows a QR code and link that connect it immediately, and a phone signed in to your account needs no code at all.

Existing managed embedded installations join a network of one on upgrade. External or shared-database installations are not converted automatically; keep those deployments and their backups.

## How workers reach each other

Workers talk to each other directly, and one rule decides which address they use. A worker signed in to your Shuffle account has an encrypted tunnel address, and every other worker reaches it only through that tunnel, never over its plain `http://` local address, even if it was added with an invite code and even when both sit on the same network (the small extra latency is the price of one consistent rule). Plain local addresses are used only between workers that have no account link, which is why invite codes are meant for your home network or a VPN such as Tailscale; a plain `http://` invite to a public address is refused. A worker whose tunnel does not answer shows as offline (its sessions offer **Fork here**) rather than being contacted in the clear. If you sign in after pairing by code, the other workers switch to the tunnel on their own within a few seconds; nothing needs to be paired again.

## What syncs

Shared across the network: preferences and UI state, system settings that are not bound to a host, provider keys and accounts, agents, templates, tags, permission rules, hooks, MCP definitions, webhooks, project identities, notes and attachments, plugin state, memory facts and skills, automation and connector definitions, the session catalog and transcripts.

Local to each worker: workspace paths, checkouts and worktrees, ports and advertise URLs, credential custody, the account binding, runs and queues, usage snapshots, logs.

The exact policy is available from `GET /api/v1/network/policy`.

## Running sessions on another worker

The composer's runner chip lists every online worker in the network with its capabilities. Pick one and the session is born there. From any other worker the session is a live mirror: you can read it, send messages, answer permission prompts and stop it; the owner executes. The composer shows the session's Away mode as the owner has it, and turning Away on or off from any worker changes it on the owner; other open windows and phones pick up the change within a few seconds. Stop remains available while the session is working, has background work, or the remote owner reports a pending permission/question. Typing a draft adds Send alongside Stop; it never removes Stop. Whitespace-only drafts count as empty. Stopping also stops the session’s background work. Existing code and voice project sessions also accept attachments when the viewing worker has no checkout: files are staged per session and delivered to the owner before the turn starts. Uncreated remote-only drafts still need a local checkout to attach files. If the owner is offline the mirror is read-only and offers **Fork here**, which starts a new session on the current worker with the transcript as context. The original is never changed. A session started with an API key stays that key's session on every worker, so the key's scope applies wherever you view it.

An agent can also run a Task subagent on another worker while the chat stays here. See [Running a subagent on another worker](/docs/features/subagents#running-a-subagent-on-another-worker) for what it needs and what travels.

Git controls work from any worker too: Commit, Push, Pull, Create PR, Finish and Ship It use your saved options and run against the owning worker’s checkout. Finish and Ship It show progress here and continue on the owner if you reload the page. If the owner is unreachable, reconnect it and use Retry; Git actions never fall back to a checkout on the viewing device. Both workers need a version that supports remote Git actions.

Automations and connectors have one owner device too, and only the owner runs them. A definition with no owner is claimed by one online worker. A definition whose owner is another device, including a removed one, stays inactive on this worker until you assign it here with `POST /api/v1/network/owners` (see the [Devices & Network API](/docs/api-reference/devices#definition-owners)); Settings has no control for this yet.

## Conflicts

Two workers editing the same setting while disconnected both keep the higher write; the losing version is kept for 30 days so nothing is silently lost, and can be listed with `GET /api/v1/network/conflicts`.

## Removing a device

Remove a worker, phone or browser from Settings > Devices. A removed worker stops receiving your settings and its later writes are rejected; the remaining workers switch to a fresh network key on their own. Sessions the removed worker owned stay on the other workers as read-only copies: messages, permission answers and stops are no longer sent to it, and **Fork here** continues the conversation on the current worker. Any secret the worker had already received should be treated as compromised; rotate provider keys from Settings > Providers.

## The account always wins

A worker set up with separate settings that later signs in to an account which already syncs other workers switches to the account's network on its own: settings the account already has replace the worker's copies, and anything only that worker has is added to the account. A worker signed in to an account is always in that account's network; there is no separate state to keep. Joining another worker's network with its invite (Settings > Devices) moves a worker between networks the same way.

## Existing shared-database installations

Each worker uses its own private managed PostgreSQL database. New installations do not attach to or import an existing shared database. Keep the existing deployment and its backups; they are not converted automatically.
