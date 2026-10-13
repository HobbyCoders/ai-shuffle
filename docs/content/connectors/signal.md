<!--
title: Signal
description: Chat with your agent on Signal
order: 6
-->

# Signal

Chat with your agent on Signal. Messages keep Signal's end-to-end encryption, and no cloud service is involved.

Signal has no official bot service, so the bot uses its own phone number through **signal-cli REST API**, a free program you run next to AI Shuffle.

## You need

- A phone number just for the bot, which can receive a text or call for verification. Do not use your own Signal number: registering it here signs out your phone.
- Docker, to run signal-cli REST API.

## Set up

### 1. Run signal-cli REST API

```bash
docker run -d --name signal-api \
  -p 8080:8080 \
  -v signal-cli-data:/home/.local/share/signal-cli \
  -e MODE=json-rpc \
  bbernhard/signal-cli-rest-api
```

Keep `MODE=json-rpc`. Without it, the bot does not get incoming messages.

Check it is running: `curl http://127.0.0.1:8080/api/v1/about` should return some JSON.

### 2. Register the bot's number

Request a code by text. Signal often asks for a captcha first: follow the [captcha steps](https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha) and put the result in `captcha`.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/register/+14155551234 \
  -H "Content-Type: application/json" \
  -d '{"captcha": "signalcaptcha://..."}'
```

Then confirm with the code you got:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/register/+14155551234/verify/123456 \
  -H "Content-Type: application/json"
```

Use the bot's number in international format, such as `+14155551234`.

### 3. Send a test message

```bash
curl -X POST http://127.0.0.1:8080/api/v2/send \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello from AI Shuffle!", "number": "+14155551234", "recipients": ["+15551234567"]}'
```

Use your own number as the recipient. The message should arrive on your phone.

### 4. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Signal**. Choose **Install Signal** if asked.
2. Choose **Connect account** and enter the bot's **Phone Number**.
3. If signal-cli REST API is not at `http://127.0.0.1:8080`, enter its **API URL**.
4. Add at least one **Allowed Phone Number**.
5. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `account` | Yes | The bot's number, such as `+14155551234` |
| `api_url` | No | The signal-cli REST API address. Default `http://127.0.0.1:8080` |
| `allowed_numbers` | Yes | The numbers the bot answers |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `messages`, `presence`, `reactions`, `groups`. Empty allows all |
| `auto_approve_destructive` | No | `true` skips the approval prompt for leaving a group |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can send messages, mark them read, show typing, react, list groups, look up a group and leave a group.

- Leaving a group asks first: reply `approve` or `deny`.
- Messages are plain text. Signal has no formatting or buttons.
- Messages longer than 4,000 characters are split into several messages.

## Troubleshooting

**AI Shuffle cannot reach signal-cli REST API.** Check it answers at `/api/v1/about`, and that the **API URL** matches its address and port. If it runs in another container, check the Docker network and port mapping.

**The bot gets no messages.**

- Start signal-cli REST API with `MODE=json-rpc`.
- Only allowed numbers are answered. Check the list.

**"Unregistered user".** The other person must use Signal, and numbers must be in international format.

**"Untrusted identity".** The contact reinstalled Signal or changed phones. Trust their new identity:

```bash
curl -X PUT http://127.0.0.1:8080/api/v1/identities/+14155551234/trust/+15551234567 \
  -H "Content-Type: application/json" \
  -d '{"trust_all_known_keys": true}'
```
