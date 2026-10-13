<!--
title: Connectors
description: Connector CRUD, lifecycle management, Gmail and Plaid sign-in, and webhook endpoints
order: 7
-->

# Connectors API

Endpoints for managing platform connectors — creating, configuring, testing, and controlling connections to external messaging platforms and services.

Every endpoint on this page except the inbound webhooks and the Google sign-in callback needs owner access. API-user keys (`aih_...`) get `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

## Discovery

### GET /api/v1/connectors/types

List all available connector types with their configuration fields.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/types \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "type": "telegram",
    "name": "Telegram",
    "description": "Telegram bot via Bot API",
    "config_fields": [
      { "key": "bot_token", "label": "Bot Token", "type": "password", "required": true },
      { "key": "allowed_chat_ids", "label": "Allowed Chat IDs", "type": "list", "required": false }
    ]
  }
]
```

### GET /api/v1/connectors/events

List valid connector event types (for a connector's `events`).

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/events \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Discord Guided Setup

### POST /api/v1/connectors/discord/discover

Log into Discord with a bot token and return the bot's identity plus the guilds (servers) and text channels it can see. Powers the guided Discord setup UI so the owner can pick guilds/channels from a list instead of copying IDs. This performs an HTTP-only login (no Gateway connection) and does not modify anything.

**Auth:** Owner only

Provide **exactly one** of the following in the request body:

- `bot_token` -- a plaintext bot token to validate. A masked value (starting with `****`) is rejected.
- `connector_id` -- the ID of an existing Discord connector; its stored token is decrypted server-side and used. Useful when editing (the token stays masked in the UI).

```bash
curl -X POST http://localhost:8000/api/v1/connectors/discord/discover \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot_token": "YOUR_BOT_TOKEN"}'
```

**Response:**
```json
{
  "bot": { "id": "123456789012345678", "name": "AI Shuffle Bot" },
  "application_id": "123456789012345678",
  "guilds": [
    {
      "id": "223456789012345678",
      "name": "My Server",
      "channels": [
        { "id": "323456789012345678", "name": "general" }
      ],
      "error": null
    }
  ]
}
```

All IDs are returned as strings (Discord snowflakes exceed JavaScript's safe integer range). Only text channels are listed. If the bot cannot read a guild's channels, that guild's `error` field describes the problem and its `channels` array is empty; the guild is still returned so it can be selected.

**Errors:**

| Status | Condition |
|--------|-----------|
| 400 | Neither or both of `bot_token`/`connector_id` provided, or a masked token was sent |
| 404 | `connector_id` not found or is not a Discord connector |
| 400 | Discord rejected the token (invalid bot token) |
| 502 | Discord discovery failed (timeout or upstream error) |

The token is never logged or echoed in the response.

## Connector CRUD

### GET /api/v1/connectors

List all connectors.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "id": "conn-1a2b3c4d",
    "name": "My Telegram Bot",
    "type": "telegram",
    "is_active": true,
    "status": "connected",
    "status_message": null,
    "config": {
      "bot_token": "****",
      "allowed_chat_ids": ["12345678"]
    },
    "events": [],
    "api_user_id": "a1b2c3d4",
    "plugin_id": "telegram@shuffle-connectors",
    "failure_count": 0,
    "runner_id": "ai-shuffle-1a2b3c4d",
    "created_at": "2025-01-15T10:00:00Z",
    "updated_at": "2025-01-15T10:00:00Z"
  }
]
```

Note: Sensitive config fields (tokens, secrets, passwords) are stored encrypted and returned as `****`; no characters of the secret are shown.

`runner_id` is the install that runs the connector's live client. When another install runs it, the responses of `PATCH` (with `is_active`), `activate`, `deactivate` and `restart` add `applies_on` with that install's id: the change is saved here, and that install applies it when it next restarts.

### POST /api/v1/connectors

Create a new connector. The app's runtime must be installed and enabled first (**Settings → Plugins → Explore plugins**, filter **Apps**). Unless you pass `api_user_id`, an API user is created for the connector; `profile_ids` sets which agents it may run. Incoming messages are refused while that API user has no agent.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Telegram Bot",
    "type": "telegram",
    "config": {
      "bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
      "allowed_chat_ids": ["12345678"]
    },
    "profile_ids": ["your-agent-id"],
    "project_id": "my-project"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name |
| `type` | string | Yes | Connector type from `GET /api/v1/connectors/types` |
| `config` | object | No | Values for the type's `config_fields` |
| `events` | string[] | No | Event types to deliver (from `GET /api/v1/connectors/events`) |
| `profile_ids` | string[] | No | Agents the auto-created API user may run |
| `project_id` | string | No | Project for the auto-created API user |
| `api_user_id` | string | No | Link an existing API user instead of creating one |

### GET /api/v1/connectors/{connector_id}

Get connector details.

**Auth:** Owner only

### PATCH /api/v1/connectors/{connector_id}

Update a connector. Every field is optional: `name`, `config`, `events`, `is_active`. `config` is merged into the stored config: keys you leave out, such as a saved token, keep their stored values, and a masked value (starting with `****`) keeps the stored secret. If the config changes while the connector is active, it restarts with the merged config. Setting `is_active` also starts or stops the running connector, like `activate` and `deactivate`.

Returns the updated connector. `400` for an unknown event type, or for `"is_active": false` while an enabled automation uses the connector; `404` for an unknown connector.

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/connectors/conn-1a2b3c4d \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Renamed Bot",
    "config": {
      "allowed_chat_ids": ["12345678"]
    }
  }'
```

