# AI Shuffle

Run AI coding and automation agents on your own machines, from anywhere, with any supported model and your own AI accounts.

[Watch the tour](https://hobbycoders.com/ai-shuffle).

![Chat transcript with a folded Worked for work log](docs/images/readme/folded-work-log.png)

![Phone worker list in the mobile app](docs/images/readme/phone-workers.png)

![Automations page with scheduled agent tasks](docs/images/readme/automations.png)

## Why not just a terminal?

AI Shuffle runs the same tools you use with Claude Code or Codex and adds a workspace for your fleet. Put a NAS, desktop and laptop in one account, hand a session between them, or start work from your phone. Sessions, projects, worktrees, pull requests and CI stay in one place. Run work on a schedule or from a connector such as Telegram, and choose Claude, OpenAI, OpenRouter, xAI or a local model per profile. Use subscription sign-in where supported, or bring an API key; your provider bills you directly.

The products below cover different jobs. Coder manages development environments as well as agents; OpenHands supports several agent backends. “—” means not verified, not necessarily unavailable. “Any provider” means configurable providers or compatible endpoints, not a promise that every model works. Connectors include MCP integrations.

| Capability | AI Shuffle | [Claude Code](https://code.claude.com/docs/en/overview) | [Codex CLI](https://learn.chatgpt.com/codex/cli) | [OpenHands](https://docs.openhands.dev/overview/introduction) | [Coder](https://coder.com/docs/about) |
| --- | --- | --- | --- | --- | --- |
| Runs on your own hardware | Yes | Yes | Yes | Yes | Yes |
| Several machines in one place | Worker fleet and handoff | — | — | Multiple backends | Managed workspaces |
| Phone app | Android | Claude mobile app | — | — | — |
| Any provider | Supported providers and local models | — | Custom providers and local endpoints | LiteLLM and local models | Multiple providers and self-hosted endpoints |
| Subscription sign-in (not only API keys) | Claude and ChatGPT | Claude | ChatGPT | Via external agent CLIs | — |
| Automations | Schedules and events | Schedules and CLI | CLI scripting | Schedules and events | API and CI workflows |
| Connectors | Built-in connectors and MCP | MCP | Plugins and MCP | MCP | MCP |
| Open source | AGPL-3.0-only | — | Yes | Yes | AGPL-3.0 |

Comparison checked against the linked products’ documentation on 2026-10-11. Features and plan requirements can change.

## Install

For Docker Compose, use a 64-bit Intel or AMD machine with Docker installed; 8 GB of memory is recommended. After the repository is public, run these three commands in an empty directory. Until publication, the raw download URLs return 404:

```sh
curl -fsSLo compose.yaml https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docs/install/compose/compose.yaml
curl -fsSLo .env https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docs/install/compose/env.example
docker compose up -d
```

Compose pulls the public image `ghcr.io/hobbycoders/ai-shuffle-server`. No registry token or license key is needed. Open `http://localhost:8000` on that machine, then follow [first run](docs/content/getting-started/first-run.md). See the [Docker Compose guide](docs/content/getting-started/install-docker-compose.md) for network access and storage settings.

Download the [Windows and Linux installers](https://github.com/HobbyCoders/ai-shuffle/releases/latest). Sideload the Android APK from the [release page](https://github.com/HobbyCoders/ai-shuffle/releases/latest). The [Google Play listing](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle) is coming with the production rollout; it is not public yet. The phone app connects to a worker; it does not run agents itself.

If you choose Claude, setup can install Claude Code using Anthropic’s installer. You sign in yourself; no Claude Code binary is bundled.

## Free and Cloud

The app is free and open source. Run it on your own hardware with your own AI accounts, on your LAN or your own VPN, with no HobbyCoders account or license key. Updates and same-LAN worker sync are free. AI Shuffle Cloud provides managed remote access, worker sync across networks, public endpoints for connectors, the OAuth relay for plugin sign-ins, and in-app support reports. A free Cloud account includes one remote worker with no card; a paid plan includes three workers and the other Cloud services. Ending a plan does not disable the local app; Cloud stops serving the extra tunnels. AI usage is paid to your provider, not included in Cloud. The [Cloud pricing page](https://hobbycoders.com/ai-shuffle/pricing) is coming with the public release.

## Contributing

Outside contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for DCO sign-off, local checks and draft pull requests. Team setup remains in [docs/internal/CONTRIBUTING.md](docs/internal/CONTRIBUTING.md).

## Security

Report vulnerabilities privately to support@hobbycoders.com. Never post secrets in issues. See [SECURITY.md](SECURITY.md) and [SUPPORT.md](SUPPORT.md).

## License

[GNU AGPL-3.0 only](LICENSE). Copyright (c) 2026 HobbyCoders. “AI Shuffle”, “HobbyCoders” and the shuffle mark are trademarks of HobbyCoders; modified builds may not ship under those names. See [TRADEMARKS.md](TRADEMARKS.md) and [third-party notices](THIRD_PARTY_NOTICES.md).

## Not affiliated

AI Shuffle is not affiliated with Anthropic, OpenAI, xAI or any other AI provider. None of them build, endorse or support AI Shuffle. Their names and marks belong to their respective owners.

## Links

[Website](https://hobbycoders.com/ai-shuffle) · [Docs](docs/content/getting-started/_index.md) · [Discord](https://hobbycoders.com/discord) · [FAQ](https://hobbycoders.com/ai-shuffle/faq)
