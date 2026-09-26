<!--
title: License
description: Activating a license, seats, offline use and grace, what happens when a license ends, and refunds
order: 11
-->

# License

AI Shuffle is sold as a subscription license. The license pays for the app and its updates; it never includes AI usage, which your AI provider bills you for directly.

## During the beta

While the beta runs, **Settings → License** reads **Beta: no license needed** and every feature works. When paid licenses start, you add your key there. Nothing else changes.

## What a license covers

- **One account, up to 3 workers.** A worker is a machine running AI Shuffle: a Docker host, an Unraid server or the desktop app. Each activated worker takes one seat.
- **The Android app** on your own phones, at no extra seat.
- **Every feature** and every update while the license is active.
- **One person.** A license is for one person's account. Each person who uses AI Shuffle needs their own license and their own AI accounts.

Every license starts with a **14-day trial** with every feature. There is no permanent free tier.

## Your license email

When you buy or start a trial, you get:

- a **license key**, like `ASH-XXXXX-XXXXX-XXXXX-XXXXX`, which you enter in the app;
- a **pull token**, which Docker uses to download the image (see [Install on Unraid](/docs/getting-started/install-unraid) or [Install with Docker Compose](/docs/getting-started/install-docker-compose)).

Keep the license key private. The pull token is less sensitive (it only downloads the image), but rotate it from your account page if you post it somewhere by accident.

## Activate a worker

1. Open **Settings → License**.
2. Paste your license key and activate.
3. The card shows the plan, how many of your 3 seats are in use, and the date the current lease runs until.

### Key given at install

You can hand AI Shuffle the key before its first start, and it activates on its own:

- **Desktop app:** when the first download needs a license, the first-run launcher asks for the key in its window. The worker activates that key when it first starts, and the app deletes its saved copy once the license is in use.
- **Docker Compose:** `LICENSE_KEY=` in `.env`.
- **Unraid:** the **License key** field in the template.

Each key is tried once. If it is not accepted (a typo, or all 3 seats taken), **Settings → License** says why; activate there instead. After the first start, `LICENSE_KEY` and the template field are ignored and you can clear them.

When you activate, the worker sends the license key, a random install ID, a fingerprint made from that ID and the machine ID, the host name (as the worker's display name) and the app version. It never sends prompts, files, model traffic or AI provider credentials.

### All 3 seats are taken

Activation then lists the workers holding seats. Free the seat of a worker you no longer use, then activate again:

- In that list, choose **Deactivate** next to the old worker.
- Or, on the old worker if it still runs: **Settings → License → Deactivate on this worker**.
- A worker that has not checked in for 21 days gives up its seat on its own.

### Moving to a new machine

Deactivate the old worker (or let its seat free itself), install on the new machine, and activate there with the same key. Your data moves with a backup of the data folder, not with the license.

## Offline use and renewals

- The license server gives each worker a signed **lease** that is good for **14 days**. The worker checks it offline, so a worker can be offline for two weeks without any effect, and an outage of the license server never stops you.
- The worker renews its lease about once a day. **Settings → License → Check now** renews immediately.
- If a worker cannot renew, a banner warns you **3 days** before its lease ends. Connect it to the internet to renew.

## Failed payments

If a payment fails, a banner says so and everything keeps working for **14 days** while you update your payment method. A later successful payment clears it.

## What "unlicensed" blocks

When a worker's lease ends without renewal (the trial ended, the grace period ended, the subscription was cancelled, or the worker was offline for more than 14 days), the worker is **unlicensed**. Then:

- **Blocked:** starting new agent runs. The app says why and how to fix it.
- **Not blocked:** everything else. Your sessions, projects, files, settings, exports and backups stay readable and writable. Nothing is deleted or locked. Your data is never held hostage.
- The container keeps running, and the exact version you have can still be downloaded again. New versions need an active license.

Add a key or renew, and agents run again at once.

## Cancelling

Cancel at any time from your account on hobbycoders.com. The license stays active until the end of the period you paid for. After that, workers become unlicensed as described above.

## Refunds

If AI Shuffle is not for you, email support@hobbycoders.com within **7 days of your first charge**, from the email address on your account, and you get that charge back in full. The license ends when it is refunded. Later charges and renewals are not refunded, except where the law requires it.

Your AI provider bills your AI usage, not HobbyCoders, so it cannot be refunded here. See the [refund policy](https://hobbycoders.com/refund) and the [license terms](https://hobbycoders.com/license).

## Where you can buy

At launch, licenses cannot be sold to buyers in the European Union or the United Kingdom.
