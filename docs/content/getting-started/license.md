<!--
title: License
description: Activating a license, seats, offline use, what happens when a license ends, and refunds
order: 11
-->

# License

AI Shuffle is sold as a subscription license. The license pays for the app and its updates. It never includes AI usage, which your AI provider bills you for directly.

## During the beta

While the beta runs, **Settings → System → License** reads **Beta: no license needed** and every feature works. When paid licenses start, you add your key there.

## What a license covers

- **One account, up to 3 workers.** A worker is a machine running AI Shuffle: a Docker install or the desktop app. Each activated worker takes one seat.
- **The Android app** on your own phones, at no extra seat.
- **Every feature** and every update while the license is active.
- **One person.** Each person who uses AI Shuffle needs their own license and their own AI accounts.

Every license starts with a **14-day trial** with every feature. There is no permanent free tier.

## Your license email

When you buy or start a trial, you get:

- a **license key**, like `ASH-XXXXX-XXXXX-XXXXX-XXXXX`, which you enter in the app;
- a **pull token**, which Docker uses to download the image (see [Install with Docker](/docs/getting-started/install-docker-compose)).

Keep the license key private. If you share your pull token by accident, get a new one from your account page.

## Activate a worker

1. Open **Settings → System → License**.
2. Paste your license key and activate.
3. The card shows your plan, how many of your 3 seats are in use, and when the license next renews.

You can also give the key at install time: in the desktop app's first-run window, or as `LICENSE_KEY` with Docker. If it is not accepted, **Settings → System → License** says why.

Activation sends your license key, an install ID, a machine ID, the machine name and the app version. It never sends prompts, files or AI provider credentials.

### All 3 seats are taken

Activation says "This license is in use on 3 of 3 workers" and lists the workers holding seats. Free one, then activate again:

- Choose **Deactivate** next to the old worker in that list, or remove it from your account page on hobbycoders.com.
- Or, on the old worker: **Settings → System → License → Deactivate on this worker**.
- A worker that has not checked in for 21 days frees its seat by itself.

A worker whose seat was freed elsewhere stops starting new agent runs at its next license check, about once a day. Seats of other workers can be freed 3 times a day; deactivating a worker on itself is not counted.

### Reinstalling and moving to a new machine

Reinstalling the desktop app on the same computer reuses that computer's seat. A Docker container that is recreated with the same data folder keeps its seat; one started with a fresh data folder is a new worker.

To move, deactivate the old worker (or let its seat free itself), install on the new machine, and activate there with the same key. To bring your data along, restore a backup (see [Install with Docker](/docs/getting-started/install-docker-compose#back-up)). A copy of a worker's data folder does not get its own seat: if both copies run, one of them stops starting agent runs within about a day, and activating it again moves the seat away from the other.

## Offline use

- A worker can work offline for up to **14 days** between license checks. An outage on our side never stops you.
- The worker renews about once a day. **Settings → System → License → Check now** renews right away.
- If a worker cannot renew, a banner warns you **3 days** before it would stop. Connect it to the internet to renew.

## Failed payments

If a payment fails, a banner says so and everything keeps working for **14 days** while you update your payment method.

## When a license ends

When a license ends (the trial or grace period ended, you cancelled, or the worker was offline for more than 14 days), the worker is **unlicensed**:

- **Blocked:** starting new agent runs. The app says why and how to fix it.
- **Not blocked:** everything else. Your chats, projects, files, settings, exports and backups stay available. Nothing is deleted or locked.
- The version you have keeps running, and can still be downloaded again. New versions need an active license.

Add a key or renew, and agents run again at once.

## Cancelling

Cancel any time from your account on hobbycoders.com. The license stays active until the end of the period you paid for.

## Refunds

If AI Shuffle is not for you, email support@hobbycoders.com within **7 days of your first charge**, from the email address on your account, and you get that charge back in full. The license ends when it is refunded. Later charges are not refunded, except where the law requires it.

Your AI provider bills your AI usage, so it cannot be refunded here. See the [refund policy](https://hobbycoders.com/refund) and the [license terms](https://hobbycoders.com/license).

## Where you can buy

At launch, licenses cannot be sold to buyers in the European Union or the United Kingdom.