### DELETE /api/v1/connectors/{connector_id}

Delete a connector. An API user that was created for it is deleted too. Returns `204`, `404` for an unknown connector, or `409` while an automation uses it (see [dependencies](#get-apiv1connectorsconnector_iddependencies)).

**Auth:** Owner only

## Lifecycle Management

### POST /api/v1/connectors/{connector_id}/test

Test the connector's connection without activating it.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/test \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "message": "Connection successful"
}
```

### POST /api/v1/connectors/{connector_id}/activate

Enable and start the connector. Chat connectors need at least one allowed sender first; otherwise this returns `400` "Add at least one allowed sender before enabling this connector". Any other start failure returns `500` with the connector's last error.

Activating a connector that is already connected, or still connecting, leaves that connection alone, so repeating the call never starts a second copy. A connection that failed, or has been connecting for more than 2 minutes, is replaced.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/activate \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/connectors/{connector_id}/deactivate

Stop and disable the connector. `409` while an enabled automation uses it.

**Auth:** Owner only

### POST /api/v1/connectors/{connector_id}/restart

Restart the connector (deactivate then activate).

**Auth:** Owner only

## Status & Inspection

### GET /api/v1/connectors/{connector_id}/status

Get the runtime status of a connector, including whether its instance is currently running.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/status \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "id": "conn-1a2b3c4d",
  "status": "connected",
  "status_message": null,
  "last_connected_at": "2025-01-15T10:00:00Z",
  "last_event_at": "2025-01-15T10:05:00Z",
  "failure_count": 0,
  "is_running": true
}
```

### GET /api/v1/connectors/{connector_id}/logs

Get the recent event logs for a connector.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/logs \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "id": "evt_abc123",
    "connector_id": "conn-1a2b3c4d",
    "event_type": "message_received",
    "direction": "inbound",
    "payload": {},
    "status": "ok",
    "error": null,
    "created_at": "2025-01-15T10:05:00Z"
  }
]
```

### GET /api/v1/connectors/{connector_id}/dependencies

List the automations that use this connector, so you can update them before deleting or changing it.

**Auth:** Owner only

**Response:**
```json
[
  { "id": "auto_abc123", "name": "Morning inbox summary", "is_enabled": true, "uses": ["action", "trigger"] }
]
```

`uses` names where the connector appears: `trigger`, `action` (a Connector Action step) or `output`. An automation triggered by events from any connector is listed for every connector.

### GET /api/v1/connectors/{connector_id}/senders

List the newest people who started a chat with this connector, so you can find your own account id on that platform. Returns up to five entries, newest first, or `404` for an unknown connector.

**Auth:** Owner only

**Response:**
```json
[
  { "platform_user_id": "123456789", "platform_user_name": "Sam", "last_seen": "2026-10-09T12:00:00+00:00" }
]
```

A connector runs as its API user, so its messages can't use your personal subscriptions (the Claude CLI sign-in, ChatGPT/Codex or SuperGrok). The one exception is your own account: save your platform user ids in the connector's `owner_sender_ids` config (`PATCH` with `{"config": {"owner_sender_ids": ["123456789"]}}`), and messages from those ids can use them. Only the platform's user id is matched, never a display name or username. Telegram, Discord, Slack, WhatsApp, Signal and iMessage support it. Other connectors return `400` for it, and their messages never use a subscription.

### GET /api/v1/connectors/{connector_id}/triggers

List the triggers exposed by the connector's type.

**Auth:** Owner only

### GET /api/v1/connectors/{connector_id}/actions

List the actions exposed by the connector's type.

**Auth:** Owner only

### GET /api/v1/connectors/{connector_id}/tools

List the tools exposed by the connector's type.

**Auth:** Owner only

### POST /api/v1/connectors/{connector_id}/actions/{action_id}/execute

Execute a connector action directly against a running connector instance. The JSON request body is passed through as the action parameters. Returns `409` if the connector is not running and `400` for an unknown action.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/actions/send_message/execute \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "chat_id": 12345678, "text": "Hello" }'
```

