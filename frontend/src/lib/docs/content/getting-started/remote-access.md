<!--
title: Remote Access
description: Use AI Shuffle from your phone or away from home
order: 9
-->

# Remote access

AI Shuffle runs on your own machine. There are three ways to reach it from somewhere else. Pick one, and never forward its port from your router to the internet.

| Way | Needs | Traffic goes through |
| --- | --- | --- |
| Your home network | Nothing extra | Your own network only |
| Tailscale (or another VPN you run) | Tailscale on the server and your devices | Your own private network, encrypted |
| Remote access through your HobbyCoders account | A free HobbyCoders account | A secure connection run by HobbyCoders on Cloudflare |

## On your home network

Set AI Shuffle to listen on your machine's home network address (`BIND_HOST` with Docker, see [Install with Docker](/docs/getting-started/install-docker-compose)). Any device on your network can then open `http://<machine address>:8000`.

Each browser signs in with your owner password once (see [First run](/docs/getting-started/first-run)). **Settings → Security → Trusted LAN, no login** skips the password for every device on your network. It is off by default. Turning it on makes anyone on your Wi-Fi the owner.

## With Tailscale

1. Install Tailscale on the machine running AI Shuffle and on your phone or laptop.
2. Set AI Shuffle to listen on the machine's Tailscale address (`100.x.y.z`).
3. Open `http://<tailscale address>:8000` from any of your devices.

Nothing passes through HobbyCoders.

## Remote access through your HobbyCoders account

This is the easiest way to use AI Shuffle from the Android app, from any network, without a VPN.

### Set it up

1. On the worker, open **Settings → Devices** and sign in to (or create) your free HobbyCoders account.
2. Install the [Android app](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle) and sign in with the same account.
3. Open the worker from your list of workers.

Your account sign-in gives your phone access to your linked workers, so keep your HobbyCoders account secure. Each phone or browser shows up under **Settings → Devices**, where you can remove it at any time.

### What passes through HobbyCoders

- Your worker gets its own address, such as `shuffle-<id>.hobbycoders.com`. Nothing is opened on your router.
- Traffic between your phone and your worker travels through Cloudflare's network, encrypted in transit. Cloudflare handles it to deliver it, as for any website behind Cloudflare. Sync between your workers is also encrypted end to end.
- HobbyCoders keeps your account, worker and device records. It does not store your chats or record what you send.
- **Your prompts never go to HobbyCoders.** Agents talk to your AI provider directly from the worker.

The [privacy policy](https://hobbycoders.com/privacy#remote-access) has the details.

### Turn it off

Disconnect the worker from your account in **Settings → Devices**, and remove any phone or browser you no longer use. Deleting your HobbyCoders account also removes remote access for all your workers.

## Private access details

When an agent asks for a folder, a machine or an API key, you enter the details in a private access card. For your safety, the card only works on:

- the AI Shuffle desktop app window;
- your worker's remote access address, from the Android app or a remote browser;
- a browser on the same computer, when the worker runs without Docker and without the desktop app.

Anywhere else, the card says "Private access is not available on this connection". Open the chat in the desktop app or at the remote access address instead.

## What not to do

- Do not forward port 8000 from your router, or put AI Shuffle behind a public reverse proxy. It controls agents that can run commands on your machine.
- Do not make AI Shuffle listen on `0.0.0.0`.
- Do not share your owner password, pairing codes or sign-in codes.
