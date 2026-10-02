<!--
title: Remote Access
description: Use AI Shuffle from your phone or away from home, and what passes through HobbyCoders' infrastructure
order: 9
-->

# Remote access

AI Shuffle runs at home, on your server. There are three ways to reach it from somewhere else. Pick one; never forward its port from your router to the internet.

| Way | Needs | Traffic goes through |
| --- | --- | --- |
| Your LAN | Nothing extra | Your own network only |
| Tailscale (or another VPN you run) | Tailscale on the server and your devices | Your tailnet, end to end encrypted |
| Remote access through your HobbyCoders account | A free HobbyCoders account | A Cloudflare tunnel under HobbyCoders' Cloudflare account |

## On your LAN

Set the web interface to your server's LAN address: `BIND_HOST` in `.env` for Docker Compose, **WebUI address** in the Unraid template. Any device on your network can then open `http://<server address>:8000`.

Every browser signs in with your owner password once per device (see [First run](/docs/getting-started/first-run)). **Settings → Security → Trusted LAN, no login** skips the password for every device on your network; it is off by default, and turning it on makes anyone on your Wi-Fi or tailnet the owner.

## With Tailscale

Tailscale gives your devices a private network of their own, wherever they are.

1. Install Tailscale on the server (on Unraid, from Community Applications or Unraid's built-in Tailscale support) and on your phone or laptop.
2. Bind AI Shuffle to the server's Tailscale address (`100.x.y.z`) as described above, or to its LAN address if your tailnet routes to your LAN.
3. Open `http://<tailscale address>:8000` from any device on your tailnet.

Nothing passes through HobbyCoders. Traffic between your devices is end to end encrypted by Tailscale.

## Remote access through your HobbyCoders account

This is the easiest way to use AI Shuffle from the Android app, and it works from any network without a VPN.

### Set it up

1. On the worker, open **Settings → Devices** and sign in to (or create) your free HobbyCoders account. Check the worker name.
2. Install the [Android app](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle) and sign in with the same account.
3. Open the worker from your account's worker list. Account-linked workers verify your phone's account and device credential automatically; no separate approval on each worker is required.

Account sign-in authorizes access to your linked workers, including running commands. This trusts HobbyCoders' account service: a compromised account or account service could expose those workers. A matching device name, a trusted-LAN connection, or a different account does not grant this access.

Older controller-linked connections and locally repaired credentials still require owner confirmation as a request under **Settings → Devices**. Compare the device ID with the one on your phone before approving. For a same-account phone that is still waiting after an upgrade, reopen the worker to retry with its saved credential.

To stop access, choose **Deny request** or **Revoke worker access**, then confirm the denial. This immediately blocks that identity on this worker, including existing sessions. A denied identity cannot be approved again; restoring access requires a fresh pairing with a new device identity. Expired keys also require pairing again. Supported Android clients renew still-valid keys automatically; renewal cannot override a denial or revocation.

AI Shuffle sets up the remote connection for that worker. Each phone or browser has its own authorization, visible under **Settings → Devices**.

### What passes through HobbyCoders' infrastructure

When remote access is on, the worker gets an address such as `shuffle-<id>.hobbycoders.com`, served by a **Cloudflare Tunnel** that HobbyCoders creates under its Cloudflare account. The worker keeps an outbound connection to Cloudflare; nothing is opened on your router.

- **Everything between your phone or remote browser and your worker passes through Cloudflare's network:** chats and live streams, files and attachments, the browser panel, voice, and sync between your workers when they are not on the same network.
- Encryption between your device and Cloudflare ends at Cloudflare's edge, as for any site behind Cloudflare, so Cloudflare processes that traffic to deliver it. Sync between your workers is additionally encrypted end to end, so Cloudflare cannot read it.
- HobbyCoders' connection service issues device authorizations and keeps your account, worker and device records. It does not store your chats, and HobbyCoders does not record the content of tunnel traffic. Cloudflare keeps request metadata under its own privacy policy.
- **Your prompts never go to HobbyCoders.** Agents talk to your AI provider directly from the worker, whether or not remote access is on.

Local use, Tailscale, and pairing with a direct HTTPS address you manage yourself do not use the tunnel at all. The [privacy policy](https://hobbycoders.com/privacy#remote-access) has the full details.

### Turn it off

To stop remote access for a worker, disconnect it from your account in **Settings → Devices**, and revoke any phone or browser you no longer use there. Deleting your HobbyCoders account (at hobbycoders.com/delete-account) also removes every tunnel and DNS entry for your workers.

## Private access details

When an agent asks for a folder, an SSH machine or an API key, you enter the details in a private access card. The worker accepts them only over a connection it can vouch for:

- the AI Shuffle desktop app window;
- this worker's remote access address (`https://shuffle-<id>.hobbycoders.com`), from the Android app or a remote browser;
- a browser at `localhost` on the machine running the worker, when you started it without the desktop app and outside Docker.

Anything else gets "Private access is not available on this connection". That includes plain `http://` addresses on your LAN or tailnet, a browser tab on `localhost` while the desktop app runs the worker, and a reverse proxy in front of port 8000, whose forwarded headers are ignored. Open the chat in the desktop app window or at the remote access address instead.

## What not to do

- Do not forward port 8000 from your router, and do not put AI Shuffle's port behind a public reverse proxy. The web interface controls agents that can run commands on your server.
- Do not bind AI Shuffle to `0.0.0.0` or leave Unraid's **WebUI address** as a bare port.
- Do not share your owner password, pairing codes or sign-in codes with anyone.
