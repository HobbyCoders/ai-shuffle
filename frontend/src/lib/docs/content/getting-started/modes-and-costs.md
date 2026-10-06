<!--
title: Modes and Costs
description: Subscription or API key, what each costs you, and when to use an API key
order: 7
-->

# Modes and costs

AI Shuffle includes no AI usage. You pay HobbyCoders for the app, and you pay your AI provider for the AI. The two bills never mix: AI Shuffle never pays for, resells, marks up or routes AI usage, and never uses anyone else's account for your work.

## Providers

Connect any of these, one or several. Each uses your own account and bills you under its own terms.

- **Claude:** a Claude subscription (Pro or Max) through the Claude Code CLI, an Anthropic API key, or, if you turn it on, your own cloud provider or gateway (see [Claude through a cloud provider or gateway](#claude-through-a-cloud-provider-or-gateway)).
- **OpenAI:** a ChatGPT subscription sign-in, or an OpenAI API key.
- **OpenRouter:** an OpenRouter API key, one key for many models.
- **xAI Grok:** a SuperGrok subscription sign-in, or an xAI API key.
- **Custom gateway:** any compatible endpoint with your own token, including a local model server.
- **fal.ai:** a media key for images, video and audio. It does not run agents on its own.

Images can also come from a ChatGPT subscription sign-in. Under **Settings > Models**, choose **ChatGPT subscription (OpenAI built-in)** for Text to Image or Image Editing, and every chat can make or edit images with it, whatever its text model. It needs no fal.ai key. If that ChatGPT account is not signed in on the worker, the image tools say so and use nothing else. Without that choice, OpenAI's built-in image tool appears only in chats that run on an OpenAI model.

Voice mode uses your own OpenAI account.

## Subscription or API key

Agents run on exactly the credential you chose. AI Shuffle never moves your work to another account on its own, and if a credential stops working, agents wait instead of falling back to anything.

### Subscription

You sign in to your own subscription on the provider's own sign-in page (see [First run](/docs/getting-started/first-run)).

- **What it costs you:** your plan's fixed monthly price, paid to the provider. AI Shuffle adds nothing.
- **Limits:** your plan's usage limits apply, exactly as in the provider's own apps. Providers size these plans for one person's ordinary, hands-on use. When you reach a limit, agents pause until it resets.
- **Good for:** interactive work while you are at the keyboard: chatting with an agent, reviewing its changes, a few sessions at a time.
- **Not good for:** anything that runs without you. Unattended loops on a subscription can use up your plan's limits quickly, and running them is at your own risk under the provider's terms.

Your subscription is for you. Do not share one account between several people or let other people's work run on it; that is against the providers' terms, and AI Shuffle is not built for it.

### API key

You paste an API key from your own account in the provider's console. AI Shuffle sends it only to that provider, or passes it to the agent processes it starts, and to nothing else.

- **What it costs you:** the provider bills your account per use, by the number of tokens each request reads and writes, at the prices on its pricing page. There is usually no monthly minimum.
- **Limits:** your account's rate limits and the spend limit you set there.
- **Good for:** scheduled automations, overnight runs, connectors that answer messages while you are away, and many agents at once.

## How much does an API key cost?

It depends on the model you pick and how much the agent reads. A rough way to estimate a session:

- An agent working in a code repository mostly **reads**: files, command output, its own earlier turns. Input tokens are often ten or more times the output tokens.
- Repeated context is billed at a much lower **cached** rate, so a long session usually costs far less than its raw token count suggests.
- To estimate: take a typical session's input and output tokens (your provider's usage page lists them), multiply each by the model's price per million tokens, and add them.

Start with a low monthly spend limit in your provider's console, run your normal workload for a few days, and look at its usage page. Then set the limit you are comfortable with. AI Shuffle cannot see or change your provider's bill.

## When usage numbers are estimates

The Stats page and the cost shown under each reply are added up by AI Shuffle from the token counts your provider reports for each turn. When a provider reports no counts for a call (some local and OpenAI-compatible servers do not), or when you stop a reply before its counts arrive, AI Shuffle counts the text itself at about four characters per token. Such turns are marked: **≈** next to the cost under the reply, and **est.** on the Stats tiles and rows that include them, with the reason on hover. Estimated amounts still count toward budgets and limits; only their precision is in question. Claude Code sessions always report their own counts and are never estimated. Local models never get a dollar price, even when a local model's name matches a hosted one; their cost is shown as 0.

## When to use an API key

Use an API key for anything that runs when you are not watching:

- **Automations** on a schedule, an interval, a webhook or a connector event;
- **Scheduled wake-ups** and background agents that keep working after you close the tab;
- **Connectors** that answer Telegram, Discord, Slack or other messages;
- **Many agents at once**, for example a batch of parallel tasks.

If you run much of this kind of work, an API key is the safer choice. The setup wizard gives the same advice.

## Claude: subscription or API key mode

Claude runs in exactly one mode at a time, the one you chose: **Subscription**, **API key**, **Cloud** or **Off**. Change it at any time in **Settings → Providers → Claude**, which also shows the account the Claude Code CLI is signed in to and the last four characters of your key. If both a sign-in and a key are set up, the mode you picked decides; AI Shuffle never switches on its own.

## Claude through a cloud provider or gateway

The Claude Code CLI can also run Claude through Amazon Bedrock, Google Vertex AI, Microsoft Foundry, or an LLM gateway that you run. In AI Shuffle this is a third Claude method, **Cloud provider or gateway**. It is off by default, and nothing turns it on except you.

- **Off unless you choose it.** AI Shuffle never picks up cloud credentials or a gateway address on its own, whether from the worker's environment, a profile, a project or a plugin. With the Subscription or API key mode, these variables are removed before the CLI starts, as before.
- **Owner only.** Only the owner of the worker can view, set or remove it.
- **One method at a time.** You choose one, enter its values in **Settings → Providers → Claude → Add → Cloud provider or gateway**, and AI Shuffle passes the CLI only that method's variables. Your Claude subscription sign-in and Anthropic API key are not passed in this mode, and AI Shuffle never falls back to them.
- **Secrets stay on the worker.** Keys, tokens and the service account file are stored encrypted, the same way as the Anthropic API key. The app never shows them again; a stored secret shows as "Stored", and you can replace it but not read it. The region, project, resource name and base URL are shown.
- **This worker only.** Like the Claude mode itself, the method and its secrets apply to the worker you set them on. They are not copied to your other paired workers; set each worker up on its own.
- **Turning it on.** **Save and use for Claude** turns it on. **Save** only stores the values. Choosing **Cloud** under **Runs on** opens this page; it does not switch on its own.
- **Turning it off.** Pick **Subscription** or **API key** under **Runs on** to go back to exactly what you had before. **Remove this credential** deletes the stored values and secrets. If Cloud is still the mode, Claude then waits until you set it up again or choose another mode.

Before you can save, you must confirm that you have read this warning. It stays on the page while the method is set up:

> Agents on this worker can run shell commands and can read these credentials, including the Vertex AI key file on this worker's disk, with their own tools and in terminals. A project's or your own .claude/settings.json can still change where Claude connects, and this worker's proxy and certificate settings still apply. Use an account with the smallest permissions that work, and only on a worker you trust.

For a gateway, also:

> All Claude traffic from this worker goes to the address you enter. The browser tool also receives this address and token.

The warning is accurate:

- The Claude Code CLI passes its environment to the commands its agents run, to hooks and to MCP servers, so an agent's Bash tool can read these values.
- For Vertex AI, the service account key is written to a file in the worker's data folder that only the app's user can read. Agents' Bash and Read tools and the in-app terminals run as that same user, so they can read it too. Only a key of type `service_account` is accepted.
- The CLI still reads `.claude/settings.json` in a project and in your home folder. An `env` block there can point Claude at another address.
- With a gateway, the browser tool receives the gateway address and token, the same way it receives Claude credentials in the other modes.
- The worker's own proxy and certificate settings (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`) still apply, since a company network may need them. `NODE_TLS_REJECT_UNAUTHORIZED` is always removed in this mode, so certificate checks stay on.

Give the account only the permissions it needs to call Claude.

In this mode every variable starting with `AWS_`, `GOOGLE_`, `AZURE_`, `ANTHROPIC_`, `CLOUD_ML_`, `CLAUDE_CODE_USE_` or `CLAUDE_CODE_SKIP_`, plus `CLAUDE_CODE_OAUTH_TOKEN`, is removed from what the CLI inherits from the worker and the profile, then only the chosen method's stored values are set. That includes model overrides such as `ANTHROPIC_MODEL` set on the worker or in a profile. If the stored values cannot be prepared (for example the key file cannot be written), the run stops with an error; it never continues on the worker's own credentials.

| Method | Values you enter | What the CLI gets | Setup guide |
|---|---|---|---|
| Amazon Bedrock | Region, and either access keys (an optional session token) or a Bedrock API key | `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, then `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`, or `AWS_BEARER_TOKEN_BEDROCK` | [Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock) |
| Google Vertex AI | Region, project ID, and the service account key file pasted as JSON | `CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS` (a file only the app's user can read) | [Google Vertex AI](https://code.claude.com/docs/en/google-vertex-ai) |
| Microsoft Foundry | Resource name or base URL, and an API key | `CLAUDE_CODE_USE_FOUNDRY=1`, `ANTHROPIC_FOUNDRY_RESOURCE` or `ANTHROPIC_FOUNDRY_BASE_URL`, `ANTHROPIC_FOUNDRY_API_KEY` | [Microsoft Foundry](https://code.claude.com/docs/en/microsoft-foundry) |
| LLM gateway | Base URL and auth token | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | [LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect) |

A base URL must start with `https://`. Plain `http://` is accepted only for an address on the worker itself, such as `http://127.0.0.1:4000`. It cannot contain a user name, password, query or fragment.

Usage on this method is billed by your cloud provider or gateway, not by a Claude plan, so the loop guard does not apply. The status in Settings checks only that the values are present; it does not call your cloud account. A wrong key shows up as an error on the first run.

Limits: other ways the CLI can sign in to these clouds (AWS profiles and SSO, Google's default login, Microsoft Entra ID) are not offered here. This method has not been tested against a real Amazon Bedrock, Google Vertex AI or Microsoft Foundry account.

## The loop guard

With a Claude subscription, AI Shuffle protects your plan from runaway automation:

- Turns that start without you (automations, wake-ups, connectors, and the background agents they start) count against an **unattended budget**: by default 12 per session and 60 per worker in any rolling hour. Past either budget, the next such turn does not start on its own. The session shows why, and the turn waits under **Settings → Providers → Claude subscription → Policy** until you choose **Confirm** (it runs now) or **Dismiss**.
- Background agents on Claude that an automation, wake-up or connector started get a default **time limit** of 60 minutes, so one that loops cannot run all night.
- Creating an automation or arming a wake-up on a Claude subscription shows a reminder to use an API key for scheduled runs.

You can change the budgets and the time limit, or turn the guard off, under **Settings → Providers → Claude subscription → Policy**. A change also applies to work that is already running: a lowered time limit stops an agent already past it within about a minute, and a raised one lets it keep going. The counts start again when the worker restarts. The loop guard does not apply in Claude's API key mode, where your Anthropic Console spend limit is the control, or to agents on other providers. It never holds or times work you started yourself: a chat you are driving, and every agent that chat starts, has no loop guard budget or time limit.

## What AI Shuffle costs

The license covers the app and its updates for one account with up to 3 workers, and the Android app. Every license starts with a 14-day trial with every feature, and your first charge can be refunded within 7 days. Current prices are on [hobbycoders.com](https://hobbycoders.com/ai-shuffle). See [License](/docs/getting-started/license).
