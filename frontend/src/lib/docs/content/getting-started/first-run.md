<!--
title: First Run
description: The setup wizard step by step: owner password, mode, Claude Code sign-in or API key, workspace and first agent
order: 6
-->

# First run

The first time you open AI Shuffle, it starts the setup wizard. It takes about five minutes. You can leave the wizard and come back; nothing runs an agent until a credential is set up.

<!-- verify against wizard v2 -->

## 1. Welcome

The first screen says what AI Shuffle is and what it is not: it runs agents with **your own** AI account and never pays for, resells or sees your AI usage. Nothing is sent to HobbyCoders except update checks and, once licensing is live, license checks (see [Updates](/docs/getting-started/updates) and [License](/docs/getting-started/license)).

## 2. Owner password

<!-- verify against wizard v2 -->

Create the password for the owner of this install. If you set **Owner password** in the Unraid template, the wizard skips this step.

- Browsers on the machine running AI Shuffle itself work without it.
- Every other browser, on your LAN or your tailnet, signs in with it once and stays signed in on that device.
- Choose a long password you use nowhere else, and keep it in a password manager. Anyone who has it controls your agents and everything they can reach.

Phones and remote browsers linked through your HobbyCoders account (see [Remote access](/docs/getting-started/remote-access)) use their own device authorization instead.

## 3. License

The wizard asks for your license key (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`, from your license email). While the beta runs, this step reads **Beta: no license needed** and you can continue. You can also add the key later in **Settings → License**. See [License](/docs/getting-started/license).

## 4. Choose how Claude Code is paid for

<!-- verify against wizard v2 -->

AI Shuffle runs Claude Code in one of two modes. Pick one; you can change it later in Settings, and AI Shuffle never switches between them on its own.

| Mode | Who bills you | Best for |
| --- | --- | --- |
| **Claude subscription** | Anthropic, through your Pro or Max plan | Hands-on work while you are at the keyboard |
| **API key** | Anthropic, per use, on your Console account | Scheduled jobs, overnight runs, many agents at once |

[Modes and costs](/docs/getting-started/modes-and-costs) explains the difference in detail. Other providers (OpenAI, xAI, OpenRouter, fal.ai) can be added in Settings at any time; they bill you under their own terms.

## 5a. Subscription: sign in to the Claude Code CLI

In subscription mode, AI Shuffle runs the standard, unmodified Claude Code CLI inside the container, and **you** sign in to it yourself through Anthropic's own sign-in page. AI Shuffle never sees, copies or stores your Claude sign-in.

**In the app (recommended):** the wizard opens a terminal connected to the container and runs the CLI's login there.

1. Choose **Sign in**. The terminal starts `claude auth login`.
2. Open the link it prints, in any browser, and sign in to your Claude account on Anthropic's page.
3. If Anthropic's page shows a code, paste it into the field under the terminal and choose **Send**.
4. Choose **Check again**. The wizard shows the account the CLI is signed in as, so you can see whose plan the usage goes to.

**From the host instead:** open a terminal on the machine running Docker (on Unraid, **>_ Terminal** in the web interface) and run:

```bash
# Docker Compose
docker exec -it -u appuser ai-shuffle claude auth login

# Unraid
docker exec -it -u appuser AI-Shuffle claude auth login
```

Then choose **Check again** in the wizard. Run the command as `appuser` (the `-u appuser` part): the CLI's login belongs to the user the agents run as.

The login is stored in the container's Claude Code folder (`/home/appuser/.claude`, a volume or appdata folder on the host), so it survives updates and container re-creation. To sign out or switch accounts, run `claude auth logout` the same way, then sign in again.

## 5b. API key: paste your key

<!-- verify against wizard v2 -->

1. Create a key in the Anthropic Console, under **API keys**. Set a monthly spend limit in the Console while you are there.
2. Paste it into the wizard and choose **Save**.

The key is encrypted and stored only on this worker. It is not copied to your other workers; add it on each worker where you want to use it. AI Shuffle passes it only to the agent processes it starts, and never to HobbyCoders.

## 6. Workspace

The workspace is where your projects live. Inside the container it is always `/workspace`; the wizard shows which folder on the host backs it:

- Docker Compose: `WORKSPACE_DIR` from your `.env` (`./workspace` next to `compose.yaml` by default).
- Unraid: the **Workspace** path in the template (`/mnt/user/appdata/ai-shuffle/workspace` by default).

Choose `/workspace` or a folder inside it. Agents can read and change files in the projects you give them, so keep things you do not want an agent to touch outside the workspace.

## 7. First agent

<!-- verify against wizard v2 -->

The wizard creates a small sample project in your workspace and runs one short, read-only prompt with the mode you chose, naming the account or key that pays for it. You watch the answer stream in. If it fails, the message says what is missing; see [Troubleshooting](/docs/getting-started/troubleshooting).

## 8. Optional: account and GitHub

- **HobbyCoders account:** sign in to link this worker to the Android app, to your other workers and to remote access. Local use never needs an account. You can do this later in **Settings → Devices**.
- **GitHub:** sign in to the GitHub CLI for repository features (pull requests, issues, CI). You can do this later in Settings.

Then choose **Finish**. Continue with the [Quick start](/docs/getting-started/quick-start).

## If you skip a step

If no Claude sign-in, API key or other provider is set up, AI Shuffle opens but does not start agents. The main screen shows what is missing and how to set it up. Nothing falls back to anyone else's account.
