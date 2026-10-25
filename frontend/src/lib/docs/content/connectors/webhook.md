<!--
title: Webhook
description: Send and receive web requests to connect any service to AI Shuffle
order: 14
-->

# Webhook

Connect anything that can send or receive web requests: your own app, a monitoring system, a build pipeline or a smart device.

- **Inbound:** other services send AI Shuffle a request, and your agent handles it.
- **Outbound:** an automation's **Send Webhook** step sends a request to your service.
- **Bidirectional:** both, and AI Shuffle can also tell your service when the agent finishes.

## Set up

1. Open **Settings → Plugins**, choose **Apps** and open **Webhook**. Choose **Install Webhook** if asked.
2. Choose **Connect account** and pick a **mode**.
3. Fill in the settings below. For inbound, pick the **Agent** that handles requests.
4. For bidirectional, open **Advanced settings** and tick the events to report under **Session notifications**.
5. Choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

Inbound needs AI Shuffle reachable from the service that sends. For services outside your home network, see [Remote access](/docs/getting-started/remote-access).

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `mode` | No | `inbound`, `outbound` (default) or `bidirectional` |
| `inbound_path` | For inbound | A short name for your address, such as `monitoring` |
| `inbound_token` | For inbound, this or `secret` | A password senders include |
| `secret` | For inbound, this or `inbound_token` | A key senders sign requests with. Outbound requests are signed with it too |
| `signature_header` | No | Incoming signature header; default `X-Webhook-Signature` |
| `signature_encoding` | No | Incoming HMAC-SHA256 digest encoding: `hex` (default) or `base64` |
| `signature_prefix` | No | Prefix required before the digest, such as `sha256=`. If unset, hex accepts both bare hex and `sha256=<hex>` for compatibility |
| `url` | For outbound | Where to send requests |
| `headers` | No | Extra headers for outbound requests, such as your service's API key |
| `prompt_template` | No | Instructions for the agent after the request. Default "Process the above webhook payload." |

## Send a request to AI Shuffle

Send JSON to `/api/v1/connectors/hooks/<inbound_path>` on your AI Shuffle address, with your token:

```bash
curl -X POST http://localhost:8000/api/v1/connectors/hooks/monitoring \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my_secret_bearer_token" \
  -d '{"message": "Server CPU usage is at 95%. Please investigate.", "severity": "high"}'
```

You can send the token in an `X-Webhook-Token` header instead.

Any JSON works. Your agent gets the request (up to 4,000 characters) followed by your `prompt_template`. Each request can also start an automation with the `webhook_received` trigger.

### Signing requests instead

If you set a `secret` and no `inbound_token`, sign each request instead: send `X-Webhook-Signature: sha256=<hex>`, an HMAC-SHA256 of the exact body made with your secret.

```bash
PAYLOAD='{"message": "Signed webhook test"}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "my_hmac_signing_secret" | awk '{print $2}')

curl -X POST http://localhost:8000/api/v1/connectors/hooks/custom \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

The signature settings affect inbound verification only; outbound signing is unchanged. Verification uses the exact request bytes, not re-serialized JSON. A wrong signature, encoding or required prefix is rejected.

### GitHub example

Create an inbound connector with `inbound_path` set to `github`, your GitHub webhook secret in `secret`, and no `inbound_token`. Set:

- **Signature Header:** `X-Hub-Signature-256`
- **Signature Encoding:** `hex`
- **Signature Prefix:** `sha256=`

In your GitHub repository's **Settings → Webhooks**, add your reachable AI Shuffle URL ending in `/api/v1/connectors/hooks/github`. Choose `application/json`, enter the same secret, and select the events to deliver. A GitHub push delivery then passes its `X-Hub-Signature-256: sha256=<hex>` signature to this connector.

### Automation trigger data

The `webhook_received` trigger contains `method` from the HTTP request, `body` with the complete JSON payload, and `path` with the connector's inbound slug. Its `headers` uses lowercase names and includes only `content-type`, `user-agent`, `x-request-id`, `x-github-event`, `x-github-delivery`, `x-gitlab-event`, `x-webhook-event` and `x-webhook-delivery-id`, when present. Authorization, cookies, token headers and the configured signature header are never included. A `headers` or `method` key inside the JSON stays inside `body`; it cannot replace transport metadata.

If an automation is already running on this worker, another webhook, manual or connector trigger records a `skipped_running` entry in run history with its trigger data. It does not queue another run. `skipped_running` and `budget_exceeded` entries do not consume the daily run allowance, count as executed runs in statistics, or displace executed runs from output deduplication checks. A successful webhook response acknowledges receipt, not completion of an automation.

## Requests AI Shuffle sends

In bidirectional mode, AI Shuffle tells your `url` when a chat started by an inbound request reaches an event you ticked, such as finishing or failing:

```json
{
  "event": "session.complete",
  "data": {"message": "Event details here"},
  "timestamp": "2026-02-16T10:30:05Z"
}
```

They include `X-Webhook-Event` and `X-Webhook-Delivery-Id` headers. When you set a `secret`, they also carry `X-Webhook-Signature` in the same form, so your service can check they came from you.

To react to every chat, not just webhook ones, use an [automation](/docs/features/automations) with an event trigger.

## Troubleshooting

**404 Not Found.** The name in the address must match `inbound_path` exactly, and the connection must be on and in inbound or bidirectional mode.

**403 Forbidden.**

- Set an `inbound_token` or a `secret`. Without either, every request is refused.
- Check the token matches exactly.
- With a token set, a signature alone is not enough.
- Sign the exact body you send, with no reformatting.

**400 Bad Request.** Send valid JSON with `Content-Type: application/json`.

**Nothing arrives at your service.**

- Notifications are only sent in bidirectional mode, for chats started by an inbound request, and for events you ticked.
- Check `url` and that your service is up and reachable from AI Shuffle.
- Your service needs a valid HTTPS certificate.
