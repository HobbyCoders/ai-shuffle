<!--
title: FAQ
description: Your own AI accounts, what HobbyCoders sees, requirements, licensing, trials, refunds and support
order: 13
-->

# Frequently asked questions

You bring your own AI account. HobbyCoders sells the workspace, not the AI. These answers match the [FAQ on hobbycoders.com](https://hobbycoders.com/ai-shuffle/faq).

## Do I need my own AI account?

Yes. AI Shuffle doesn't include any AI usage. You connect your own account with Claude, OpenAI, OpenRouter, xAI Grok or a custom gateway, by subscription sign-in or API key, and that provider bills you directly.

You need one provider to start. You can add more later in **Settings → Providers**, and each one bills you under its own terms.

## Does AI Shuffle use or resell my AI subscription?

No. You sign in to your subscription yourself, on the provider's own sign-in page. Your sign-in is stored encrypted on your own workers, and HobbyCoders cannot read it. HobbyCoders never routes other people's work through your account, and doesn't resell or mark up AI usage.

## Subscription or API key: which should I pick?

Use your subscription for normal, hands-on work at your desk. Use an API key for anything that runs on its own: scheduled jobs, overnight runs, or many agents at once. Subscription plans are sized for one person working normally, and heavy automation can hit their limits. An API key is billed per use, so you see exactly what automation costs. See [Modes and costs](/docs/getting-started/modes-and-costs).

## Where do my prompts and files go?

Your projects, sessions and database stay on the machine running AI Shuffle. Prompts go from that machine straight to the AI provider you chose. They never pass through HobbyCoders' servers.

If you use remote access from your phone or another browser, that traffic travels through HobbyCoders' secure connection. [Remote access](/docs/getting-started/remote-access) and the [privacy policy](https://hobbycoders.com/privacy#remote-access) explain what that means.

## What does AI Shuffle's own server see?

Very little, and never your prompts. Your install checks for updates and, once licensing is live, checks its license. Activation sends your license key, an install ID, a machine ID, the machine name and the app version. The daily check sends only the install ID, the machine ID and the app version. If you link a phone or use remote access, the connection service keeps your account and device records. AI Shuffle has no analytics or tracking. The [privacy policy](https://hobbycoders.com/privacy) lists every address the app contacts and why.

## What do I need to run it?

Any of these:

- **Docker** on a Linux computer, NAS or home server (64-bit Intel or AMD).
- **Windows 10 or 11**, with the desktop app (in beta).
- **Linux** (64-bit Intel or AMD), with the desktop app (in beta).
- **macOS desktop app**: coming soon.

The **Android app** is optional, for using your workspace away from your desk. An iPhone and iPad app is coming soon; use a browser for now. See [Requirements](/docs/getting-started/requirements).

## How many machines does a license cover?

One license covers one account with up to 3 workers, the machines that run AI Shuffle. The Android app on your phones is included. See [License](/docs/getting-started/license) and the [license terms](https://hobbycoders.com/license).

## How much does AI Shuffle cost?

A monthly or yearly subscription, after a 14-day trial. Beta users get a founder price, locked for as long as their subscription stays active. Current prices are on [hobbycoders.com](https://hobbycoders.com/ai-shuffle).

## Is there a free trial?

Yes. Every license starts with a 14-day trial with every feature. There is no permanent free tier.

## Does it work offline?

Yes. The app keeps working for up to 14 days between license checks, so a short outage on our side or yours never stops you. Your AI provider still needs internet access.

## What happens if I cancel?

Your license stays active until the end of the period you paid for. After that, the app stops starting new agent runs. Your projects, sessions and database stay on your machine, untouched. At any time, **Settings → System → Export all data** downloads every chat (as a private backup `.jsonl` and as Markdown), every agent and your project list in one ZIP. To keep everything else, including project files, back up your install; [Back up](/docs/getting-started/install-docker-compose#back-up) shows how for Docker. We never hold your data hostage.

## Can I get a refund?

Yes. If AI Shuffle isn't for you, email support@hobbycoders.com within 7 days of your first charge and we'll refund it in full. See the [refund policy](https://hobbycoders.com/refund).

## Can I buy it from anywhere?

Not quite. At launch we can't sell to buyers in the European Union or the United Kingdom. We plan to add them later; join the waitlist on hobbycoders.com and we'll tell you when.

## Is there a team plan?

Not yet. A license is for one person's account. Each person uses their own AI accounts; sharing one AI subscription between several people is against the AI providers' terms, and AI Shuffle is not built for it.

## Is AI Shuffle made by an AI provider?

No. AI Shuffle is an independent product by HobbyCoders. It is not affiliated with Anthropic, OpenAI or xAI. None of them build, endorse or support AI Shuffle.

## Where do I get help?

Start with [Troubleshooting](/docs/getting-started/troubleshooting). Then email support@hobbycoders.com, or send a report from **Report a problem** in Settings. Don't send passwords, API keys or sign-in codes.
