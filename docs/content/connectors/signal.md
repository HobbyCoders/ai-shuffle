<!--
title: Signal
description: Connect AI Shuffle to Signal using signal-cli for privacy-focused encrypted messaging
order: 6
-->

# Signal Connector

## Overview

The Signal connector integrates your AI Shuffle agent with Signal, the privacy-focused messaging platform known for its end-to-end encryption. Unlike most other connectors that use cloud APIs, the Signal connector operates through **signal-cli**, an open-source client for Signal. This means all messages are handled locally on your server with full end-to-end encryption preserved.

Because Signal does not provide an official bot API, this connector requires a dedicated phone number registered with signal-cli. The phone number acts as the bot's identity on the Signal network. The connector talks to a **signal-cli REST API** service over HTTP (default `http://127.0.0.1:8080`): it sends messages via the REST endpoint and receives incoming messages over a Server-Sent Events (SSE) stream. This gives you a fully self-hosted, privacy-preserving bot without any cloud service dependency.

## Prerequisites

- A **dedicated phone number** for the bot (can be a VoIP number that receives SMS or voice calls for verification)
- A running **signal-cli REST API** service that wraps signal-cli and exposes an HTTP/SSE interface (e.g. the [bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) container)
- AI Shuffle backend with network access to the signal-cli REST API service (default `http://127.0.0.1:8080`)
- A host capable of running the signal-cli REST API service (Docker is the simplest option)

## Setup Guide

### Step 1: Run the signal-cli REST API Service

The connector communicates with a **signal-cli REST API** service over HTTP, so you do not invoke signal-cli directly. The easiest way to run it is the [bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) container, which bundles signal-cli and exposes the HTTP/SSE endpoints this connector expects.

```bash
docker run -d --name signal-api \
  -p 8080:8080 \
  -v signal-cli-data:/home/.local/share/signal-cli \
  -e MODE=json-rpc \
  bbernhard/signal-cli-rest-api
```

Run the service in `json-rpc` mode so that incoming messages are delivered over the Server-Sent Events stream the connector subscribes to.

**Verify the service is up:**
```bash
curl http://127.0.0.1:8080/api/v1/about
```
You should get back a JSON document listing the signal-cli versions.

### Step 2: Register the Phone Number

Register the dedicated phone number through the REST API. You may need to solve a captcha first (see [Registration with captcha](https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha)).

**Request an SMS verification code:**
```bash
curl -X POST http://127.0.0.1:8080/api/v1/register/+14155551234 \
  -H "Content-Type: application/json" \
  -d '{"captcha": "signalcaptcha://..."}'
```

Replace `+14155551234` with your actual phone number in international E.164 format.

### Step 3: Verify the Registration

After receiving the verification code via SMS, complete the registration:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/register/+14155551234/verify/123456 \
  -H "Content-Type: application/json"
```

Replace `123456` with the actual verification code you received.

### Step 4: Send a Test Message

Confirm the service can send messages on behalf of the bot:

```bash
curl -X POST http://127.0.0.1:8080/api/v2/send \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Hello from AI Shuffle!",
    "number": "+14155551234",
    "recipients": ["+15551234567"]
  }'
```

Replace `+15551234567` with your personal phone number. You should receive the message on your phone.

### Step 5: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **Signal**.
2. Click **Install Signal** if prompted, then click **Connect account**.
3. Fill in the configuration fields:
   - **Phone Number (E.164)**: The registered phone number in E.164 format (e.g., `+14155551234`)
   - **API URL** (optional): Base URL of the signal-cli REST API service. Defaults to `http://127.0.0.1:8080`.
   - **Allowed Phone Numbers**: At least one number. The bot only processes messages from these numbers.
4. Click **Create Connector**, then turn on the connection's **Activate** switch.

