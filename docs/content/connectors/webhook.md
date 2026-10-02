<!--
title: Webhook
description: Generic webhook connector for integrating any HTTP service with AI Shuffle
order: 14
-->

# Webhook Connector

## Overview

The Webhook connector is the most flexible connector in AI Shuffle. It lets your agent receive payloads and send notifications via standard HTTP webhooks, making it possible to integrate with virtually any service that can make or receive HTTP requests. Whether you are connecting a custom application, an IoT device, a CI/CD pipeline, a monitoring system, or any other HTTP-capable service, the Webhook connector can handle it.

The connector supports three modes of operation: **inbound** (receive webhooks from external services), **outbound** (deliver event notifications to a configured URL), and **bidirectional** (both). Inbound requests are authenticated with a bearer token or an HMAC signature.

## Prerequisites

- An external service or application that can send and/or receive HTTP requests
- Network access between AI Shuffle and the external service (in the appropriate direction depending on mode)
- For inbound mode: the AI Shuffle host must be reachable from the external service (may require port forwarding or a tunnel for services outside your network)

## Setup Guide

### 1. Choose your webhook mode

Decide which mode fits your integration:

| Mode | Use Case | Description |
|------|----------|-------------|
| `inbound` | Receive events from external services | AI Shuffle exposes an endpoint that external services can POST to. Your agent processes each payload in an AI Shuffle session. |
| `outbound` | Send HTTP requests from automations | An automation's **Send Webhook** action sends a request, to the configured `url` by default. Session event notifications are not sent in this mode (see below). |
| `bidirectional` | Two-way integration | Combines inbound and outbound. External services send payloads in, and event notifications for those sessions are sent back out to the configured `url`. |

### 2. Create the Webhook connector

