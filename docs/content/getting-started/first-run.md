<!--
title: First Run
description: The setup wizard step by step: owner password, license, mode, Claude Code sign-in or API key, workspace and first agent
order: 6
-->

# First run

The first time you open AI Shuffle, it starts the setup wizard. It takes about five minutes. You can close the page and come back: the wizard starts again from the top and keeps what you already saved. Nothing runs an agent until a credential is set up, and setup cannot be finished without one.

## 1. Welcome

The first screen says what AI Shuffle is and what it is not:

- **Agents act with your access.** They can read and change files in the projects you give them, run commands (inside the container on Docker and Unraid, as your user on the desktop app) and use the accounts you connect, such as GitHub.
- **You bring your own AI account.** AI Shuffle runs agents with **your own** Claude subscription or API key and never pays for, resells, shares or sees your AI usage. Your prompts go from your worker straight to your AI provider.
- **Update checks.** AI Shuffle checks for updates when it starts and once a day, without asking, like other desktop apps. The check sends no prompts, files or usage data; once licensing is live it also renews your license. The screen links to the [privacy policy](https://hobbycoders.com/privacy#updates), which lists what is sent. See [Updates](/docs/getting-started/updates) and [License](/docs/getting-started/license).

Choose **Continue**.

## 2. Owner password

Next, AI Shuffle asks you to create the password for the owner of this install. On a fresh Docker or Unraid install, the welcome screen and this step come before anything else, because until the password exists no browser can sign in. If you set **Owner password** in the Unraid template (or `OWNER_PASSWORD` in `.env` for Docker Compose) before the first start, the password already exists: the step is skipped and your browser asks you to sign in with it instead.

- Every browser signs in with it once and stays signed in on that device for 30 days. With Docker this includes a browser on the server itself: Docker hands AI Shuffle your browser's connection from its own network address, so it cannot tell the server's browser from any other.
- The desktop app never asks for it, and its wizard has no password step. Its window signs itself in with a key that changes on every launch, and **Open in Browser** signs your default browser in the same way.
- Choose a long password (at least 12 characters) you use nowhere else, and keep it in a password manager. Anyone who has it controls your agents and everything they can reach.
- Until a password exists, any browser that can reach AI Shuffle can create it. Create it right after the first start, or set it in the template or `.env` beforehand.

Phones and remote browsers linked through your HobbyCoders account (see [Remote access](/docs/getting-started/remote-access)) use their own device authorization instead.

To change the password later, open **Settings → Security → Owner password**. The same place has **Trusted LAN, no login**, off by default: when on, every device on your network or tailnet is the owner without a password. Leave it off unless the network is yours alone.

## 3. License

Enter your license key (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`, from your license email) and choose **Activate**. While the beta runs, this step reads **Beta: no license needed** and you just continue. If you gave the key when installing (the desktop launcher, `LICENSE_KEY` in `.env`, or **License key** in the Unraid template), it is already active and the step says so. If you have no key yet, choose **Add it later** and add it in **Settings → System → License**; until a license is active, agents do not start new runs, and nothing else is affected. See [License](/docs/getting-started/license).

## 4. Choose how Claude Code is paid for

AI Shuffle runs Claude Code in one of two modes. Pick one; you can change it later in Settings, and AI Shuffle never switches between them on its own.

| Mode | Who bills you | Best for |
| --- | --- | --- |
| **Claude subscription** | Anthropic, through your Pro or Max plan | Hands-on work while you are at the keyboard |
| **API key** | Anthropic, per use, on your Console account | Scheduled jobs, overnight runs, many agents at once |

The wizard's advice is the same as this guide's: **use an API key for scheduled and unattended runs**. [Modes and costs](/docs/getting-started/modes-and-costs) explains the difference in detail. Other providers (OpenAI, xAI, OpenRouter, fal.ai) can be added in **Settings → Providers** after setup; they bill you under their own terms.

## 5a. Subscription: sign in to the Claude Code CLI

In subscription mode, AI Shuffle runs the standard, unmodified Claude Code CLI inside the container, and **you** sign in to it yourself through Anthropic's own sign-in page. AI Shuffle never sees, copies or stores your Claude sign-in.

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

**Continue** stays unavailable until the CLI reports a sign-in. Until then the wizard shows **Agents cannot start yet** and what is missing.

The login is stored in the container's Claude Code folder (`/home/appuser/.claude`, a volume or appdata folder on the host), so it survives updates and container re-creation. To sign out or switch accounts, run `claude auth logout` the same way, then sign in again.

## 5b. API key: paste your key

1. Create a key in the Anthropic Console, under **API keys**. Set a monthly spend limit in the Console while you are there.
2. Paste it into the wizard and choose **Save**. AI Shuffle checks the key with Anthropic before saving it; a key Anthropic refuses is not saved, and the wizard says why.

The key is encrypted and stored only on this worker. It is not copied to your other workers; add it on each worker where you want to use it. AI Shuffle passes it only to the agent processes it starts, and never to HobbyCoders.

**Continue** stays unavailable until a key is saved.

## 6. Workspace

The workspace is where your projects live. Inside the container it is always `/workspace`; the wizard shows which folder on the host backs it:

- Docker Compose: `WORKSPACE_DIR` from your `.env` (`./workspace` next to `compose.yaml` by default).
- Unraid: the **Workspace** path in the template (`/mnt/user/appdata/ai-shuffle/workspace` by default).

Choose `/workspace` or a folder inside it. If you pick a folder outside `/workspace`, the wizard warns you: its files live only inside the container and are lost when the container is re-created. Agents can read and change files in the projects you give them, so keep things you do not want an agent to touch outside the workspace.

On the desktop app, the workspace is an ordinary folder on your computer.

## 7. First agent

The wizard shows a one-line task and says what pays for it: your Claude plan (with the account the CLI is signed in as) or your API key. Choose **Run it**. The task runs in a new chat session with no project and no access to your files, and you watch the answer stream in. If you have no agent yet, the wizard creates one called **Assistant**, which you can rename or delete later. The session stays in your sidebar.

If the run fails, the message says what is missing; see [Troubleshooting](/docs/getting-started/troubleshooting). Fix it and choose **Try again**, or choose **Continue anyway** and fix it later.

## 8. Optional: account and GitHub

- **HobbyCoders account:** sign in to link this worker to the Android app, to your other workers and to remote access. Local use never needs an account. To join another worker's network with an invite instead, choose **Use a worker invite instead**. You can do either later in **Settings → Devices**.
- **GitHub:** sign in to the GitHub CLI for repository features (pull requests, issues, CI). You can do this later in Settings.

Then choose **Finish**. Continue with the [Quick start](/docs/getting-started/quick-start).

## Which steps you can skip

- The license key, if you have none yet (**Add it later**). Agents do not start new runs until a license is active.
- The first agent run, after a failed attempt (**Continue anyway**).
- The account and GitHub.

Everything else is required, and there is no way to finish setup without a Claude sign-in or an API key. If a credential stops working later (for example the CLI was signed out), agents wait instead of falling back to anyone else's account; set it up again in **Settings → Providers**.
