<!--
title: iMessage
description: Connect Claude to iMessage via a BlueBubbles server running on macOS
order: 8
-->

# iMessage Connector

## Overview

The iMessage connector allows Claude to send and receive iMessages through AI Shuffle. Under the hood, it communicates with a [BlueBubbles](https://bluebubbles.app/) server running on a Mac, which acts as a bridge to Apple's iMessage service. This means Claude can participate in your iMessage conversations, respond to texts, react to messages, and send files — all through the familiar iMessage interface on any Apple device.

Because iMessage is a proprietary Apple protocol with no public API, BlueBubbles is required as an intermediary. The connector handles all communication with the BlueBubbles API internally, so you only need to provide the server URL and password during setup.

## Prerequisites

- A **Mac computer** (Mac mini, MacBook, iMac, or Mac Pro) that will remain powered on and connected to the internet at all times
- **macOS Monterey (12.0)** or later
- An **Apple ID** signed into iMessage on the Mac
- **BlueBubbles server** installed and running on the Mac (see the [BlueBubbles connector guide](./bluebubbles.md) for detailed installation steps)
- The Mac must be on the same network as AI Shuffle, or the BlueBubbles server must be accessible via a public URL (e.g., through Cloudflare Tunnel or ngrok)

## Setup Guide

### 1. Install and configure BlueBubbles on your Mac

If you have not already set up BlueBubbles, follow the full installation guide in the [BlueBubbles connector documentation](./bluebubbles.md). Once the BlueBubbles server is running and you can see its status page, proceed to the next step.

### 2. Get your BlueBubbles server URL and password

Open the BlueBubbles server app on your Mac. On the main status screen, note:

- **Server URL** -- The address where the server is accessible (e.g., `http://192.168.1.50:1234` for local network, or a Cloudflare Tunnel URL like `https://bb.yourdomain.com`)
- **Password** -- The server password you set during BlueBubbles setup

### 3. Verify the BlueBubbles API is reachable

From the machine running AI Shuffle, confirm you can reach the BlueBubbles API:

```bash
curl "http://YOUR_BLUEBUBBLES_URL/api/v1/server?password=YOUR_PASSWORD"
```

You should receive a JSON response with server status information. If you get a connection error, check your network configuration and firewall rules.

### 4. Create the iMessage connector in AI Shuffle

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My iMessage",
    "connector_type": "imessage",
    "config": {
      "server_url": "http://192.168.1.50:1234",
      "password": "your_bluebubbles_password"
    }
  }'
```

Or add it through the AI Shuffle UI under **Settings > Connectors > Add Connector** and select **iMessage** from the connector type dropdown.

### 5. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

### 6. Test the connection

Send an iMessage to the phone number or Apple ID associated with the Mac running BlueBubbles. The message should appear in AI Shuffle, and Claude's response will be delivered back through iMessage.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `server_url` | string | Yes | The full URL of your BlueBubbles server (e.g., `http://192.168.1.50:1234` or `https://bb.yourdomain.com`) |
| `password` | string | Yes | The password configured in your BlueBubbles server settings |

## Capabilities

The iMessage connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `typing_indicator` | Yes | Shows a typing bubble in iMessage while Claude is generating a response |
| `reactions` | Yes | Claude can react to messages with tapback reactions (love, like, dislike, laugh, emphasis, question) |
| `rich_text` | Yes | Supports formatted text in messages |
| `file_upload` | Yes | Claude can send images, documents, and other files through iMessage |
| `read_receipts` | No | Not supported |
| `threads` | No | iMessage does not support threaded replies |
| `buttons` | No | iMessage does not support interactive buttons |

**Message length limit:** 4,096 characters per message. Messages exceeding this limit will be split into multiple parts automatically.

## Troubleshooting

### Messages are not arriving in AI Shuffle

- **Check BlueBubbles server status.** Open the BlueBubbles app on your Mac and verify it shows "Connected" status. If it shows disconnected, restart the server.
- **Verify iMessage is signed in.** Open the Messages app on the Mac and confirm your Apple ID is signed in and iMessage is enabled under **Messages > Settings > iMessage**.
- **Check the server URL.** Ensure the `server_url` in your connector config matches the URL shown in the BlueBubbles server app. Include the port number if using a local address.
- **Test API connectivity.** Run the curl command from step 3 above from the AI Shuffle host machine. If it fails, there is a network issue between AI Shuffle and the BlueBubbles server.

### Responses are not being sent back

- **Check connector activation.** Verify the connector shows as "active" in AI Shuffle settings or via the API.
- **Check BlueBubbles permissions.** On the Mac, ensure BlueBubbles has been granted Full Disk Access and Accessibility permissions in **System Settings > Privacy & Security**.
- **Review AI Shuffle logs.** Check the backend logs for error messages related to the iMessage connector.

### Typing indicators not showing

- Typing indicators require BlueBubbles Private API features to be enabled. In the BlueBubbles server app, go to **Settings > Features** and ensure the Private API is configured and connected.

### Mac goes to sleep and stops receiving messages

- **Disable sleep.** Go to **System Settings > Energy Saver** (or **Battery > Options** on laptops) and set the Mac to never sleep. For laptops, keep them plugged in at all times.
- **Use a keep-alive utility.** Tools like Amphetamine (free on the Mac App Store) can prevent sleep more reliably.
- **Consider a Mac mini.** A Mac mini is the most cost-effective dedicated machine for running BlueBubbles 24/7.

### Connection drops after network changes

- If the Mac's IP address changes (e.g., after a router restart), the `server_url` will become invalid. Either assign a static IP to the Mac in your router settings, or use a tunnel service like Cloudflare Tunnel for a stable URL.
