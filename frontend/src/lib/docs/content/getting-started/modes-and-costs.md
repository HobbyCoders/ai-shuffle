<!--
title: Modes and Costs
description: Claude subscription or API key, what each costs you, and when to use an API key
order: 7
-->

# Modes and costs

AI Shuffle includes no AI usage. You pay HobbyCoders for the app, and you pay your AI provider for the AI. The two bills never mix: AI Shuffle never pays for, resells, marks up or routes AI usage, and never uses anyone else's account for your work.

## The two ways to run Claude Code

AI Shuffle runs Claude Code in exactly one mode at a time, the one you chose. It never switches modes on its own, and if the mode you chose has no credential, agents wait instead of falling back to anything.

### Subscription mode

AI Shuffle runs the standard, unmodified Claude Code CLI, which you sign in to yourself with your own Claude account through Anthropic's sign-in page (see [First run](/docs/getting-started/first-run)).

- **What it costs you:** your Claude plan's fixed monthly price (Pro or Max), paid to Anthropic. AI Shuffle adds nothing.
- **Limits:** your plan's usage limits apply, exactly as when you use Claude Code in your own terminal. Anthropic sizes these plans for one person's ordinary, hands-on use. When you reach a limit, agents pause until it resets.
- **Good for:** interactive work while you are at the keyboard: chatting with an agent, reviewing its changes, a few sessions at a time.
- **Not good for:** anything that runs without you. Unattended loops on a subscription can use up your plan's limits quickly, and running them is at your own risk under Anthropic's terms.

Your subscription is for you. Do not share one Claude account between several people or let other people's work run on it; that is against Anthropic's terms, and AI Shuffle is not built for it.

### API key mode

You paste an Anthropic API key from your own Anthropic Console account. AI Shuffle passes it to the agent processes it starts and to nothing else.

- **What it costs you:** Anthropic bills your Console account per use, by the number of tokens each request reads and writes, at the prices on Anthropic's pricing page. There is no monthly minimum.
- **Limits:** your Console account's rate limits and the spend limit you set there.
- **Good for:** scheduled automations, overnight runs, connectors that answer messages while you are away, and many agents at once.

## How much does an API key cost?

It depends on the model you pick and how much the agent reads. A rough way to estimate a session:

- An agent working in a code repository mostly **reads**: files, command output, its own earlier turns. Input tokens are often ten or more times the output tokens.
- Repeated context is billed at a much lower **cached** rate, so a long session usually costs far less than its raw token count suggests.
- To estimate: take a typical session's input and output tokens (the Anthropic Console's usage page lists them), multiply each by the model's price per million tokens, and add them.

Start with a low monthly spend limit in the Anthropic Console, run your normal workload for a few days, and look at the Console's usage page. Then set the limit you are comfortable with. AI Shuffle cannot see or change your Console bill.

## When usage numbers are estimates

The Stats page and the cost shown under each reply are added up by AI Shuffle from the token counts your provider reports for each turn. When a provider reports no counts for a call (some local and OpenAI-compatible servers do not), or when you stop a reply before its counts arrive, AI Shuffle counts the text itself at about four characters per token. Such turns are marked: **≈** next to the cost under the reply, and **est.** on the Stats tiles and rows that include them, with the reason on hover. Estimated amounts still count toward budgets and limits; only their precision is in question. Claude Code sessions always report their own counts and are never estimated. Local models never get a dollar price, even when a local model's name matches a hosted one; their cost is shown as 0.

## When to use an API key

Use an API key for anything that runs when you are not watching:

- **Automations** on a schedule, an interval, a webhook or a connector event;
- **Scheduled wake-ups** and background agents that keep working after you close the tab;
- **Connectors** that answer Telegram, Discord, Slack or other messages;
- **Many agents at once**, for example a batch of parallel tasks.

If you run much of this kind of work, API key mode is the safer choice. The setup wizard gives the same advice when you choose the mode. You can change the mode at any time in **Settings → Providers → Claude**, which also shows the account the CLI is signed in to and the last four characters of your key. If both a sign-in and a key are set up, the mode you picked decides; AI Shuffle never switches on its own.

## The loop guard

In subscription mode, AI Shuffle protects your plan from runaway automation:

- Turns that start without you (automations, wake-ups, connectors, and the background agents they start) count against an **unattended budget**: by default 12 per session and 60 per worker in any rolling hour. Past either budget, the next such turn does not start on its own. The session shows why, and the turn waits under **Settings → Providers → Claude subscription → Policy** until you choose **Confirm** (it runs now) or **Dismiss**.
- Background agents on Claude that an automation, wake-up or connector started get a default **time limit** of 60 minutes, so one that loops cannot run all night.
- Creating an automation or arming a wake-up in subscription mode shows a reminder to use an API key for scheduled runs.

You can change the budgets and the time limit, or turn the guard off, under **Settings → Providers → Claude subscription → Policy**. A change also applies to work that is already running: a lowered time limit stops an agent already past it within about a minute, and a raised one lets it keep going. The counts start again when the worker restarts. The loop guard does not apply in API key mode, where your Console spend limit is the control, or to agents on other providers. It never holds or times work you started yourself: a chat you are driving, and every agent that chat starts, has no loop guard budget or time limit.

## Other providers

You can also add OpenAI, xAI, OpenRouter, fal.ai (images, video and audio) or a local model in Settings. Each uses your own account or key and bills you under its own terms. Voice mode uses your own OpenAI account.

## What AI Shuffle costs

The license covers the app and its updates for one account with up to 3 workers, and the Android app. Every license starts with a 14-day trial with every feature, and your first charge can be refunded within 7 days. Current prices are on [hobbycoders.com](https://hobbycoders.com/ai-shuffle). See [License](/docs/getting-started/license).
