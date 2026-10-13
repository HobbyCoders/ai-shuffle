<!--
title: Modes and Costs
description: Subscription or API key, what each costs you, and when to use an API key
order: 7
-->

# Modes and costs

AI Shuffle includes no AI usage. You pay HobbyCoders for the app, and you pay your AI provider for the AI. The two bills never mix: AI Shuffle never pays for, resells or marks up AI usage, and never uses anyone else's account for your work.

## Providers

Connect one or several. Each uses your own account and bills you under its own terms.

- **Claude:** a Claude subscription (Pro or Max) through the Claude Code CLI, an Anthropic API key, or your own cloud provider or gateway.
- **OpenAI:** a ChatGPT subscription sign-in, or an OpenAI API key.
- **OpenRouter:** one API key for many models.
- **xAI Grok:** a SuperGrok subscription sign-in, or an xAI API key.
- **Custom gateway:** any compatible endpoint with your own token, including a local model server.
- **fal.ai:** a key for images, video and audio. It does not run agents on its own.

A ChatGPT subscription can also make images for every chat. Under **Settings → Models**, choose **ChatGPT subscription (OpenAI built-in)** for Text to Image or Image Editing.

Voice calls use your own OpenAI API account.

## Subscription or API key

Agents run on exactly the account you chose. If it stops working, agents wait. AI Shuffle never switches to another account on its own.

### Subscription

You sign in to your own plan on the provider's own sign-in page.

- **Cost:** your plan's monthly price, paid to the provider. AI Shuffle adds nothing.
- **Limits:** your plan's usage limits, as in the provider's own apps. When you reach one, agents pause until it resets.
- **Good for:** hands-on work at your desk, a few chats at a time.
- **Not good for:** anything that runs without you. Unattended runs can use up your limits quickly, and running them is at your own risk under the provider's terms.

A subscription is for one person. Do not share one account between several people.

### API key

You paste a key from your provider's console.

- **Cost:** the provider bills you per use, by the tokens each request reads and writes.
- **Limits:** your account's rate limits and the spend limit you set there.
- **Good for:** automations, overnight runs, connectors that answer while you are away, and many agents at once.

## How much does an API key cost?

It depends on the model and on how much the agent reads.

- An agent working in a code project mostly **reads**: files, command output and its own earlier turns.
- Repeated context is billed at a much lower **cached** rate, so long chats cost less than their raw size suggests.
- Your provider's usage page shows your real numbers.

Start with a low monthly spend limit in your provider's console, work normally for a few days, check the usage page, and then set a limit you are comfortable with.

## When usage numbers are estimates

The Stats page and the cost under each reply come from the token counts your provider reports. When a provider reports none, or you stop a reply early, AI Shuffle estimates them. Estimated amounts are marked **≈** under the reply and **est.** on the Stats page. They still count toward budgets and limits. Local models are always shown as free.

## When to use an API key

Use an API key for anything that runs while you are not watching:

- **automations** on a schedule, an interval, a webhook or a connector event;
- **scheduled wake-ups** and background agents that keep going after you close the tab;
- **connectors** that answer Telegram, Discord, Slack or other messages;
- **many agents at once**.

## Claude: choosing a mode

Claude runs in one mode at a time: **Subscription**, **API key**, **Cloud** or **Off**. Change it in **Settings → Providers → Claude**, which also shows the signed-in account. AI Shuffle never switches modes on its own.

## Claude through a cloud provider or gateway

Claude can also run through Amazon Bedrock, Google Vertex AI, Microsoft Foundry, or an LLM gateway you run. Set it up in **Settings → Providers → Claude → Add → Cloud provider or gateway** (owner only), then choose **Save and use for Claude**. Your cloud provider or gateway bills this usage.

| Method | What you enter | Setup guide |
|---|---|---|
| Amazon Bedrock | Region, and access keys or a Bedrock API key | [Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock) |
| Google Vertex AI | Region, project ID and the service account key file | [Google Vertex AI](https://code.claude.com/docs/en/google-vertex-ai) |
| Microsoft Foundry | Resource name or base URL, and an API key | [Microsoft Foundry](https://code.claude.com/docs/en/microsoft-foundry) |
| LLM gateway | Base URL and auth token | [LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect) |

- The values apply to this worker only. Set up each worker on its own.
- Secrets are encrypted at rest and never shown again.
- You can also use the Claude Code CLI's standard environment variables for these methods on the worker. Settings then shows **(worker environment)**.
- Agents on this worker can read these credentials. Use an account with the smallest permissions that work, and only on a worker you trust. You confirm this before saving.

## The loop guard

With a Claude subscription, AI Shuffle protects your plan from runaway automation:

- Turns that start without you (automations, wake-ups, connectors and the background agents they start) have an hourly budget: by default 12 per chat and 60 per worker. Past it, the next one waits for you under **Settings → Providers → Claude subscription → Policy**, where you choose **Confirm** or **Dismiss**.
- Background agents started that way stop after 60 minutes by default.
- Creating an automation on a Claude subscription reminds you to use an API key.

Change the budgets and time limit, or turn the guard off, on the same **Policy** page. Work you start yourself is never held or timed.

## What AI Shuffle costs

The app and its updates are free. An optional free Cloud account serves 1 remote worker with no card. The paid Cloud plan serves 3 remote workers and adds cross-network sync, connector endpoints, OAuth relay and support. Founder pricing is $14/month or $140/year; regular pricing is $20/month or $200/year. Your first charge can be refunded within 7 days. See [AI Shuffle Cloud](/docs/getting-started/cloud).
