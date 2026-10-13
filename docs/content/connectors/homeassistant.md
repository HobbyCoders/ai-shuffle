<!--
title: Home Assistant
description: Control smart home devices through your AI Shuffle agent via Home Assistant
order: 10
-->

# Home Assistant

Connect your [Home Assistant](https://www.home-assistant.io/) and ask your agent to turn on lights, set the thermostat, lock doors, read sensors and run your automations and scripts, in plain words.

## You need

- Home Assistant 2023.1 or later, of any install type.
- AI Shuffle able to reach it, on your home network or through Home Assistant Cloud (Nabu Casa) or your own HTTPS address.

## Set up

### 1. Create an access token

1. In Home Assistant, open your **profile** (bottom left, or `/profile`).
2. Under **Long-Lived Access Tokens**, choose **Create Token** and name it `AI Shuffle`.
3. Copy the token right away. It is shown only once.

### 2. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Home Assistant**. Choose **Install Home Assistant** if asked.
2. Choose **Connect account** and enter the **Home Assistant URL**, such as `http://homeassistant.local:8123` or `https://YOUR_INSTANCE.ui.nabu.casa`, and the token.
3. Optional: set an **Entity Filter** to only follow some devices, such as `light.,switch.,climate.,lock.,sensor.`.
4. Choose **Create Connector**, then turn on **Activate**.

Try "What's the temperature in the living room?" or "Turn on the kitchen lights".

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `url` | Yes | Your Home Assistant address, with `http://` or `https://` and the port |
| `access_token` | Yes | The long-lived access token |
| `entity_filter` | No | Only follow entities whose ID starts with one of these, comma separated |
| `poll_interval_seconds` | No | How often to check device states. Default `60` |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `states`, `control`, `discovery`, `events`. Empty allows all |

## What your agent can do

While Home Assistant is on, every chat can use it. See [How AI actions work](/docs/connectors#how-ai-actions-work).

- Read the state and history of any device: lights, sensors, switches, climate and more.
- Turn things on, off or toggle them, set brightness or temperature, lock and unlock.
- Run Home Assistant services, automations, scripts and scenes.
- Look up services and settings, render templates, fire events and post notifications.

The agent can reach anything the token's user can. Use a Home Assistant user with fewer rights to limit it.

## Troubleshooting

**"401 Unauthorized".** The token was deleted or copied wrong. Paste the whole token, without the word `Bearer`, or create a new one.

**AI Shuffle cannot connect.**

- Include `http://` or `https://` and the port, usually `8123`.
- If `homeassistant.local` does not work, use the IP address, such as `http://192.168.1.100:8123`.
- Check no firewall blocks the port.
- Self-signed HTTPS certificates are refused. Use a trusted certificate, such as Let's Encrypt, or plain HTTP on your home network.

**The agent cannot find a device.** Check its exact ID in Home Assistant under **Developer Tools → States**, such as `light.living_room`. If you set an entity filter, the ID must start with one of its prefixes.

**Commands run but nothing happens.** Check the device is online and works from Home Assistant itself, and look in **Settings → System → Logs** there.

**Answers are slow.** With hundreds of devices, set an entity filter.
