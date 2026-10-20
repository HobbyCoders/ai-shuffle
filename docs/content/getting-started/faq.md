<!--
title: FAQ
description: Your own AI accounts, what HobbyCoders sees, requirements, Cloud plans, refunds and support
order: 13
-->

# Frequently asked questions

You bring your own AI account. The app is free; HobbyCoders sells optional Cloud services, not AI usage. These answers match the [FAQ on hobbycoders.com](https://hobbycoders.com/ai-shuffle/faq).

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

Your install checks for free updates. Linking a paid Cloud plan sends your key, an install ID, hashed machine identifiers, the machine name and the app version. Lease renewals send the install token, install ID, hashed fingerprint and app version, never prompts or AI provider credentials. If you use Cloud remote access, the service keeps your account and device records. AI Shuffle has no analytics or tracking. The [privacy policy](https://hobbycoders.com/privacy) lists the services it contacts.

## What do I need to run it?

Any of these:

- **Docker** on a Linux computer, NAS or home server (64-bit Intel or AMD).
- **Windows 10 or 11**, with the desktop app.
- **Linux** (64-bit Intel or AMD), with the desktop app.
- **macOS desktop app**: coming soon.

The **Android app** is optional, for using your workspace away from your desk. An iPhone and iPad app is coming soon; use a browser for now. See [Requirements](/docs/getting-started/requirements).

## How many machines can I use?

Run as many local workers as you need on your LAN or VPN. An optional free Cloud account serves 1 remote worker; the paid plan serves 3. See [AI Shuffle Cloud](/docs/getting-started/cloud).

## How much does AI Shuffle cost?

The app and updates are free. AI Shuffle Cloud has a free account with 1 remote worker and no card, or a paid plan with 3 remote workers. Founder pricing is $14/month or $140/year; regular pricing is $20/month or $200/year.

## Is there a free account?

Yes. One remote worker, no card and no time limit. There is no trial, and local use needs no account.

## Does it work offline?

Yes. Local use has no license checks or time limit. Your AI provider may still need internet access.

## What happens if I cancel?

Your paid Cloud plan stays active until the end of the period you paid for. After that, the hub stops serving extra tunnels; your free account keeps one remote worker. Nothing in the app stops working, and updates stay free. Your projects, sessions and database stay on your machine, untouched. At any time, **Settings → System → Export all data** downloads every chat (as a private backup `.jsonl` and as Markdown), every agent and your project list in one ZIP. To keep everything else, including project files, back up your install; [Back up](/docs/getting-started/install-docker-compose#back-up) shows how for Docker. We never hold your data hostage.

## Can I get a refund?

Yes. If AI Shuffle isn't for you, email support@hobbycoders.com within 7 days of your first charge and we'll refund it in full. See the [refund policy](https://hobbycoders.com/refund).

## Can I buy it from anywhere?

Not quite. At launch we can't sell to buyers in the European Union or the United Kingdom. We plan to add them later. The app itself is free to download and use anywhere.

## Is there a team plan?

Not yet. A Cloud account is for one person. Each person uses their own AI accounts; sharing one AI subscription between several people is against the AI providers' terms, and AI Shuffle is not built for it.

## Is AI Shuffle made by an AI provider?

No. AI Shuffle is an independent product by HobbyCoders. It is not affiliated with Anthropic, OpenAI or xAI. None of them build, endorse or support AI Shuffle.

## Where do I get help?

Start with [Troubleshooting](/docs/getting-started/troubleshooting). Then email support@hobbycoders.com, or send a report from **Report a problem** in Settings. Don't send passwords, API keys or sign-in codes.
