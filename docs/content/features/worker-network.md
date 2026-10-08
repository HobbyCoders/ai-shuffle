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

## Your account's network wins

A worker with separate settings that later signs in to your account joins your account's network. Settings your account already has replace the worker's own, and anything only that worker had is added.

To manage the network from your own code, see the [Devices and Network API](/docs/api-reference/devices).
