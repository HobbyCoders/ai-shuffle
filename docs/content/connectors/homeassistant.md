<!--
title: Home Assistant
description: Control smart home devices through your AI Shuffle agent via Home Assistant
order: 10
-->

# Home Assistant Connector

## Overview

The Home Assistant connector integrates AI Shuffle with your [Home Assistant](https://www.home-assistant.io/) instance, allowing your agent to monitor and control your smart home devices. With this connector, you can ask your agent to turn on lights, adjust thermostats, lock doors, check sensor readings, trigger automations, and interact with any entity managed by Home Assistant.

Home Assistant is a popular open-source home automation platform that supports over 2,000 integrations. By connecting it to AI Shuffle, you gain natural language control over your entire smart home -- no need to remember entity IDs or write YAML automations. Your agent can interpret requests like "set the living room to 72 degrees" or "turn off all the lights downstairs" and translate them into the appropriate Home Assistant service calls.

## Prerequisites

- A running **Home Assistant** instance (Home Assistant OS, Container, Supervised, or Core)
- Home Assistant version **2023.1** or later
- Network access from AI Shuffle to the Home Assistant instance (local network or remote via Nabu Casa / reverse proxy)
- A **Long-Lived Access Token** generated from your Home Assistant profile

## Setup Guide

### 1. Verify your Home Assistant instance is accessible

Confirm you can reach your Home Assistant instance from the machine running AI Shuffle:

```bash
curl -s http://homeassistant.local:8123/api/ \
  -H "Authorization: Bearer YOUR_TOKEN" | head -c 100
```

If using Home Assistant Cloud (Nabu Casa), your URL will look like `https://YOUR_INSTANCE.ui.nabu.casa`.

### 2. Create a Long-Lived Access Token

Long-Lived Access Tokens allow external applications to authenticate with Home Assistant without going through the OAuth flow.

1. Open your Home Assistant web interface
2. Click on your **user profile** (bottom-left corner of the sidebar, or navigate to `/profile`)
3. Scroll down to the **Long-Lived Access Tokens** section
4. Click **Create Token**
5. Enter a descriptive name (e.g., `AI Shuffle Connector`)
6. Click **OK**
7. **Copy the token immediately** -- it will only be shown once. The token is a long string starting with `eyJ...`

If you lose the token, you must delete it and create a new one.

### 3. Test the token

Verify the token works by querying the Home Assistant API:

```bash
# Check API status
curl http://homeassistant.local:8123/api/ \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN"

# List all entities (useful for finding entity_ids)
curl http://homeassistant.local:8123/api/states \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" | python3 -m json.tool | head -50
```

### 4. Create the Home Assistant connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **Home Assistant**.
2. Click **Install Home Assistant** if prompted, then click **Connect account**.
3. Fill in **Home Assistant URL** (e.g., `http://homeassistant.local:8123`) and **Long-Lived Access Token**.
4. Optionally set **Entity Filter** to limit which entities the connector monitors. It is a comma-separated list of entity-id prefixes, such as `light.,switch.,climate.,lock.,sensor.`; only entities whose ID starts with one of the prefixes are tracked. You can also tune how often states are polled with **Poll Interval (seconds)** (defaults to `60`).
5. Click **Create Connector**.

To create the connector through the API instead, see [API Management](/docs/connectors#api-management); the `config` object takes the fields in the table below.

### 5. Activate the connector

Turn on the connection's **Activate** switch.

### 6. Test with a command

In an AI Shuffle chat session, try asking your agent something like:

- "What's the current temperature in the living room?"
- "Turn on the kitchen lights"
- "Set the thermostat to 70 degrees"

Your agent will use the Home Assistant connector to query or control the appropriate entities.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | The base URL of your Home Assistant instance (e.g., `http://homeassistant.local:8123` or `https://your-instance.ui.nabu.casa`) |
| `access_token` | string | Yes | A Long-Lived Access Token generated from your Home Assistant user profile |
| `entity_filter` | string | No | A comma-separated list of entity-id prefixes to restrict which entities are tracked (e.g., `light.,switch.,climate.`). When omitted, all entities are tracked. |
| `poll_interval_seconds` | string | No | Seconds between entity-state polls. Defaults to `"60"`. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `states`, `control`, `discovery`, `events`. |

## Capabilities

The Home Assistant connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | No | Not a chat connector |
| `typing_indicator` | No | Not applicable for smart home control |
| `reactions` | No | Not applicable for smart home control |
| `file_upload` | No | Not supported |
| `threads` | No | Not supported |
| `buttons` | No | Not supported |

### AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work). While a Home Assistant connector is active, every agent session gets its tools.

Your agent can use 13 actions: entity states and history, turn on/off/toggle, set values by domain, any service call, service and config lookup, template rendering, event firing and persistent notifications.

`call_service` reaches any of Home Assistant's domain services. Your agent finds them with `list_services`. The connector's `entity_filter` still applies to state listings, and the access token's own permissions limit what your agent can reach.

What that lets your agent do:

- **Query state** -- Ask about the current state and history of any entity (lights, sensors, switches, climate, etc.)
- **Control devices** -- Turn devices on/off, set brightness, change temperature, lock/unlock
- **Trigger automations** -- Execute Home Assistant automations, scripts, and scenes via `call_service`
- **Monitor sensors** -- Read temperature, humidity, motion, door/window sensors
- **Inspect the instance** -- Render templates, list services, read the HA config

## Troubleshooting

### "401 Unauthorized" error

- **Token expired or invalid.** Long-Lived Access Tokens do not expire, but they can be revoked. Go to your Home Assistant profile and check that the token still exists. If not, create a new one.
- **Wrong token format.** The token should be the full string starting with `eyJ...`. Do not include the `Bearer` prefix in the config -- the connector adds it automatically.

### Cannot connect to Home Assistant

- **Check the URL.** Ensure the `url` in your config includes the protocol (`http://` or `https://`) and the correct port (default is `8123`).
- **DNS resolution.** If using `homeassistant.local`, ensure mDNS is working on your network. Alternatively, use the IP address directly (e.g., `http://192.168.1.100:8123`).
- **Firewall.** Ensure no firewall is blocking traffic on port 8123 between the AI Shuffle host and the Home Assistant host.
- **HTTPS issues.** The connector always verifies TLS certificates, so a self-signed certificate fails. Use a certificate from a trusted CA (e.g., Let's Encrypt), or connect over plain HTTP on your LAN.

### Your agent says it cannot find an entity

- **Check entity ID.** Entity IDs in Home Assistant are case-sensitive and follow the format `domain.name` (e.g., `light.living_room`). Check the exact ID in Home Assistant under **Developer Tools > States**.
- **Entity filtering.** If you specified an `entity_filter` in the connector config, ensure the entity's ID starts with one of the configured prefixes. Entities that do not match the filter are not tracked.
- **Entity unavailable.** The device may be offline. Check the entity status in Home Assistant directly.

### Commands execute but nothing happens

- **Device online?** Verify the physical device is powered on and connected to your network.
- **Check Home Assistant logs.** Go to **Settings > System > Logs** in Home Assistant to see if service calls are failing.
- **Test manually.** Try controlling the device from the Home Assistant UI directly. If it fails there too, the issue is with Home Assistant or the device, not the connector.

### High latency in responses

- **Network.** If AI Shuffle and Home Assistant are on different networks, latency depends on the connection between them. For the fastest response, run them on the same local network.
- **Large entity list.** If you have hundreds of entities and no `entity_filter`, the connector polls and processes a large state list. Adding an `entity_filter` to scope down to relevant entity prefixes will improve response times.
