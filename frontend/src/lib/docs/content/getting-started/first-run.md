<!--
title: First Run
description: The setup wizard step by step: owner password, license, connecting an AI provider, workspace and first agent
order: 6
-->

# First run

The first time you open AI Shuffle, it starts the setup wizard. It takes about five minutes. Its steps are **Welcome**, **Password**, **License**, **AI provider**, **Workspace**, **First run** and **Account & GitHub**; the progress bar at the top shows where you are. You can close the page and come back: the wizard starts again from the top and keeps what you already saved. Nothing runs an agent until an AI provider is connected, and setup cannot be finished without one.

## 1. Welcome

The first screen says what AI Shuffle is and what it is not:

- **Agents act with your access.** They can read and change files in the projects you give them, run commands (inside the container on Docker and Unraid, as your user on the desktop app) and use the accounts you connect, such as GitHub.
- **You bring your own AI account.** AI Shuffle runs agents with **your own** AI provider account, by subscription sign-in or API key, and never pays for, resells, shares or sees your AI usage. Your prompts go from your worker straight to your AI provider.
- **Update checks.** AI Shuffle checks for updates when it starts and once a day, without asking, like other desktop apps. The check sends no prompts, files or usage data; once licensing is live it also renews your license. The screen links to the [privacy policy](https://hobbycoders.com/privacy#updates), which lists what is sent. See [Updates](/docs/getting-started/updates) and [License](/docs/getting-started/license).

Choose **Continue**.

## 2. Password

Next, AI Shuffle asks you to create the password for the owner of this install. Type it twice and choose **Save password**. On a fresh Docker or Unraid install, the welcome screen and this step come before anything else, because until the password exists no browser can sign in. If you set **Owner password** in the Unraid template (or `OWNER_PASSWORD` in `.env` for Docker Compose) before the first start, the password already exists: the step is skipped and your browser asks you to sign in with it instead.

- Every browser signs in with it once and stays signed in on that device for 30 days. With Docker this includes a browser on the server itself: Docker hands AI Shuffle your browser's connection from its own network address, so it cannot tell the server's browser from any other.
- The desktop app never asks for it, and its wizard has no password step. Its window signs itself in with a key that changes on every launch, and **Open in Browser** signs your default browser in the same way.
- Choose a long password (at least 12 characters) you use nowhere else, and keep it in a password manager. Anyone who has it controls your agents and everything they can reach.
- Until a password exists, any browser that can reach AI Shuffle can create it. Create it right after the first start, or set it in the template or `.env` beforehand.

Phones and remote browsers linked through your HobbyCoders account (see [Remote access](/docs/getting-started/remote-access)) use their own device authorization instead.

To change the password later, open **Settings → Security → Owner password**. The same place has **Trusted LAN, no login**, off by default: when on, every device on your network or tailnet is the owner without a password. Leave it off unless the network is yours alone.

## 3. License

Enter your license key (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`, from your license email) and choose **Activate**. While the beta runs, this step reads **Beta: no license needed** and you just continue. If you gave the key when installing (the desktop launcher, `LICENSE_KEY` in `.env`, or **License key** in the Unraid template), it is already active and the step says so. If you have no key yet, choose **Add it later** and add it in **Settings → System → License**; until a license is active, agents do not start new runs, and nothing else is affected. See [License](/docs/getting-started/license).

## 4. AI provider

Connect at least one AI provider. Any of them works, and you can add more later in **Settings → Providers**. Each provider bills you directly under its own terms. The wizard lists them in the same order as **Settings → Providers**:

| Provider | How you connect it |
| --- | --- |
| **OpenAI** | **Subscription** (ChatGPT sign-in) or **API key** |
| **xAI Grok** | **Subscription** (SuperGrok sign-in) or **API key** |
| **Claude** | **Subscription** (sign in to the Claude Code CLI) or **API key** (Anthropic) |
| **OpenRouter** | An OpenRouter API key |
| **Your own gateway** | An endpoint address, its API dialect (**Anthropic Messages** or **OpenAI chat**) and an optional token |

Choose a provider to open it, then choose how to connect it. A subscription is simplest for hands-on work at your desk. **Use an API key for scheduled and unattended runs.** [Modes and costs](/docs/getting-started/modes-and-costs) explains the difference. You can connect several providers. A provider shows **Connected** once the worker can use it and at least one of its models is picked.

**Continue** stays unavailable until one provider is connected. Until then the wizard says **Connect at least one provider to continue.** Choose **Check again** after connecting one somewhere else; it also asks each provider that is not ready yet for its models again. The wizard also checks by itself every few seconds.

### API key

1. Create a key in your provider's console. Set a monthly spend limit there while you are at it.
2. Paste it into the wizard and choose **Save key**. For an OpenAI, xAI or OpenRouter key, AI Shuffle then asks the provider for its models. Pick the ones you want to use and choose **Use selected**. The first model is already picked, so one click is enough. Setup needs at least one model; you can change the list later in **Settings → Providers**. For an Anthropic key, AI Shuffle checks it with Anthropic first, and a key Anthropic refuses is not saved.

If the provider rejects the key, the wizard says so and keeps the key form open. Paste the right key and choose **Save key** again: it replaces the rejected key instead of adding a second one. If the provider cannot be reached (it timed out, was busy or had a server error), the wizard shows the error; choose **Retry**.

### Your own gateway

Give it a name, its endpoint (for example `https://host/v1`), its API dialect and, if it needs one, an auth token. Choose **Connect**, then pick the models you want to use and choose **Use selected**.

AI Shuffle reads a gateway's models from its `/models` list. A gateway without that list cannot be finished in setup, and there is no way to type model names by hand yet. The wizard says so; choose **Remove** and connect another provider.

The key is encrypted and stored only on this worker. It is not copied to your other workers; add it on each worker where you want to use it. AI Shuffle sends it only to that provider, or passes it to the agent processes it starts, and never to HobbyCoders.

### ChatGPT or SuperGrok subscription

1. Choose **Subscription**. The wizard shows a short code.
2. Choose **Open sign-in page**, sign in to your own account on OpenAI's or xAI's page, and enter the code there.
3. The wizard notices the sign-in by itself. Then pick the models you want to use and choose **Use selected**.

AI Shuffle keeps the resulting sign-in encrypted on this worker and renews it as needed. It never sees your password and never sends the sign-in to HobbyCoders. To sign out, remove the subscription in **Settings → Providers**.

### Claude subscription

A Claude subscription works through the standard, unmodified Claude Code CLI, which the Docker image installs on first start. **You** sign in to it yourself through Anthropic's own sign-in page. AI Shuffle never sees, copies or stores your Claude sign-in.

On Claude Code 2.1.287 and newer, each session also loads a small AI Shuffle mod, an add-on in Claude Code's own documented plugin format. It keeps Claude on the dedicated file tools instead of shell commands, and after each turn it passes the plan usage the CLI measured (the share of each limit used and when it resets, nothing else) to the usage panel, so a limit that holds still does not read as stale while a session runs. It does not read your sign-in. Set `FLUID_CLAUDE_MOD=false` to turn it off.

Choose **Claude**, then **Subscription**.

**In the app (recommended on Docker and Unraid):** the wizard opens a terminal connected to the container and runs the CLI's login there.

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

Then choose **Check again** in the wizard (it also checks by itself every few seconds). Run the command as `appuser` (the `-u appuser` part): the CLI's login belongs to the user the agents run as.

**Desktop app:** the wizard has no terminal. It shows the command to run, `claude auth login`, in your own terminal on the same computer; then choose **Check again**.

The login is stored in the container's Claude Code folder (`/home/appuser/.claude`, a volume or appdata folder on the host), so it survives updates and container re-creation. To sign out or switch accounts, run `claude auth logout` the same way, then sign in again.

## 5. Workspace

The workspace is where your projects live. Inside the container it is always `/workspace`; the wizard shows which folder on the host backs it:

- Docker Compose: `WORKSPACE_DIR` from your `.env` (`./workspace` next to `compose.yaml` by default).
- Unraid: the **Workspace** path in the template (`/mnt/user/appdata/ai-shuffle/workspace` by default).

Choose `/workspace` or a folder inside it. If you pick a folder outside `/workspace`, the wizard warns you: its files live only inside the container and are lost when the container is re-created. Agents can read and change files in the projects you give them, so keep things you do not want an agent to touch outside the workspace.

On the desktop app, the workspace is an ordinary folder on your computer.

Choose **Continue** when the folder is right.

## 6. First run

The wizard shows a one-line task and what it runs on: a model name, then **on** and the provider's name. Choose **Run it**. The task runs in a new chat session with no project and no access to your files, and you watch the answer stream in. If you have no agent yet, the wizard creates one called **Assistant**, which you can rename or delete later. The session stays in your sidebar.

If no connected provider has a model picked, the step names the provider that needs one and links back to **AI provider**, so you can pick one there before running.

If the run fails, the wizard shows what went wrong (for example **Agents cannot start yet** and what is missing); see [Troubleshooting](/docs/getting-started/troubleshooting). Fix it and choose **Try again**, or choose **Continue anyway** and fix it later.

## 7. Account & GitHub (optional)

- **HobbyCoders account:** sign in to link this worker to the Android app, to your other workers and to remote access. Local use never needs an account. To join another worker's network with an invite instead, choose **Use a worker invite instead**. You can do either later in **Settings → Devices**.
- **GitHub:** sign in to the GitHub CLI for repository features (pull requests, issues, CI). You can do this later in Settings.
- **Earlier conversations:** if this computer has Claude Code CLI conversations from the last 30 days, the step offers to import them as sessions. It only appears when there is something to import.

Then choose **Finish**. Continue with the [Quick start](/docs/getting-started/quick-start).

## Which steps you can skip

- The license key, if you have none yet (**Add it later**). Agents do not start new runs until a license is active.
- The first agent run, after a failed attempt (**Continue anyway**).
- The account and GitHub.

Everything else is required, and there is no way to finish setup without at least one connected AI provider. If a credential stops working later (for example a subscription was signed out), agents wait instead of falling back to anyone else's account; set it up again in **Settings → Providers**.
