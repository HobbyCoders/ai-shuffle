<!--
title: Choose an Install
description: Which AI Shuffle install fits your computer or server
order: 3
-->

# Choose an install

AI Shuffle runs on Windows, macOS, Linux and Docker, and you can use it from Android and iOS. Here is what is available today.

| Platform | How | Status |
| --- | --- | --- |
| Linux server, NAS or home server | Docker | Available. See [Install with Docker](/docs/getting-started/install-docker-compose). |
| Any PC with Docker (Windows, macOS or Linux) | Docker | Available. See [Install with Docker](/docs/getting-started/install-docker-compose). |
| Windows 10 or 11 | Desktop app | Available. Download it from [hobbycoders.com](https://hobbycoders.com/ai-shuffle/downloads). |
| macOS | Desktop app | Coming soon. Use Docker for now. |
| Linux desktop (64-bit) | Desktop app | Available. Download it from [hobbycoders.com](https://hobbycoders.com/ai-shuffle/downloads). |
| Android | App on Google Play | Connects to your workers. See [Remote access](/docs/getting-started/remote-access). |
| iPhone and iPad | App | Coming soon. Use a browser for now. |

The Docker image runs on 64-bit Intel or AMD machines only. Check the [requirements](/docs/getting-started/requirements) first.

## Docker or the desktop app?

- **Docker** suits a machine that is always on, like a server or NAS. Agents run inside the container, away from the rest of the machine.
- **The desktop app** suits a single PC. Agents run as your own user, with your files and programs. It is the only install that can use **computer use** (agents controlling the screen), because a container has no desktop.

You can use both for free on your LAN or VPN. Same-LAN sync is free. An optional [Cloud account](/docs/getting-started/cloud) adds managed remote access: 1 worker for free or 3 on the paid plan.

## What every install has in common

- **Your data stays on your machine.** Projects, chats, settings and saved sign-ins stay on your own workers. Anything shared between your workers is encrypted.
- **Your credentials are encrypted at rest.** API keys and provider sign-ins are stored encrypted.
- **You choose the version.** Nothing changes until you update. See [Updates](/docs/getting-started/updates).
- **Keep it private.** Open AI Shuffle on the machine itself, your home network or a VPN. Never forward its port from your router. See [Remote access](/docs/getting-started/remote-access).

## After installing

Open AI Shuffle and follow [First run](/docs/getting-started/first-run): set your owner password, connect an AI provider, pick a workspace and run your first agent.

## More than one worker

Install each worker the same way and link them under **Settings → Devices**. No license is required for local use. Each worker keeps its own data. Never point two workers at the same data folder.