**Response:**
```json
{
  "status": "ok",
  "action_id": "send_message",
  "result": {}
}
```

## Gmail Sign-in (Google OAuth)

Gmail connections are created by signing in with Google instead of pasting credentials. You register your own Google OAuth app once, then each sign-in creates (or reconnects) a Gmail connector. See the [Gmail connector guide](/docs/connectors/gmail) for the Google Cloud setup.

The Gmail app must be installed and enabled first (**Settings → Plugins → Explore plugins**, filter **Apps**).

### GET /api/v1/connectors/oauth/google/status

**Auth:** Owner only

**Response:**
```json
{
  "configured": true,
  "client_id": "1234-abc.apps.googleusercontent.com",
  "redirect_uri": "https://shuffle.example.com/api/v1/connectors/oauth/google/callback",
  "suggested_redirect_uri": "https://shuffle.example.com/api/v1/connectors/oauth/google/callback",
  "environment_managed": false
}
```

`environment_managed` is `true` when the app settings come from the `SHUFFLE_GOOGLE_CLIENT_ID`, `SHUFFLE_GOOGLE_CLIENT_SECRET` and `SHUFFLE_GOOGLE_REDIRECT_URI` environment variables; they then cannot be changed through the API.

### PUT /api/v1/connectors/oauth/google/app

Save the Google OAuth app. The secret is stored encrypted.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/connectors/oauth/google/app \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "1234-abc.apps.googleusercontent.com",
    "client_secret": "GOCSPX-...",
    "redirect_uri": "http://localhost:8000/api/v1/connectors/oauth/google/callback"
  }'
```

`redirect_uri` must be the address you reach AI Shuffle on followed by `/api/v1/connectors/oauth/google/callback`, over HTTPS (plain HTTP only for `localhost`). Returns `{"configured": true}`, `400` for a redirect URI that does not match, or `409` while credential storage is locked.

### POST /api/v1/connectors/oauth/google/start

Start a sign-in. Returns `{"authorization_url": "...", "flow_id": "..."}` and sets a short-lived cookie that ties the sign-in to this browser. Open `authorization_url` in the same browser within 10 minutes.

**Auth:** Owner only

```json
{
  "name": "Work mail",
  "connector_id": null,
  "options": { "labels": "INBOX", "watch_inbox": false, "poll_interval_seconds": 60, "max_results": 20 }
}
```

Every field is optional. `name` defaults to the Google account's address. Set `connector_id` to reconnect an existing Gmail connector; the same Google account must be chosen. `options` also accepts `enabled_action_categories`.

### GET /api/v1/connectors/oauth/google/callback

Google redirects here after sign-in. It creates or updates the connector and shows a page that reports the result to the window that opened it (`{"type": "connector-oauth", "flow_id", "connector_id", "error"}`) and closes itself. Not called directly.

**Auth:** None (public — checked against the pending sign-in and its browser cookie)

## Personal Finance (Plaid)

The Plaid connector gives agents read-only access to bank and credit-card accounts through Plaid Link. Agents cannot make payments or transfers, and bank logins never reach AI Shuffle. You need your own Plaid app with the Transactions product; `sandbox` uses Plaid's test institutions, `production` uses real accounts and may incur Plaid fees.

The Plaid app must be installed and enabled first (**Settings → Plugins → Explore plugins**, filter **Apps**). Connecting a bank needs a browser, because Plaid Link runs in the page at `/plaid-link`.

### GET /api/v1/connectors/oauth/plaid/status

**Auth:** Owner only

Returns `configured`, and once configured `client_id`, `environment`, `country_codes` and `redirect_uri` (never the secret), plus `suggested_redirect_uri`.

### PUT /api/v1/connectors/oauth/plaid/app

Save the Plaid app. The secret is stored encrypted.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/connectors/oauth/plaid/app \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "your-plaid-client-id",
    "client_secret": "your-plaid-secret",
    "environment": "sandbox",
    "country_codes": ["US"],
    "redirect_uri": "https://shuffle.example.com/plaid-link"
  }'
```

