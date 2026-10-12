<!--
title: AI Shuffle Cloud
description: Free local use, free and paid Cloud accounts, remote workers, cancellation and refunds
order: 11
-->

# AI Shuffle Cloud

AI Shuffle is free and open source. Everything in the app works on your own hardware, on your LAN or over your own VPN, without an account or a key. Updates are free. You bring your own AI accounts; your AI provider bills any AI usage directly.

## Free account

An optional free AI Shuffle Cloud account gives you **1 remote worker**, with no card and no time limit. A worker is a machine running AI Shuffle, such as a Docker install or the desktop app. You do not need a Cloud account for local use or same-LAN sync.

## Paid plan

The paid plan includes **3 remote workers**, worker-to-worker sync across different networks, public connector endpoints, the OAuth relay for one-click plugin sign-ins, and support through in-app reports and the support inbox.

- **Founder:** $14/month or $140/year.
- **Regular:** $20/month or $200/year.

Prices are in US dollars. There is no trial: the free account lets you try remote access on one worker without a card.

## Link a paid plan

Open **Settings → Cloud** to see your account, plan, seats and lease renewal. If you have a license key (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`), enter it there to link your paid plan. Keep the key private.

You can also supply the key at install time, in the desktop app's first-run window or as `LICENSE_KEY` with Docker. It is optional. If it is not accepted, Settings explains why; local use still works.

Activation sends your key, an install ID, hashed machine identifiers, the machine name and the app version. It never sends prompts, files or AI provider credentials. The worker renews its signed Cloud lease about once a day; **Check now** requests a renewal immediately.

### All 3 seats are taken

Free a seat on the old worker or from your account on hobbycoders.com, then link the new worker. A worker that has not checked in for 21 days frees its seat by itself. Seats of other workers can be freed 3 times a day; deactivating a worker on itself is not counted.

Reinstalling the desktop app on the same computer reuses its seat. Recreating a Docker container with the same data folder keeps its seat. Copying a worker's data folder does not grant another paid seat. None of these seat checks stops local agent runs.

## Offline use and failed payments

Local use has no lease deadline. Your AI provider may still need internet access. A paid Cloud lease lasts 14 days, and failed payments have 14 days of grace. Settings shows renewal or payment problems without an app-wide banner. If the lease expires or a seat is freed, the hub no longer grants that paid remote capacity; your app and data keep working.

## When a plan ends

Nothing in the app stops working when a plan ends. Agents, chats, projects, files, settings, exports, backups, LAN or VPN access and free updates remain available. Your free account keeps one remote worker; the hub stops serving extra tunnels.

Cancel any time from your account on hobbycoders.com. The paid plan stays active until the end of the period you paid for.

## Refunds

If AI Shuffle is not for you, email support@hobbycoders.com within **7 days of your first charge**, from the email address on your account, and you get that charge back in full. The paid plan ends when it is refunded. Later charges are not refunded, except where the law requires it.

Your AI provider bills your AI usage, so it cannot be refunded here. See the [refund policy](https://hobbycoders.com/refund) and the [license terms](https://hobbycoders.com/license).

## Where you can buy

At launch, paid plans cannot be sold to buyers in the European Union or the United Kingdom.
