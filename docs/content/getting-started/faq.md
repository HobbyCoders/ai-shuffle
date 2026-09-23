<!--
title: FAQ
description: Your own AI accounts, what HobbyCoders sees, requirements, licensing, trials, refunds and support
order: 13
-->

# Frequently asked questions

You bring your own AI account. HobbyCoders sells the workspace, not the AI. These answers match the [FAQ on hobbycoders.com](https://hobbycoders.com/ai-shuffle/faq).

## Do I need my own Claude account?

Yes. AI Shuffle doesn't include any AI usage. To use Claude Code in AI Shuffle you need your own Claude subscription or an Anthropic API key. You sign in or add your key yourself, and Anthropic bills you directly.

AI Shuffle can also use other AI providers you already have an account with, such as an OpenAI or xAI account or an OpenRouter key. Each one bills you under its own terms.

## Does AI Shuffle use or resell my Claude subscription?

No. When you use a subscription, AI Shuffle runs the standard Claude Code program, and you sign in to it yourself through Anthropic's own sign-in page. HobbyCoders doesn't copy, store or pass on your sign-in, never routes other people's work through your account, and doesn't resell or mark up AI usage.

## Subscription or API key: which should I pick?

Use your subscription for normal, hands-on work at your desk. Use an API key for anything that runs on its own: scheduled jobs, overnight runs, or many agents at once. Subscription plans are sized for one person working normally, and heavy automation can hit their limits. An API key is billed per use, so you see exactly what automation costs. See [Modes and costs](/docs/getting-started/modes-and-costs).

## Where do my prompts and files go?

Your projects, sessions and database stay on the machine running AI Shuffle. Prompts go from that machine straight to the AI provider you chose. They never pass through HobbyCoders' servers.

If you use remote access from your phone or another browser, that traffic travels through a Cloudflare tunnel for your worker. [Remote access](/docs/getting-started/remote-access) and the [privacy policy](https://hobbycoders.com/privacy#remote-access) explain what that means.

## What does AI Shuffle's own server see?

Very little, and never your prompts. Your install checks for updates and, once licensing is live, checks its license: that sends the license key, a random install ID and the app version. If you link a phone or use remote access, the connection service keeps your account and device records. AI Shuffle has no analytics or tracking. The [privacy policy](https://hobbycoders.com/privacy) lists every address the app contacts and why.

## What do I need to run it?

A Linux server or NAS with Docker (64-bit x86), an Unraid server, or a Windows 10 or 11 PC. macOS and Linux desktop builds are coming. An Android phone is optional, for using your workspace away from your desk. See [Requirements](/docs/getting-started/requirements).

## How many machines does a license cover?

One license covers one account with up to 3 workers, the machines that run AI Shuffle. The Android app on your phones is included. See [License](/docs/getting-started/license) and the [license terms](https://hobbycoders.com/license).

## How much does AI Shuffle cost?

A monthly or yearly subscription, after a 14-day trial. Beta users get a founder price, locked for as long as their subscription stays active. Current prices are on [hobbycoders.com](https://hobbycoders.com/ai-shuffle).

## Is there a free trial?

Yes. Every license starts with a 14-day trial with every feature. There is no permanent free tier.

## Does it work offline?

Yes. The app keeps working for up to 14 days between license checks, so a short outage on our side or yours never stops you. Your AI provider still needs internet access.

## What happens if I cancel?

Your license stays active until the end of the period you paid for. After that, the app stops starting new agent runs. Your projects, sessions and database are untouched, and you can export them at any time. We never hold your data hostage.

## Can I get a refund?

Yes. If AI Shuffle isn't for you, email support@hobbycoders.com within 7 days of your first charge and we'll refund it in full. See the [refund policy](https://hobbycoders.com/refund).

## Can I buy it from anywhere?

Not quite. At launch we can't sell to buyers in the European Union or the United Kingdom. We plan to add them later; join the waitlist on hobbycoders.com and we'll tell you when.

## Is there a team plan?

Not yet. A license is for one person's account. Each person uses their own AI accounts; sharing one AI subscription between several people is against the AI providers' terms, and AI Shuffle is not built for it.

## Is AI Shuffle made by Anthropic?

No. AI Shuffle is an independent product by HobbyCoders. It runs Claude Code, which is made by Anthropic, but Anthropic doesn't build, endorse or support AI Shuffle.

## Where do I get help?

Start with [Troubleshooting](/docs/getting-started/troubleshooting). Then email support@hobbycoders.com. Once the beta opens, you can also ask in the #install-help channel on [our Discord](https://hobbycoders.com/discord). Don't send passwords, API keys or sign-in codes in either.
