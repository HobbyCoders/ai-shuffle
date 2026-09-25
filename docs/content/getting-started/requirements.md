<!--
title: Requirements
description: Hardware, software, network access and accounts AI Shuffle needs
order: 2
-->

# Requirements

## The machine that runs AI Shuffle

AI Shuffle runs as one Docker container. The machine that runs it is called a **worker**.

| | Minimum | Recommended |
| --- | --- | --- |
| Processor | 64-bit Intel or AMD (amd64) | 4 cores or more |
| Memory | 4 GB free for light use | 8 GB or more if you run several agents at once, plus swap |
| Disk | 20 GB free for the image, database and a few projects | More for large repositories and long session histories |
| Docker | Docker Engine 24 with Docker Compose 2.24 | The current Docker release |

- **ARM is not supported yet.** Raspberry Pi, ARM NAS boxes and ARM servers cannot run the image. On an Apple silicon Mac, Docker runs it in emulation, which is slow.
- **Swap matters.** Each agent is its own process with its own memory. Without swap on the host, a burst of agents can run the whole machine out of memory.
- **Unraid** works with its built-in Docker support; see [Install on Unraid](/docs/getting-started/install-unraid).

The container carries everything else it needs: its own private PostgreSQL database, the built-in browser (Chromium), Node.js and the GitHub CLI. On first start it installs the Claude Code CLI with Anthropic's own installer, and the CLI keeps itself up to date after that.

## Network access

The worker needs outbound HTTPS to:

- `registry.hobbycoders.com`, to download the AI Shuffle image with your pull token (image layers then come from GitHub's container storage);
- `claude.ai` and Anthropic's download servers, to install and update the Claude Code CLI;
- your AI provider, for example Anthropic's API, and any other provider you set up;
- `hobbycoders.com`, for update checks, license checks and (only if you turn it on) remote access;
- the Python package index (`pypi.org`) and the npm registry (`registry.npmjs.org`), only while an in-app update installs.

Nothing needs to reach the worker from the internet. Never forward its port from your router. See [Remote access](/docs/getting-started/remote-access) for safe ways to use it away from home.

## Accounts you need

- **An AI Shuffle license**, or a trial. Your license email contains your **license key** (you enter it in the app) and your **pull token** (Docker uses it to download the image). See [License](/docs/getting-started/license).
- **Your own AI account.** For Claude Code this is either a Claude subscription that you sign in to yourself, or an Anthropic API key. AI Shuffle includes no AI usage. See [Modes and costs](/docs/getting-started/modes-and-costs).
- **Optional:** accounts with other AI providers (OpenAI, xAI, OpenRouter, fal.ai), a GitHub account for repository features, and a free HobbyCoders account for the Android app and remote access.

## Devices you use it from

- Any current desktop browser: Chrome, Edge, Firefox or Safari.
- The [Android app](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle), Android 8.0 or later. It connects to your workers; it does not run agents itself.
- A desktop app for Windows is available in beta. It runs a worker on the PC itself, without Docker.

## Where you can buy

At launch, licenses cannot be sold to buyers in the European Union or the United Kingdom. Prices are in US dollars.
