# AI Shuffle

A self-hosted workspace for AI agents, by HobbyCoders.

[Website](https://hobbycoders.com/ai-shuffle) · [Documentation](docs/content/getting-started/_index.md) · [FAQ](https://hobbycoders.com/ai-shuffle/faq) · [Android app](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle)

AI Shuffle runs on your own server, NAS or PC and brings your agents, projects, conversations and tools into one place. Build software, research a problem, automate recurring work, browse the web or generate media, from a browser, the desktop app or the Android app.

It runs AI agents with **your own** AI account. You connect any supported provider (Claude, OpenAI, OpenRouter, xAI Grok or a custom gateway) by subscription sign-in or API key, and that provider bills you directly. One provider is enough to start. An existing Claude subscription works through the standard Claude Code CLI, which you sign in to yourself. AI Shuffle includes no AI usage and never pays for, resells or sees it. Your projects, sessions and database stay on your machine; prompts go from there straight to the provider you chose.

## What you get

- **A full agent workspace:** streaming sessions with tool activity, file attachments, diffs, branches and worktrees, pull requests and CI, profiles, subagents, MCP servers and plugins.
- **A built-in browser** your agents can drive, with its own persistent profile.
- **Automations and hooks:** runs on a schedule, an interval, a webhook or a connector event.
- **Connectors:** Telegram, Discord, Slack, WhatsApp, Gmail, Home Assistant, Spotify and more.
- **Voice**, with your own OpenAI account.
- **Several workers, one account:** up to 3 machines per license, synced end to end encrypted, and the Android app for your phones.

## Install

If you choose Claude, install Claude Code from setup or **Settings > Providers > Claude**. Anthropic's own installer downloads it; no Claude Code binary is bundled. It is reinstalled at worker start only while **Keep Claude Code installed** is on.

AI Shuffle is sold as a license with a 14-day trial. Your license email has your license key and your pull token.

- [Requirements](docs/content/getting-started/requirements.md): a 64-bit Intel or AMD machine with Docker, 8 GB of memory recommended.
- [Install on Unraid](docs/content/getting-started/install-unraid.md)
- [Install with Docker Compose](docs/content/getting-started/install-docker-compose.md)
- [First run](docs/content/getting-started/first-run.md), then [Modes and costs](docs/content/getting-started/modes-and-costs.md)

The same guides are inside the app under **Docs**, together with [troubleshooting](docs/content/getting-started/troubleshooting.md), [updates](docs/content/getting-started/updates.md), [license](docs/content/getting-started/license.md) and the [changelog](docs/content/getting-started/changelog.md).

The Android app is on [Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle). It connects to your workers and runs no agents itself; it needs Android 8.0 or later.

## Support

Email support@hobbycoders.com. Never send passwords, API keys, license keys, pull tokens or sign-in codes. See the [privacy policy](https://hobbycoders.com/privacy), [terms](https://hobbycoders.com/terms), [license terms](https://hobbycoders.com/license) and [refund policy](https://hobbycoders.com/refund).

AI Shuffle is an independent product. It is not affiliated with Anthropic, OpenAI, xAI or any other AI provider, and none of them build, endorse or support it.

## License

Proprietary. Copyright HobbyCoders. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).

HobbyCoders team members: development and contribution guides are in [docs/internal](docs/internal/README.md).
