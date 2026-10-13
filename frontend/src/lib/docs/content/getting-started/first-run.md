<!--
title: First Run
description: The setup wizard step by step: owner password, optional Cloud account, AI provider, workspace and first agent
order: 6
-->

# First run

The first time you open AI Shuffle, the setup wizard starts. It takes about five minutes. You can close the page and come back: the wizard keeps what you already saved. You need at least one AI provider to finish.

## 1. Welcome

The welcome screen explains three things:

- **Agents act with your access.** They can read and change files in the projects you give them, run commands, and use the accounts you connect, such as GitHub.
- **You bring your own AI account.** Your prompts go from your worker straight to your AI provider. AI Shuffle never pays for, resells or sees your AI usage.
- **Update checks.** AI Shuffle checks for updates when it starts and once a day. The check sends no prompts, files or usage data. See [Updates](/docs/getting-started/updates) and the [privacy policy](https://hobbycoders.com/privacy#updates).

Choose **Continue**.

## 2. Password

Create the password for the owner of this install. Type it twice and choose **Save password**. If you already set `OWNER_PASSWORD` when you installed with Docker, this step is skipped and your browser asks you to sign in instead.

- Use at least 12 characters, and a password you use nowhere else. Anyone who has it controls your agents.
- Each browser signs in once and stays signed in on that device for 30 days.
- The desktop app never asks for the password. It signs in by itself.
- Until a password exists, any browser that can reach AI Shuffle can create it, so do this right after the first start.

Phones and browsers linked through your HobbyCoders account sign in with their own device approval instead (see [Remote access](/docs/getting-started/remote-access)).

To change the password later, open **Settings → Security → Owner password**.

## 3. Optional Cloud plan

No key is needed to run AI Shuffle. If you have a paid Cloud plan, you can enter its license key to link this worker. A key supplied at install time is activated automatically.

You can skip linking a plan and use every local feature now. Manage remote access and paid seats in **Settings → Cloud**. See [AI Shuffle Cloud](/docs/getting-started/cloud).

## 4. AI provider

Connect at least one AI provider. You can add more later in **Settings → Providers**. Each provider bills you directly.

| Provider | How you connect it |
| --- | --- |
| **OpenAI** | ChatGPT subscription sign-in, or an API key |
| **xAI Grok** | SuperGrok subscription sign-in, or an API key |
| **Claude** | Claude subscription (through the Claude Code CLI), or an Anthropic API key |
| **OpenRouter** | An OpenRouter API key |
| **Your own gateway** | Its address, its API type and an optional token |

A subscription is simplest for hands-on work. **Use an API key for scheduled and unattended runs.** See [Modes and costs](/docs/getting-started/modes-and-costs).

**Continue** unlocks once a provider is connected and at least one of its models is picked.

### API key

1. Create a key in your provider's console. Set a monthly spend limit there too.
2. Paste it into the wizard and choose **Save key**.
3. Pick the models you want to use and choose **Use selected**.

If the provider rejects the key, paste the right one and choose **Save key** again. The key is stored encrypted and is shared with your other connected workers, so they can use the same provider.

### Your own gateway

Enter a name, its address (for example `https://host/v1`), its API type and, if needed, a token. Choose **Connect**, then pick your models and choose **Use selected**. The gateway must list its models; one that does not cannot be set up yet.

### ChatGPT or SuperGrok subscription

1. Choose **Subscription**. The wizard shows a short code.
2. Choose **Open sign-in page**, sign in to your account on the provider's page, and enter the code.
3. The wizard notices the sign-in. Pick your models and choose **Use selected**.

AI Shuffle never sees your password. The sign-in is stored encrypted and is shared with your other connected workers.

### Claude subscription

A Claude subscription works through Anthropic's Claude Code CLI. Install it from the wizard or from **Settings → Providers → Claude**, then sign in to it yourself on Anthropic's page. AI Shuffle never sees or stores your Claude sign-in.

1. Choose **Claude**, then **Subscription**, then **Sign in**. A terminal opens and starts the sign-in.
2. Open the link it shows and sign in to your Claude account.
3. If Anthropic's page shows a code, paste it under the terminal and choose **Send**.
4. Choose **Check again**. The wizard shows which account is signed in.

On Docker you can also sign in from a terminal on the host:

```bash
docker exec -it -u appuser ai-shuffle claude auth login
```

On the desktop app, run `claude auth login` in your own terminal, then choose **Check again**.

The sign-in is kept when you update or re-create the container. To sign out or switch accounts, run `claude auth logout`, then sign in again.

## 5. Workspace

The workspace is where your projects live. On Docker it is `/workspace` inside the container, which is the workspace folder you chose when installing. On the desktop app it is an ordinary folder on your computer.

Pick `/workspace` or a folder inside it, then choose **Continue**. Keep files you do not want agents to touch outside the workspace.

## 6. First run

The wizard shows a short task and the model it runs on. Choose **Run it** and watch the answer stream in. If you have no agent yet, the wizard creates one called **Assistant**, which you can rename or delete later.

If the run fails, the wizard says why. Fix it and choose **Try again**, or choose **Continue anyway** and fix it later. See [Troubleshooting](/docs/getting-started/troubleshooting).

## 7. Account & GitHub (optional)

- **HobbyCoders account:** sign in to use the Android app, connect your other workers and turn on remote access. Local use never needs an account. You can also join another worker with an invite instead.
- **GitHub:** sign in for repository features like pull requests, issues and CI.
- **Earlier conversations:** if this computer has recent Claude Code conversations, you can import them as chats.

Choose **Finish**, then continue with the [Quick start](/docs/getting-started/quick-start).

## Which steps you can skip

- Cloud linking is optional. Agents run without a Cloud account or key.
- The first agent run, after a failed attempt.
- The account and GitHub step.

Everything else is required. If a provider sign-in stops working later, agents wait instead of using any other account. Set it up again in **Settings → Providers**.
