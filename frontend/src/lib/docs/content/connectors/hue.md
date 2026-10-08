<!--
title: Philips Hue
description: Control Philips Hue smart lights through your AI Shuffle agent
order: 11
-->

# Philips Hue

Ask your agent to turn lights on and off, change brightness and color, set scenes and control whole rooms. Commands go straight to your Hue Bridge at home, so they are fast and do not need the Philips cloud.

## You need

- A Philips Hue Bridge on your home network, with lights paired to it.
- AI Shuffle on the same network as the bridge, or able to reach it over a VPN.
- To be near the bridge once, to press its button.

## Set up

### 1. Find the bridge's address

Use any of these:

- In the Hue app, open **Settings → Hue Bridges** and tap the **(i)** next to your bridge.
- Open [discovery.meethue.com](https://discovery.meethue.com/) in a browser on your home network.
- Look for "Philips Hue" in your router's device list.

### 2. Get an API key

1. Press the round button on top of the bridge.
2. Within 30 seconds, run this on a computer on your network, with your bridge's address:

```bash
curl -X POST "http://YOUR_BRIDGE_IP/api" \
  -H "Content-Type: application/json" \
  -d '{"devicetype": "ai_shuffle#connector"}'
```

3. Copy the `username` from the reply. That is your API key. It does not expire.

If you see `link button not pressed`, press the button and run the command again.

### 3. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Philips Hue**. Choose **Install Philips Hue** if asked.
2. Choose **Connect account** and enter the **Bridge IP Address** (just the address, such as `192.168.1.42`) and **API Key (Username)**.
3. Choose **Create Connector**, then turn on **Activate**.

Try "Turn on the living room lights" or "Set the bedroom to 50%".

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `bridge_ip` | Yes | The bridge's address on your network |
| `api_key` | Yes | The key from step 2 |
| `poll_interval_seconds` | No | How often to check the lights. Default `60`. `0` turns it off |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `control`, `discovery`. Empty allows all |

## What your agent can do

While Hue is on, every chat can use it. See [How AI actions work](/docs/connectors#how-ai-actions-work). Nothing asks for approval.

- Turn single lights on and off, and set brightness, color, color temperature and effects such as color loop.
- Control a room or group, or every light at once.
- Turn on scenes you made in the Hue app.
- Say which lights are on and how they are set.

## Troubleshooting

**The bridge cannot be found.** The bridge needs a network cable and three blue lights. AI Shuffle and the bridge must be able to reach each other, so check separate networks or VLANs. If discovery finds nothing, use your router's device list.

**Getting a key fails.** Have the command ready before you press the button, then run it within 30 seconds.

**"Unauthorized user".** The key was removed, for example by a factory reset. Make a new one (step 2) and update the connection.

**Lights respond slowly.** Lights far from the bridge may need another Hue light in between. The bridge also handles only a few commands a second, so ask for rooms or scenes rather than many single lights.

**The bridge's address changed.** Give the bridge a fixed address in your router, or find it again (step 1) and update the connection.
