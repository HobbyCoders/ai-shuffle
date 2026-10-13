<!--
title: iMessage
description: Chat with your agent through iMessage on a Mac
order: 8
-->

# iMessage

Chat with your agent through iMessage and SMS, using the Apple ID on your Mac.

**Availability:** this connector only works when AI Shuffle itself runs on the Mac that is signed in to Messages. The macOS desktop app is coming soon. AI Shuffle in Docker cannot use it, even on a Mac. Until then, [BlueBubbles](/docs/connectors/bluebubbles) can send iMessages from AI Shuffle on another machine.

## You need

- A Mac running AI Shuffle, always on and online.
- An Apple ID signed in to iMessage in the Messages app.
- The free **`imsg`** command-line tool installed on the Mac.

## Set up

### 1. Install imsg

Install `imsg` on the Mac and check it works in Terminal with `imsg --help`. If it is not on your `PATH`, note its full path.

Run `imsg rpc` once and press Ctrl-C to stop it. If macOS asks to let it control Messages and read your data, allow it.

### 2. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **iMessage**. Choose **Install iMessage** if asked.
2. Choose **Connect account**.
3. Add at least one **Allowed Handle**: a phone number or email address.
4. Optional: set the **CLI Path** if `imsg` is not on your `PATH`, and the **Service** (`imessage`, `sms` or `auto`).
5. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

### 3. Test it

From another device, send an iMessage to the Apple ID or number on the Mac. The agent's reply comes back in the same conversation.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `cli_path` | No | Where `imsg` is. Default `imsg` |
| `service` | No | Send by `imessage`, `sms` or `auto`. Default `auto` |
| `allowed_handles` | Yes | The phone numbers and emails the agent answers |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can send messages, send tapbacks, show typing, list chats and look up a chat. None of these ask for approval.

Replies are plain text, with no attachments.

## Troubleshooting

**"imsg CLI not found".** Install `imsg`, or set **CLI Path** to its full path.

**Messages do not arrive.**

- Check that iMessage is on in **Messages → Settings → iMessage**.
- Only allowed handles are answered. Check the list.
- Check that `imsg rpc` runs in Terminal.

**Replies are not sent.**

- Check that the connection is activated.
- Allow the macOS permissions `imsg` asks for.
- Set **Service** to `imessage` or `sms` instead of `auto`.

**It stops working when the Mac sleeps.** Set the Mac to never sleep in **System Settings → Energy** (keep a laptop plugged in), or use a keep-awake app such as Amphetamine.
