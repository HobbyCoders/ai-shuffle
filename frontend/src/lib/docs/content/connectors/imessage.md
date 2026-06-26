<!--
title: iMessage
description: Connect Claude to iMessage using the imsg CLI on macOS
order: 8
-->

# iMessage Connector

## Overview

The iMessage connector allows Claude to send and receive iMessages through AI Shuffle. Under the hood, it drives the [`imsg`](https://github.com/) command-line tool, which exposes a JSON-RPC interface over a subprocess. AI Shuffle spawns `imsg rpc`, subscribes to incoming messages, and sends replies through the same connection. This means Claude can participate in your iMessage (and SMS) conversations and respond to texts — all from the Mac that owns the iMessage account.

Because iMessage is a proprietary Apple protocol with no public API, the connector relies on the `imsg` CLI talking directly to the local Messages database and `Messages.app`. As a result, **this connector only runs on macOS** — the machine running the AI Shuffle backend must be a Mac that is signed into iMessage.

## Prerequisites

- A **Mac computer** (Mac mini, MacBook, iMac, or Mac Pro) running the AI Shuffle backend, kept powered on and online
- An **Apple ID** signed into iMessage in the Messages app on that Mac
- The **`imsg` CLI** installed and on the `PATH` (or its full path known), with the permissions it needs to read the Messages database and send messages

## Setup Guide

### 1. Install the `imsg` CLI on the Mac

Install the `imsg` binary on the same Mac that runs the AI Shuffle backend. Confirm it is reachable from a terminal:

```bash
imsg --help
```

If you installed it somewhere not on your `PATH`, note the full path — you will provide it as the `cli_path` config field.

### 2. Verify the RPC interface works

The connector communicates with `imsg` over JSON-RPC on stdin/stdout. You can sanity-check that the CLI starts its RPC server:

```bash
imsg rpc
```

This launches the RPC process (it reads requests on stdin). Press Ctrl-C to exit. The first time you run it, macOS may prompt for permission to control Messages and access your data — grant it so the connector can read incoming messages and send replies.

### 3. Create the iMessage connector in AI Shuffle

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My iMessage",
    "connector_type": "imessage",
    "config": {
      "cli_path": "imsg",
      "service": "auto",
      "allowed_handles": ["+15551234567"]
    }
  }'
```

All config fields are optional. Omit `allowed_handles` to process messages from every sender. Or add the connector through the AI Shuffle UI under **Settings > Connectors > Add Connector** and select **iMessage** from the connector type dropdown.

### 4. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

When activated, the connector spawns `imsg rpc`, probes it with a `chats.list` request, and subscribes to incoming messages.

### 5. Test the connection

Send an iMessage to the Apple ID or phone number signed into Messages on the Mac. The message should appear in AI Shuffle, and Claude's response will be delivered back through iMessage. Messages sent by the account itself are ignored to avoid echoes.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cli_path` | string | No | Path to the `imsg` binary. Defaults to `imsg` (resolved on the `PATH`). |
| `service` | string | No | Default service to send through: `imessage`, `sms`, or `auto`. Defaults to `auto`. |
| `allowed_handles` | list of strings | No | If set, only messages from these phone numbers or email addresses are processed. Omit to allow all senders. |

## Capabilities

The iMessage connector exposes plain text messaging only. None of the optional rich-messaging capabilities are supported:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `typing_indicator` | No | Not supported |
| `reactions` | No | Tapback reactions are not supported |
| `rich_text` | No | Messages are sent as plain text |
| `file_upload` | No | Sending attachments is not supported |
| `read_receipts` | No | Not supported |
| `threads` | No | iMessage does not support threaded replies |
| `buttons` | No | iMessage does not support interactive buttons; permission prompts are sent as plain text and answered by replying `approve` or `deny` |

The connector does not define a maximum message length, so long messages are sent as-is without automatic splitting.

## Troubleshooting

### `imsg` CLI not found

- The connector reports "imsg CLI not found (macOS only)" when it cannot start the binary. Confirm `imsg` is installed and on the `PATH`, or set the `cli_path` config field to its full path.
- Remember this connector only runs on macOS — the AI Shuffle backend itself must be running on the Mac that is signed into iMessage.

### Messages are not arriving in AI Shuffle

- **Verify iMessage is signed in.** Open the Messages app on the Mac and confirm your Apple ID is signed in and iMessage is enabled under **Messages > Settings > iMessage**.
- **Check `allowed_handles`.** If you set `allowed_handles`, messages from any sender not in that list are ignored. Remove the field or add the sender's phone number / email to receive their messages.
- **Confirm the RPC subscription.** On activation the connector probes `imsg` with `chats.list` and then subscribes via `watch.subscribe`. If the probe fails, activation reports an "RPC probe failed" error — check that running `imsg rpc` manually works.

### Responses are not being sent back

- **Check connector activation.** Verify the connector shows as "active" in AI Shuffle settings or via the API.
- **Check macOS permissions.** Ensure the process running the AI Shuffle backend (and `imsg`) has the macOS permissions it needs to control Messages and access your data. You may have to grant these the first time `imsg rpc` runs.
- **Check the `service` setting.** If sends fail, try setting `service` to `imessage` or `sms` explicitly instead of `auto`.
- **Review AI Shuffle logs.** Check the backend logs for error messages related to the iMessage connector and the `imsg` RPC subprocess.

### Mac goes to sleep and stops receiving messages

- **Disable sleep.** Go to **System Settings > Energy Saver** (or **Battery > Options** on laptops) and set the Mac to never sleep. For laptops, keep them plugged in at all times.
- **Use a keep-alive utility.** Tools like Amphetamine (free on the Mac App Store) can prevent sleep more reliably.
- **Consider a Mac mini.** A Mac mini is the most cost-effective dedicated machine for running AI Shuffle and iMessage 24/7.
