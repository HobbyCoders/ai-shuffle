<!--
title: Requirements
description: Hardware, accounts and network access AI Shuffle needs
order: 2
-->

# Requirements

The machine that runs AI Shuffle is called a **worker**. You can run it with Docker on a server, NAS or PC, or with the desktop app.

## Docker

| | Minimum | Recommended |
| --- | --- | --- |
| Processor | 64-bit Intel or AMD (amd64) | 4 cores or more |
| Memory | 4 GB free for light use | 8 GB or more if you run several agents at once, plus swap |
| Disk | 20 GB free | More for large projects and long chat histories |
| Docker | Docker Engine 24 with Docker Compose 2.24 | The current Docker release |

- **ARM is not supported yet.** Raspberry Pi, ARM NAS boxes and ARM servers cannot run the image. On an Apple silicon Mac, Docker runs it in emulation, which is slow.
- **Add swap.** Each agent uses its own memory. Without swap, a burst of agents can run the machine out of memory.

Everything else is inside the image, including its database and a built-in browser. You never set up a database.

## Desktop app

The desktop app runs a worker on your computer without Docker.

- **Windows 10 or 11 (64-bit):** available as a beta.
- **macOS and Linux:** coming soon.

Its built-in browser is your installed Google Chrome, or Microsoft Edge when Chrome is not installed.

## Devices you use it from

- Any current desktop browser: Chrome, Edge, Firefox or Safari.
- The [Android app](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle), Android 8.0 or later. It connects to your workers and does not run agents itself.
- An iPhone and iPad app is coming soon. Use a browser for now.

## Accounts you need

- **An AI Shuffle license**, or a trial. Your license email contains your **license key** (you enter it in the app) and your **pull token** (Docker uses it to download the image). See [License](/docs/getting-started/license).
- **Your own AI account** with at least one supported provider: Claude, OpenAI, OpenRouter, xAI Grok or a custom gateway. AI Shuffle includes no AI usage. See [Modes and costs](/docs/getting-started/modes-and-costs).
- **Optional:** more AI providers, fal.ai for images, video and audio, a GitHub account for repository features, and a free HobbyCoders account for the Android app and remote access.

## Network access

The worker needs outbound internet access to:

- `registry.hobbycoders.com`, to download the AI Shuffle image;
- `hobbycoders.com`, for update checks, license checks and (only if you turn it on) remote access;
- the AI providers you connect;
- Anthropic's download servers, if you install the Claude Code CLI;
- the public Python and npm package registries, while an in-app update or a connector installs.

Nothing needs to reach the worker from the internet. Never forward its port from your router. See [Remote access](/docs/getting-started/remote-access) for safe ways to use it away from home.

## Where you can buy

At launch, licenses cannot be sold to buyers in the European Union or the United Kingdom. Prices are in US dollars.
