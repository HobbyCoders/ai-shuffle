<!--
title: Webhook
description: Generic webhook connector for integrating any HTTP service with Claude
order: 14
-->

# Webhook Connector

## Overview

The Webhook connector is the most flexible connector in AI Shuffle. It allows Claude to receive and send messages via standard HTTP webhooks, making it possible to integrate with virtually any service that can make or receive HTTP requests. Whether you are connecting a custom application, an IoT device, a CI/CD pipeline, a monitoring system, or any other HTTP-capable service, the Webhook connector can handle it.

The connector supports three modes of operation: **inbound** (receive webhooks from external services), **outbound** (deliver event notifications to a configured URL), and **bidirectional** (both). It includes optional authentication via bearer tokens and HMAC signature verification for secure integrations.

## Prerequisites

- An external service or application that can send and/or receive HTTP requests
- Network access between AI Shuffle and the external service (in the appropriate direction depending on mode)
- For inbound mode: the AI Shuffle host must be reachable from the external service (may require port forwarding or a tunnel for services outside your network)

## Setup Guide

### 1. Choose your webhook mode

Decide which mode fits your integration:

| Mode | Use Case | Description |
|------|----------|-------------|
| `inbound` | Receive events from external services | AI Shuffle exposes an endpoint that external services can POST to. Claude processes the incoming messages and responds within AI Shuffle. |
| `outbound` | Send event notifications to an external service | Event notifications are automatically POSTed to a configured URL. |
| `bidirectional` | Two-way integration | Combines inbound and outbound. External services send messages in, and event notifications are sent back out to a configured URL. |

### 2. Create the Webhook connector

**Inbound only:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Monitoring Alerts",
    "connector_type": "webhook",
    "config": {
      "mode": "inbound",
      "inbound_path": "monitoring",
      "inbound_token": "my_secret_bearer_token"
    }
  }'
```

**Outbound only:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Notification Sender",
    "connector_type": "webhook",
    "config": {
      "mode": "outbound",
      "url": "https://your-service.example.com/api/messages",
      "headers": {
        "Authorization": "Bearer your_service_token",
        "X-Source": "ai-shuffle"
      }
    }
  }'
```

**Bidirectional:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Custom Integration",
    "connector_type": "webhook",
    "config": {
      "mode": "bidirectional",
      "inbound_path": "custom",
      "inbound_token": "my_inbound_secret",
      "secret": "my_hmac_signing_secret",
      "url": "https://your-service.example.com/api/responses",
      "headers": {
        "Authorization": "Bearer your_service_token"
      }
    }
  }'
```

Or use the AI Shuffle UI: **Settings > Connectors > Add Connector > Webhook**.

### 3. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

### 4. Test inbound webhooks

Once activated, the inbound webhook endpoint is available at:

```
POST http://localhost:8000/api/v1/connectors/hooks/{inbound_path}
```

The `{inbound_path}` is the bare slug you set in the connector's `inbound_path` config (e.g. `monitoring`). The connector must be running and in `inbound` or `bidirectional` mode for the endpoint to match.

**Basic test with curl:**

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

**Test without authentication (if `inbound_token` is not set):**

```bash
curl -X POST http://localhost:8000/api/v1/connectors/hooks/monitoring \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Deployment to production completed successfully."
  }'
```

**Test with HMAC signature:**

```bash
# Generate HMAC signature
PAYLOAD='{"message": "Signed webhook test"}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "my_hmac_signing_secret" | awk '{print $2}')

curl -X POST http://localhost:8000/api/v1/connectors/hooks/monitoring \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

### 5. Test outbound webhooks

For outbound mode, event notifications are automatically sent to the configured `url`. To test this, start a simple HTTP listener:

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

Then trigger an event in AI Shuffle that the connector delivers. The notification payload will be printed by the test listener.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mode` | string | No | The webhook mode: `"inbound"`, `"outbound"` (default), or `"bidirectional"` |
| `inbound_path` | string | No | URL slug for the inbound webhook endpoint, a bare slug with no leading slash (e.g., `my-service`). The public endpoint is `/api/v1/connectors/hooks/<slug>`. Required to enable inbound; without it there is no inbound endpoint. Only used in `inbound` and `bidirectional` modes. |
| `inbound_token` | string | No | Bearer token required for authenticating inbound webhook requests. When set, incoming requests must include `Authorization: Bearer <token>` (or `X-Webhook-Token: <token>`) header. Only used in `inbound` and `bidirectional` modes. |
| `secret` | string | No | HMAC secret used for verifying inbound signatures and signing outbound requests. When set, incoming requests are validated against the `X-Webhook-Signature` header using HMAC-SHA256. |
| `url` | string | No | The outbound webhook endpoint URL where notifications are POSTed. Required for `outbound` and `bidirectional` modes. |
| `headers` | object | No | Additional HTTP headers to include in outbound requests (e.g., authorization headers, custom identifiers). Only used in `outbound` and `bidirectional` modes. |

## Capabilities

The Webhook connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | Yes | Webhook payloads can include formatted text content |
| `typing_indicator` | No | Not supported |
| `reactions` | No | Not supported |
| `file_upload` | No | Not supported |
| `threads` | No | Not supported |
| `buttons` | No | Not supported |

**Message length limit:** 10,000 characters per message. This is higher than most other connectors to accommodate structured data payloads (JSON, logs, etc.) commonly sent via webhooks.

### Inbound payload format

The connector accepts JSON payloads with a flexible schema. At minimum, include a `message` field:

```json
{
  "message": "The text content to send to Claude"
}
```

Additional fields are passed as metadata and may be used by Claude for context:

```json
{
  "message": "Server disk usage alert",
  "source": "monitoring",
  "severity": "warning",
  "host": "web-server-01",
  "timestamp": "2026-02-16T10:30:00Z"
}
```

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

When a `secret` is configured, the connector verifies inbound requests using HMAC-SHA256. The external service must compute a signature over the raw request body and include it in the `X-Webhook-Signature` header.

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

- **Missing or wrong token.** If `inbound_token` is configured, every inbound request must include the header `Authorization: Bearer <your_inbound_token>` (or `X-Webhook-Token: <your_inbound_token>`). Check that the token matches exactly.
- **HMAC signature invalid.** If `secret` is configured and the signature does not match, the request is rejected. Verify that you are signing the exact raw request body (no extra whitespace or reformatting) with the correct secret.

### Inbound webhook returns 400 Bad Request

- **Invalid JSON.** Ensure the request body is valid JSON with the `Content-Type: application/json` header.
- **Missing message field.** The payload must include a `message` field with a non-empty string value.
- **Message too long.** The message must not exceed 10,000 characters.

### Outbound requests are not being sent

- **Check mode.** The connector must be in `outbound` or `bidirectional` mode.
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
- **Header format.** The `X-Webhook-Signature` header must be in the format `sha256=<hex_digest>`. The `sha256=` prefix is required.
