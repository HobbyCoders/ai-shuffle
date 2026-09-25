<!--
title: BlueBubbles
description: Direct BlueBubbles connector for sending iMessages from AI Shuffle
order: 9
-->

# BlueBubbles Connector

## Overview

The BlueBubbles connector provides a direct integration between AI Shuffle and a [BlueBubbles](https://bluebubbles.app/) server. BlueBubbles is an open-source application that runs on macOS and exposes Apple's iMessage service through a REST API and WebSocket interface. This connector communicates directly with the BlueBubbles API, letting AI Shuffle send iMessages. It does not receive messages (see [Send a test message](#7-send-a-test-message)).

The [iMessage connector](/docs/connectors/imessage) is a separate option: it runs the `imsg` CLI on the Mac that hosts AI Shuffle, and it does not use BlueBubbles. Use BlueBubbles when AI Shuffle runs on a different machine from the Mac.

## Prerequisites

- A **Mac computer** (Mac mini recommended for always-on operation) running **macOS Monterey (12.0)** or later
- An **Apple ID** signed into iMessage on the Mac
- **Xcode Command Line Tools** installed (`xcode-select --install`)
- Network access between the AI Shuffle host and the Mac (local network or tunnel)

## Setup Guide

### 1. Download and install BlueBubbles server

Download the latest BlueBubbles server release from the [official website](https://bluebubbles.app/downloads/):

1. Go to [https://bluebubbles.app/downloads/](https://bluebubbles.app/downloads/) and download the macOS Server `.dmg` file
2. Open the `.dmg` and drag **BlueBubbles Server** into your Applications folder
3. Launch BlueBubbles Server from Applications

Alternatively, install via Homebrew:

```bash
brew install --cask bluebubbles
```

### 2. Grant required macOS permissions

BlueBubbles needs several permissions to function. macOS will prompt for these on first launch, but you can also set them manually:

1. **Full Disk Access** -- Required to read the iMessage database
   - Go to **System Settings > Privacy & Security > Full Disk Access**
   - Click the `+` button and add BlueBubbles Server
2. **Accessibility** (optional) -- Required only if you enable the BlueBubbles Private API. The Private API is needed for the AI Shuffle actions that send tapbacks, rename group chats or leave chats. Plain text sending works without it.
   - Go to **System Settings > Privacy & Security > Accessibility**
   - Click the `+` button and add BlueBubbles Server
3. **Contacts** (optional) -- Allows BlueBubbles to resolve contact names
   - Go to **System Settings > Privacy & Security > Contacts**
   - Grant access to BlueBubbles Server

After granting permissions, restart BlueBubbles Server.

### 3. Configure the BlueBubbles server

On first launch, BlueBubbles will walk you through a setup wizard:

1. **Set a server password** -- Choose a strong password. You will need this for the AI Shuffle connector config.
2. **Choose a connection method** -- Options include:
   - **Cloudflare Tunnel** (recommended for remote access) -- Free, stable, and requires no port forwarding
   - **Dynamic DNS** -- Requires port forwarding on your router
   - **Local Network Only** -- Only works if AI Shuffle runs on the same network
3. **Configure the Private API** (optional) -- Required for the AI Shuffle actions that send tapbacks, rename group chats or leave chats. Plain text sending works without it.
4. **Set the port** -- Default is `1234`. Change if needed.

### 4. Verify the server is running

Once setup is complete, the BlueBubbles status page should show:

- Server status: **Running**
- Private API: **Connected** (if enabled)
- Connection method: Your chosen method with a URL

Test the API from your AI Shuffle host:

```bash
# Check server info
curl "http://YOUR_BLUEBUBBLES_URL:1234/api/v1/server?password=YOUR_PASSWORD"

# List recent chats
curl "http://YOUR_BLUEBUBBLES_URL:1234/api/v1/chat?password=YOUR_PASSWORD&limit=5"
```

### 5. Create the BlueBubbles connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **BlueBubbles**.
2. Click **Install BlueBubbles** if prompted, then click **Connect account**.
3. Fill in the configuration fields:
   - **Server URL**: Full URL of the BlueBubbles server including port (e.g., `http://192.168.1.50:1234`)
   - **API Password**: The server password you set in BlueBubbles
   - **Allowed Handles**: At least one phone number or email address. The connector will not start without one.
4. Click **Create Connector**.

To create the connector through the API instead, see [API Management](/docs/connectors#api-management); the `config` object takes the fields in the table below.

### 6. Activate the connector

Turn on the connection's **Activate** switch.

### 7. Send a test message

With the connector active, AI Shuffle can send outgoing iMessages through the BlueBubbles REST API. Trigger a send from an [automation](/docs/features/automations) with a **Connector Action** step (`send_message`) and confirm the message arrives on the recipient's device.

> **Note:** The BlueBubbles connector does not receive messages. AI Shuffle has no URL that BlueBubbles can send its webhooks to, so incoming iMessages never reach your agent.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `server_url` | string | Yes | Full URL of the BlueBubbles server including port (e.g., `http://192.168.1.50:1234` or `https://bb.yourdomain.com`) |
| `password` | string | Yes | The server password set during BlueBubbles configuration |
| `allowed_handles` | list | Yes (to enable) | Phone numbers or emails the connector is allowed to work with. At least one entry is required before the connector can be enabled. |

## Capabilities

The BlueBubbles connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `typing_indicator` | No | Not advertised by the connector |
| `reactions` | No | Not advertised by the connector |
| `rich_text` | No | Messages are sent as plain text |
| `file_upload` | No | Not advertised by the connector |
| `read_receipts` | No | Not supported by the connector |
| `threads` | No | iMessage does not have a threading model |
| `buttons` | No | iMessage does not support interactive buttons |

**Message length limit:** 4,000 characters per message. Longer messages are automatically split.

## AI Actions

BlueBubbles has 6 actions: send message, send tapback reaction, list chats, get chat info, rename chat and leave chat.

Your agent does not get these actions in its sessions, because no BlueBubbles chat ever reaches it. You can run them from an [automation](/docs/features/automations) with a **Connector Action** step. Actions run this way, including `leave_chat`, do not ask for approval.

## Troubleshooting

### BlueBubbles server won't start

- **Check macOS version.** BlueBubbles requires macOS Monterey (12.0) or later.
- **Reinstall.** Delete BlueBubbles from Applications and reinstall from the latest `.dmg`.
- **Check Console logs.** Open the macOS Console app and filter for "BlueBubbles" to see detailed error messages.

### Private API not connecting

The Private API is required for the tapback, rename chat and leave chat actions. If it shows "Disconnected":

1. Ensure you have granted **Accessibility** permission to BlueBubbles
2. Restart BlueBubbles Server
3. If still disconnected, try re-injecting the Private API helper from **BlueBubbles > Settings > Private API > Re-install**
4. On macOS Ventura and later, you may need to disable SIP (System Integrity Protection) for the Private API. See the [BlueBubbles documentation](https://docs.bluebubbles.app/private-api/) for instructions.

### Outgoing messages fail to send

- **Check iMessage sign-in.** Open Messages.app on the Mac and verify you can send a message manually.
- **Check password.** Ensure the `password` in your connector config exactly matches the one in BlueBubbles Server settings.
- **Check the server URL.** Confirm `server_url` (host and port) is reachable from the AI Shuffle host.
- **Check BlueBubbles logs.** In the BlueBubbles Server app, go to **Logs** to see detailed send/receive errors.

### Cloudflare Tunnel disconnects frequently

- Update BlueBubbles to the latest version -- tunnel stability is regularly improved.
- Check your Mac's internet connection stability.
- Consider switching to a static local network connection if AI Shuffle is on the same network.

### High memory usage on Mac

BlueBubbles caches message data. If memory usage grows large:

1. In BlueBubbles, go to **Settings > Database > Clear Cache**
2. Restart the BlueBubbles server
3. If the Mac has limited RAM (8 GB or less), consider limiting the number of chats BlueBubbles syncs
