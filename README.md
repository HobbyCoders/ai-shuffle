# AI Shuffle

**Your AI agents, on your own machines, from anywhere.**

AI Shuffle is a free, open-source workspace for running AI agents on your PC, home server or NAS. Sign in with the AI account you already have and put agents to work: writing code, running scheduled jobs, answering your messages, or handling chores across your machines. Check in from your phone.

[Watch the tour](https://hobbycoders.com/ai-shuffle) · [Download](https://hobbycoders.com/ai-shuffle/downloads) · [Docs](docs/content/getting-started/_index.md) · [Discord](https://hobbycoders.com/discord)

![Chat transcript with a folded Worked for work log](docs/images/readme/folded-work-log.png)

## Why AI Shuffle

- **Use the AI you already pay for.** Claude, ChatGPT or SuperGrok subscriptions, or API keys for Claude, OpenAI, OpenRouter, xAI and local models. Your provider bills you directly; we add no markup and never see your prompts.
- **Runs on your hardware.** Code, chats and keys stay on your machines. No account needed.
- **Mix models freely.** Any signed-in model can hand work to any other. A Claude session can delegate to a ChatGPT model, or the other way round.
- **All your machines, one app.** Connect your desktop, laptop and server, move chats between them, and check in from the Android app.
- **All your usage in one place.** See what's left on every plan, with reset times, without visiting each provider's site.
- **A smarter harness.** Every session runs on [Fluid](#fluid-our-own-agent-harness), our own agent engine: compaction that forgets nothing, memory that learns, and batched tool calls.
- **Free and open source.** AGPL-3.0, with no license key, trial or usage cap.

## Install

Every download is free and needs no account or license key.

| Platform | How |
| --- | --- |
| Windows or Linux | Desktop app with a built-in worker, from [hobbycoders.com](https://hobbycoders.com/ai-shuffle/downloads) |
| Server or NAS | Docker Compose (below) |
| Android | APK from the [downloads page](https://hobbycoders.com/ai-shuffle/downloads); [Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle) testing track. Connects to your workers; does not run agents itself. |
| macOS and iOS | Coming soon |

**Docker:** a 64-bit Intel or AMD machine with Docker; 8 GB of memory recommended. In an empty folder:

```sh
curl -fsSLo compose.yaml https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docs/install/compose/compose.yaml
curl -fsSLo .env https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docs/install/compose/env.example
docker compose up -d
```

Then open `http://localhost:8000` on that machine. It pulls the public image `ghcr.io/hobbycoders/ai-shuffle-server`; the [Docker Compose guide](docs/content/getting-started/install-docker-compose.md) covers network access and storage.

Next, follow [first run](docs/content/getting-started/first-run.md) to connect your AI account. For a Claude subscription, setup can install Claude Code with Anthropic's installer; you sign in yourself, and no Claude Code binary is bundled.

## What you can do

- **Code with agents.** Point a project at a folder; agents read, edit, test and commit. Run several chats at once, each in its own git worktree, and let agents hand parts of a job to [subagents](docs/content/features/subagents.md).
- **Work across machines.** A [worker network](docs/content/features/worker-network.md) shares settings, keys and chats. Move a chat mid-task, or let an agent start and run chats on your other machines for you.
- **Automate.** [Automations](docs/content/features/automations.md) run agents on a schedule, a timer, a webhook or an incoming message, with spending limits and approval steps.
- **Connect your apps.** [Connectors](docs/content/connectors/_index.md) for Telegram, Discord, Slack, WhatsApp, Signal, iMessage, Gmail, Home Assistant, Philips Hue, Spotify and more, plus any MCP server and [plugins](docs/content/features/plugins.md).
- **Talk and create.** Live [voice calls](docs/content/features/voice.md) on OpenAI's realtime voice models, dictation, and images, video and audio from fal.ai's model library. Higgsfield support is coming soon.
- **Stay in control.** Choose what each agent may do, approve risky steps, and block commands with [hooks](docs/content/features/hooks.md).
- **And more.** [Terminals](docs/content/features/terminals.md) beside the chat, [notes](docs/content/features/notes.md), and a [REST API](docs/content/api-reference/_index.md).

![Phone worker list in the mobile app](docs/images/readme/phone-workers.png)

![Automations page with scheduled agent tasks](docs/images/readme/automations.png)

## Fluid: our own agent harness

Fluid is the engine that runs your agents: it talks to the model, runs tools, asks for approval, and manages context and memory. It lives in [`fluid/`](fluid/README.md) and also runs on its own from the command line.

**What we fixed.** If you have run long sessions in other agent harnesses, you have likely hit these:

- **Compaction that forgets nothing.** Most harnesses compact by having the model summarize, which is slow and lossy; small drifts from your original ask compound over long work. Fluid compacts in seconds with no summary pass. The full transcript is always kept, and the model can search and re-read any part of it.
- **Memory that maintains itself.** Instead of a memory file that bloats and needs constant pruning, the model keeps checking and updating what it knows from what you say and correct.
- **Skills it writes for you.** Work you repeat becomes a reusable skill automatically. Each skill is tested before it is kept, and broken ones are flagged.
- **Many tool calls in one step.** A Python layer lets the model batch independent tool calls in a single step instead of one round trip each, with every call still going through approvals.
- **Cheap long sessions.** The prompt is laid out so providers can reuse almost all of it between turns.

**Why not just wrap each provider's agent?** Claude Code, Codex and the rest each have their own tools, permissions, memory and limits. Wrapping them would mean features that differ by provider and bugs we can't fix. With Fluid, an agent works the same way whichever model you pick.

**The exception: Claude subscriptions.** Anthropic only allows Claude Pro and Max subscriptions through its own Claude Code, so for those Fluid drives the official Claude Code CLI and never reuses your sign-in elsewhere. Those sessions keep AI Shuffle's tools, connectors and memory, but compaction and tool calling are Claude Code's own. For Claude fully inside Fluid, use an Anthropic API key.

<details>
<summary>How each provider runs</summary>

| Provider | How Fluid runs it | Sign-in |
| --- | --- | --- |
| Anthropic (Claude) API | Fluid's own loop | API key |
| OpenAI | Fluid's own loop | API key or ChatGPT subscription |
| xAI | Fluid's own loop | API key or SuperGrok subscription |
| OpenRouter and OpenAI-compatible gateways | Fluid's own loop | API key |
| Local models (llama.cpp, Ollama and similar) | Fluid's own loop | Optional key |
| Claude Pro or Max subscription | Official Claude Code CLI, driven by Fluid | Claude Code's own sign-in |

</details>

## Free and Cloud

The app is free: run it on your own hardware, on your home network or your own VPN, with no HobbyCoders account. Updates and same-network worker sync are free too.

[AI Shuffle Cloud](docs/content/getting-started/cloud.md) is the optional paid part for reaching your workers from anywhere without a VPN: remote access, sync across networks, public connector endpoints, plugin sign-in relays and in-app support. A free account includes one remote worker, no card needed. Ending a plan never disables the local app, and AI usage is always between you and your provider.

## Contributing, security and license

- **Contributing:** outside contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for DCO sign-off, local checks and draft pull requests; team setup is in [docs/internal/CONTRIBUTING.md](docs/internal/CONTRIBUTING.md).
- **Security:** report vulnerabilities privately to support@hobbycoders.com, and never post secrets in issues. See [SECURITY.md](SECURITY.md) and [SUPPORT.md](SUPPORT.md).
- **License:** [GNU AGPL-3.0 only](LICENSE). Copyright (c) 2026 HobbyCoders. "AI Shuffle", "HobbyCoders" and the shuffle mark are trademarks of HobbyCoders; modified builds may not ship under those names. See [TRADEMARKS.md](TRADEMARKS.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
- **Not affiliated:** AI Shuffle is not affiliated with Anthropic, OpenAI, xAI or any other AI provider. None of them build, endorse or support it; their names and marks belong to their respective owners.