1. Open the AI Shuffle web UI and go to **Settings > Plugins**, choose the **Apps** filter and open **Webhook**.
2. Click **Install Webhook** if prompted, then click **Connect account**.
3. Fill in the fields for your mode (see the [Configuration Reference](#configuration-reference)). For `inbound` or `bidirectional` mode, choose the **Agent** that handles incoming payloads. To get event notifications, open **Advanced settings** and tick the events under **Session notifications**.
4. Click **Create Connector**.

An inbound connection runs your agent, so it needs an API user with an agent, or every request is refused. See [Common Setup Steps](/docs/connectors#common-setup-steps). To create the connector through the API instead, see [API Management](/docs/connectors#api-management). Example `config` objects for each mode:

**Inbound only:**

```json
{
  "mode": "inbound",
  "inbound_path": "monitoring",
  "inbound_token": "my_secret_bearer_token"
}
```

**Outbound only:**

```json
{
  "mode": "outbound",
  "url": "https://your-service.example.com/api/messages",
  "headers": {
    "Authorization": "Bearer your_service_token",
    "X-Source": "ai-shuffle"
  }
}
```

**Bidirectional** (also set `"events": ["session.complete", "session.error"]` next to `config` in the API call, or tick them under **Session notifications**):

```json
{
  "mode": "bidirectional",
  "inbound_path": "custom",
  "secret": "my_hmac_signing_secret",
  "url": "https://your-service.example.com/api/responses",
  "headers": {
    "Authorization": "Bearer your_service_token"
  }
}
```

### 3. Activate the connector

Turn on the connection's **Activate** switch.

### 4. Test inbound webhooks

Once activated, the inbound webhook endpoint is available at:

```
POST http://localhost:8000/api/v1/connectors/hooks/{inbound_path}
```

The `{inbound_path}` is the bare slug you set in the connector's `inbound_path` config (e.g. `monitoring`). The connector must be running and in `inbound` or `bidirectional` mode for the endpoint to match.

Inbound requests must authenticate: set `inbound_token` or `secret`. A connector with neither rejects every request with 403.

**Test with a bearer token** (the `monitoring` connector above):

```bash
curl -X POST http://localhost:8000/api/v1/connectors/hooks/monitoring \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my_secret_bearer_token" \
  -d '{
    "message": "Server CPU usage is at 95%. Please investigate.",
    "source": "monitoring-system",
    "severity": "high"
  }'
```

The bearer token may alternatively be supplied via the `X-Webhook-Token` header instead of `Authorization: Bearer`.

**Test with an HMAC signature** (the `custom` connector above, which has a `secret` and no `inbound_token`):

```bash
# Generate HMAC signature
PAYLOAD='{"message": "Signed webhook test"}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "my_hmac_signing_secret" | awk '{print $2}')

curl -X POST http://localhost:8000/api/v1/connectors/hooks/custom \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

### 5. Test outbound webhooks

Event notifications are sent only for sessions started through this connector's inbound endpoint, and only for the events the connector subscribes to. So they need `bidirectional` mode with `events` set. For install-wide reactions to session events, use an automation with an event trigger instead.

To test, start a simple HTTP listener:

```bash
# Using Python's built-in HTTP server (for testing only)
python3 -c "
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(length))
        print(json.dumps(body, indent=2))
        self.send_response(200)
        self.end_headers()

HTTPServer(('0.0.0.0', 9999), Handler).serve_forever()
"
```

Point `url` at the listener, then send an inbound request (step 4). When the session it starts completes, the notification payload is printed by the test listener.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mode` | string | No | The webhook mode: `"inbound"`, `"outbound"` (default), or `"bidirectional"` |
| `inbound_path` | string | No | URL slug for the inbound webhook endpoint, a bare slug with no leading slash (e.g., `my-service`). The public endpoint is `/api/v1/connectors/hooks/<slug>`. Required to enable inbound; without it there is no inbound endpoint. Only used in `inbound` and `bidirectional` modes. |
| `inbound_token` | string | No | Bearer token for authenticating inbound webhook requests. When set, incoming requests must include `Authorization: Bearer <token>` (or `X-Webhook-Token: <token>`) header. Inbound needs `inbound_token` or `secret`. |
| `secret` | string | No | HMAC secret used for verifying inbound signatures and signing outbound requests. HMAC is checked only when `inbound_token` is not set; if a token is set, the token is required. |
| `url` | string | No | The outbound webhook endpoint URL where notifications are POSTed. Required for `outbound` and `bidirectional` modes. |
| `headers` | object | No | Additional HTTP headers to include in outbound requests (e.g., authorization headers, custom identifiers). Only used in `outbound` and `bidirectional` modes. |
| `prompt_template` | string | No | Instructions appended after the payload. Defaults to "Process the above webhook payload." |

## Capabilities

The Webhook connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | No | Not applicable |
| `typing_indicator` | No | Not supported |
| `reactions` | No | Not supported |
| `file_upload` | No | Not supported |
| `threads` | No | Not supported |
| `buttons` | No | Not supported |

### Inbound payload format

Any JSON object is accepted. The full payload (truncated to 4,000 characters) is passed to your agent as untrusted data, followed by `prompt_template` (default: "Process the above webhook payload."). For example:

```json
{
  "message": "Server disk usage alert",
  "source": "monitoring",
  "severity": "warning",
  "host": "web-server-01",
  "timestamp": "2026-02-16T10:30:00Z"
}
```

Each inbound request also fires a `webhook_received` trigger that automations can use.

### Outbound payload format

Outbound event notifications are sent as JSON POST requests to the configured `url`:

```json
{
  "event": "session.complete",
  "data": {
    "message": "Event details here"
  },
  "timestamp": "2026-02-16T10:30:05Z"
}
```

The `event` field is the event type, `data` carries the event-specific payload, and `timestamp` is an ISO-8601 UTC time. Outbound requests also include `X-Webhook-Event` and `X-Webhook-Delivery-Id` headers, and an `X-Webhook-Signature` header when a `secret` is configured.

### HMAC Signature Verification

When a `secret` is configured and no `inbound_token` is set, the connector verifies inbound requests using HMAC-SHA256. The external service must compute a signature over the raw request body and include it in the `X-Webhook-Signature` header.

**How to compute the signature (examples in various languages):**

**Bash / curl:**

```bash
PAYLOAD='{"message": "Hello from webhook"}'
SECRET="my_hmac_signing_secret"
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -X POST http://localhost:8000/api/v1/connectors/hooks/my-service \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

**Python:**

```python
import hashlib
import hmac
import requests

payload = b'{"message": "Hello from webhook"}'
secret = b"my_hmac_signing_secret"
signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()

requests.post(
    "http://localhost:8000/api/v1/connectors/hooks/my-service",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={signature}",
    },
)
```

**Node.js:**

```javascript
const crypto = require("crypto");
const https = require("https");

const payload = JSON.stringify({ message: "Hello from webhook" });
const secret = "my_hmac_signing_secret";
const signature = crypto
  .createHmac("sha256", secret)
  .update(payload)
  .digest("hex");

// Include header: "X-Webhook-Signature": `sha256=${signature}`
```

## Troubleshooting

### Inbound webhook returns 404

- **Check the slug.** Ensure you are POSTing to the correct URL. If you set `inbound_path` to `my-service`, the full URL is `http://localhost:8000/api/v1/connectors/hooks/my-service`. The slug in the URL must match `inbound_path` exactly.
- **Connector not running.** The slug only matches when the connector is running and in `inbound` or `bidirectional` mode. Verify the connector status and mode.
- **No `inbound_path` configured.** Inbound requires `inbound_path` to be set; without it there is no inbound endpoint to match.

### Inbound webhook returns 403 Forbidden

- **No credentials configured.** A connector with neither `inbound_token` nor `secret` rejects every inbound request.
- **Missing or wrong token.** If `inbound_token` is configured, every inbound request must include the header `Authorization: Bearer <your_inbound_token>` (or `X-Webhook-Token: <your_inbound_token>`). Check that the token matches exactly.
- **HMAC signature invalid.** If `secret` is configured (and no `inbound_token`) and the signature does not match, the request is rejected. If an `inbound_token` is set, a signature alone is not enough. Verify that you are signing the exact raw request body (no extra whitespace or reformatting) with the correct secret.

### Inbound webhook returns 400 Bad Request

- **Invalid JSON.** Ensure the request body is valid JSON with the `Content-Type: application/json` header.

### Outbound requests are not being sent

- **Check mode and events.** Event notifications are only sent in `bidirectional` mode, for sessions started through this connector's inbound endpoint, and only for the events the connector subscribes to.
- **Check `url`.** Verify the outbound URL is correct and the target service is reachable from the AI Shuffle host.
- **Check target service.** Ensure the target service is running and accepting POST requests. Check its logs for incoming requests.
- **Network issues.** If the target service is on a different network, verify there are no firewall rules blocking the connection.

### Outbound requests fail with connection errors

- **DNS resolution.** If using a hostname, verify it resolves correctly from the AI Shuffle host: `nslookup your-service.example.com`.
- **TLS/SSL.** If the outbound URL uses HTTPS, ensure the target has a valid SSL certificate. Self-signed certificates may cause failures.
- **Timeout.** If the target service takes too long to respond, the request may time out. Check the target service's performance.

### HMAC signature verification fails

- **Encoding.** Ensure the signature is computed over the raw request body bytes, not a re-serialized or pretty-printed version. JSON keys must be in the same order.
- **Secret mismatch.** Verify the `secret` in the connector config matches exactly what the sender is using to compute the signature.
- **Header format.** The `X-Webhook-Signature` header carries the hex digest, in the format `sha256=<hex_digest>`. The `sha256=` prefix is optional.
