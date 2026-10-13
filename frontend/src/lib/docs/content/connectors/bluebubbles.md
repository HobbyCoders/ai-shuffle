<!--
title: BlueBubbles
description: Send iMessages from AI Shuffle through a BlueBubbles server
order: 9
-->

# BlueBubbles

Send iMessages from AI Shuffle through [BlueBubbles](https://bluebubbles.app/), a free app that runs on a Mac. AI Shuffle can run on any other machine.

**Sending only.** Incoming iMessages do not reach your agent. Send messages from an [automation](/docs/features/automations) with a **Connector Action** step.

## You need

- A Mac with macOS 12 (Monterey) or later, always on, signed in to iMessage.
- A network path from AI Shuffle to the Mac: the same home network, or a tunnel.

## Set up

### 1. Install BlueBubbles Server

Download the macOS server from [bluebubbles.app/downloads](https://bluebubbles.app/downloads/) and drag it into **Applications**, or run `brew install --cask bluebubbles`. Open it.

### 2. Give it permissions

In **System Settings → Privacy & Security**:

- **Full Disk Access:** add BlueBubbles Server. Required.
- **Accessibility:** add it if you turn on the Private API (below).
- **Contacts:** optional, to show contact names.

Restart BlueBubbles Server afterwards.

### 3. Finish the BlueBubbles setup

1. Set a strong **server password**.
2. Pick a connection method: **Cloudflare Tunnel** (no port forwarding), **Dynamic DNS**, or **Local Network Only** when AI Shuffle is on the same network.
3. Optional: turn on the **Private API**. Tapbacks, renaming group chats and leaving chats need it. Plain messages do not.
4. Keep the default port, `1234`, unless you need another.

The status page should show **Running**.

### 4. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **BlueBubbles**. Choose **Install BlueBubbles** if asked.
2. Choose **Connect account** and enter the **Server URL** with its port, such as `http://192.168.1.50:1234`, and the **API Password**.
3. Add at least one **Allowed Handle**: a phone number or email address.
4. Choose **Create Connector**, then turn on **Activate**.

### 5. Send a test message

Make an automation with a **Connector Action** step that runs `send_message`, run it, and check the message arrives.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `server_url` | Yes | The BlueBubbles address with port |
| `password` | Yes | The BlueBubbles server password |
| `allowed_handles` | Yes | The phone numbers and emails it can message |

## Actions

BlueBubbles actions send messages and tapbacks, list chats, look up a chat, rename a chat and leave a chat. Run them from an automation's **Connector Action** step. They do not ask for approval there.

Messages are plain text. Messages longer than 4,000 characters are split.

## Troubleshooting

**BlueBubbles will not start.** Check you have macOS 12 or later, reinstall the latest version, and look for "BlueBubbles" in the macOS Console app.

**The Private API shows Disconnected.** Allow **Accessibility**, restart BlueBubbles, and try **Settings → Private API → Re-install**. Newer macOS versions may need extra steps: see the [BlueBubbles Private API guide](https://docs.bluebubbles.app/private-api/).

**Messages do not send.**

- Send a message by hand in Messages on the Mac to check iMessage works.
- Check the password matches BlueBubbles exactly.
- Check AI Shuffle can reach the server URL and port.
- Check **Logs** in BlueBubbles Server.

**The Cloudflare Tunnel drops.** Update BlueBubbles and check the Mac's internet connection. On the same network, a local address is more stable.

**BlueBubbles uses a lot of memory.** Clear its cache in **Settings → Database**, restart it, and sync fewer chats on a Mac with 8 GB or less.