`environment` is `sandbox` (default) or `production`. `country_codes` defaults to `["US"]`. `redirect_uri` must be the HTTPS address you reach AI Shuffle on followed by `/plaid-link`; add the same URI to the allowed redirect URIs in the Plaid dashboard. Returns `{"configured": true}`, `400` for a redirect URI that does not match, or `409` while credential storage is locked.

### POST /api/v1/connectors/oauth/plaid/start

Start a bank connection. No body. Returns `{"flow_id": "...", "link_token": "..."}` and sets a cookie that ties the flow to this browser for 30 minutes. Open Plaid Link with `link_token`.

`400` when Plaid is not set up, the redirect URI does not match, the app is not installed or Plaid refuses the request; `429` while 128 bank connections are already pending; `502` when Plaid answers without a link token (check the Plaid app settings and try again).

**Auth:** Owner only

### POST /api/v1/connectors/oauth/plaid/exchange

Finish the connection with the `public_token` that Plaid Link returns on success. Must come from the same browser as `start`.

**Auth:** Owner only

```json
{ "flow_id": "...", "public_token": "public-sandbox-..." }
```

Returns `{"connector_id": "..."}`. The new connector is named after the first account, for example `Checking (Plaid sandbox)`.

### Plaid actions

Run these through `POST /api/v1/connectors/{connector_id}/actions/{action_id}/execute`, or let an agent use them while the connector is active. Every result also includes `environment`.

| Action | Parameters | Returns |
|--------|------------|---------|
| `accounts` | — | `accounts` (name, type, masked number, cached balances) |
| `balances` | — | `accounts` with balances fetched now from the bank (Plaid may charge for this) |
| `transactions` | `start_date`, `end_date` (required, `YYYY-MM-DD`), `count` (1–100, default 50), `offset` | `accounts`, `transactions`, `total_transactions`, `offset` |

In `transactions`, positive amounts are money spent and negative amounts are refunds or credits. Pending entries can still change.

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn-1a2b3c4d/actions/transactions/execute \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"start_date": "2025-01-01", "end_date": "2025-01-31", "count": 50}'
```

## Inbound Webhooks

### GET /api/v1/connectors/whatsapp/webhook

WhatsApp (Meta) webhook verification endpoint. Meta calls this with the `hub.mode`, `hub.verify_token`, and `hub.challenge` query parameters; the challenge is echoed back when the verify token matches a configured WhatsApp connector.

**Auth:** None (public — verified via the connector's verify token)

### POST /api/v1/connectors/whatsapp/webhook

Receive incoming WhatsApp messages. Requests are matched to a running WhatsApp connector by `phone_number_id`. Every request must carry a valid Meta `X-Hub-Signature-256` header made with the connector's app secret; a connector without an app secret accepts nothing.

**Auth:** None (public — verified via Meta signature)

### POST /api/v1/connectors/hooks/{webhook_slug}

Generic inbound webhook endpoint. External services POST JSON here to trigger AI processing; the `webhook_slug` must match the `inbound_path` of a running webhook connector in `inbound` or `bidirectional` mode. Any slug works, including ones such as `test` or `activate`. Authenticated via a Bearer token (`Authorization` header or `X-Webhook-Token`) or an HMAC signature (`X-Webhook-Signature` header).

Returns `{"status": "ok", "message": "Webhook received and processing"}`. `400` for a body that is not JSON, `403` for a missing or wrong token or signature (or a connector with neither configured), `404` when no running connector matches the slug, `413` above the server's request size limit (50 MB by default), `500` when the connector fails to process it.

**Auth:** None (public — verified via the connector's token or HMAC signature)

```bash
curl -X POST http://localhost:8000/api/v1/connectors/hooks/my-hook \
  -H "Authorization: Bearer my_webhook_token" \
  -H "Content-Type: application/json" \
  -d '{ "message": "Hello from my service" }'
```

## Connector Statuses

| Status | Description |
|--------|-------------|
| `connected` | Active and receiving messages |
| `disconnected` | Stopped, not receiving |
| `connecting` | In the process of connecting |
| `error` | Connection failed |