The connection needs an API user with an agent, or every message is refused. See [Common Setup Steps](/docs/connectors#common-setup-steps).

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account` | string | Yes | The phone number registered with signal-cli in international E.164 format (e.g., `+14155551234`). This is the identity of the bot on the Signal network. |
| `api_url` | string | No | Base URL of the signal-cli REST API service. Defaults to `http://127.0.0.1:8080`. Set this if the service runs on a different host or port. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `messages`, `presence`, `reactions`, `groups`. |
| `auto_approve_destructive` | string | No | Set to `true` to skip the text-based Approve/Deny prompt for destructive AI actions (leaving a group). Default: prompts are required. |
| `allowed_numbers` | list | Yes (to enable) | Only messages from these phone numbers (in E.164 format) are processed. At least one entry is required before the connector can be enabled. |

## Capabilities

The Signal connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Read receipts | Yes | Sends read receipts back to the sender via the REST API |
| File upload | No | Only text is sent |
| Typing indicator | Yes | Available to the AI via the `send_typing_indicator`/`stop_typing_indicator` actions (see below). Not sent automatically. |
| Reactions | Yes | Available to the AI via the `add_reaction`/`remove_reaction` actions (see below). Not sent automatically. |
| Rich text | No | Signal does not support text formatting in messages. All messages are sent as plain text. |
| Buttons | No | Signal does not support interactive buttons. Permission prompts are sent as text; reply `approve` or `deny`. |
| Threads | No | Signal does not have a thread model |
| End-to-end encryption | Yes | All messages are end-to-end encrypted by default (this is inherent to Signal) |

**Message Length Limit:** 4,000 characters per message. Messages exceeding this limit are automatically split.

## AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work).

Your agent can use 9 actions: send message, mark read, start/stop typing indicator, add/remove reaction, list groups, get group info and leave group.

- Message and group-lookup actions only work in the conversation the session started from and the connector's `allowed_numbers`.
- Signal has no buttons, so the one destructive action (`leave_group`) is confirmed by text: the connector sends a "Destructive Action Approval" message asking you to reply `approve` or `deny`, and only proceeds once a person replies `approve`.
- All other actions (messages, presence, reactions, group lookups) run immediately.

## Troubleshooting

### Connector cannot reach the REST API service

- **Service not running or wrong URL.** The connector health-checks `GET /api/v1/about` on connect. Verify the service responds:
  ```bash
  curl http://127.0.0.1:8080/api/v1/about
  ```
  If this fails, confirm the signal-cli REST API container is running and that the **API URL** in the connector config matches its address and port.
- **Network access.** Ensure the AI Shuffle backend can reach the service host and port (e.g., `8080`). If the service runs in a separate container, check the Docker network and port mapping.

### Registration fails

- **Phone number already registered.** If the phone number is already registered on another device, registration will take over the number and deregister the other device. Use a dedicated phone number that is not your personal Signal account.
- **Captcha required.** Signal may require a captcha for registration from certain IPs. Solve the captcha in a browser and include the token in the registration request body:
  ```bash
  curl -X POST http://127.0.0.1:8080/api/v1/register/+14155551234 \
    -H "Content-Type: application/json" \
    -d '{"captcha": "signalcaptcha://signal-recaptcha-v2.example..."}'
  ```
  Follow the instructions in the [signal-cli documentation](https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha) for solving the captcha.

### Bot does not receive messages

- **Service not in json-rpc mode.** The connector receives messages over the SSE stream at `GET /api/v1/receive/{account}`. Make sure the REST API service is started with `MODE=json-rpc` so the stream stays open.
- **Messages filtered out.** Only messages from the numbers in **Allowed Phone Numbers** are processed; all others are silently ignored. Add or correct numbers in the list to receive from other senders.

### "Unregistered user" error when sending messages

- The recipient phone number must be registered with Signal. The bot can only communicate with active Signal users.
- Verify the phone number format uses the E.164 international format (e.g., `+14155551234`, not `4155551234`).

### "Untrusted identity" warnings

- When a contact reinstalls Signal or changes devices, their identity key changes. By default, signal-cli may block messages to untrusted identities. Use the REST API to trust the contact's new identity:
  ```bash
  curl -X PUT http://127.0.0.1:8080/api/v1/identities/+14155551234/trust/+15551234567 \
    -H "Content-Type: application/json" \
    -d '{"trust_all_known_keys": true}'
  ```
