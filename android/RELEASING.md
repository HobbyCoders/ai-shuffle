# Releasing the Android app

The owner's checklist for moving AI Shuffle for Android from Play internal
testing to a public listing. Build and signing mechanics are in
[README.md](README.md); this file covers what Play Console asks for, and
how the release APK and the Play link reach the website's downloads page.

## Builds and secrets (unchanged)

`release.yml` and `android-play.yml` build the signed App Bundle and send it to
the **internal** track only. They use these existing GitHub secrets and
variables; nothing new is needed for this checklist.

| Name | Kind | Purpose |
| --- | --- | --- |
| `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD` | secrets | upload key for the App Bundle |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | secret in the `google-play-internal` environment | Play publishing service account |
| `PLAY_SIGNING_READY`, `ANDROID_WORKERS_READY` | variables | gates described in README.md |

Promote a tested internal build to closed, open or production testing from
Play Console (Test and release > the track > Promote release). The workflows
never publish to production.

## APK on the website

The downloads page (`hobbycoders.com/ai-shuffle/downloads`) links a sideload
APK once the account hub lists one: the `android` entry of
`GET /v1/desktop/installers`, served at `/v1/desktop/installer/android`
(`android/hub/README.md`, "Installers per platform"). It is published per
release tag, after `release.yml` has created the tag's GitHub release:

1. Deploy the hub with migration 0024 (`deploy-hub.yml`) if it is not live yet.
2. In Actions, run **Publish a platform installer** (`platform-installer.yml`)
   with **Use workflow from** set to the tag (`vX.Y.Z`), platform `android`,
   and the build run ID left empty.
3. The workflow calls Android CI (`android.yml`) with the tag as
   `release_tag`. That builds the release APK with `versionName` set to the
   tag, checks it is signed with the release key, runs the unit, lint, hub
   and emulator checks, and attaches `AI-Shuffle-Android.apk`, its `.sha256`
   file and `android-build.json` to the release. It needs the same keystore
   secrets as the Play build (`KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`,
   `KEY_ALIAS`, `KEY_PASSWORD`).
4. The publish job then runs `desktop/scripts/publish-platform-installer.mjs
   android`. It checks the APK against its `.sha256` file and
   `android-build.json`, and refuses the APK if its `versionName` is not the
   tag, if it was built from another commit, if the release asset's bytes
   differ, or if a newer APK is already listed. Then it signs the entry with
   the application update key and writes the hub row. It runs in the
   `release-signing` environment (`DESKTOP_APPLICATION_SIGNING_KEY`,
   `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `ACCOUNT_HUB_DATABASE_ID`).

If an Android CI run with that `release_tag` finished in the last day, pass
its run ID instead to publish its `android-download` artifact without
building again. A plain Android CI run from main is not accepted: its
`versionName` is the Gradle default, not the tag.

Check the result at `https://hobbycoders.com/v1/desktop/installers`
(`platforms.android`). The link on the downloads page appears only while the
hub's `PUBLIC_APP_DOWNLOADS` is `enabled`; until then the page says the APK
download is paused. The page caches the hub's answer for a minute.

The rolling `android-latest` release (Android CI dispatched on main with
`publish_download`) is separate and never reaches the hub.

### Google Play link on the downloads page

Once the Play listing is public, set the repository variable
`SITE_PLAY_LISTING_URL` (Settings > Secrets and variables > Actions >
Variables) to `https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle`
and run **Deploy website** (`deploy-site.yml`). The site accepts only a
`https://play.google.com/store/apps/details?id=` address; anything else is
ignored and the page keeps saying the listing opens with the public release.
Leaving the variable unset keeps whatever the site Worker already has.

## Account deletion (required before a public listing)

Play requires apps that let people create an account to let them delete it
both **inside the app** and from a **web page** that works without the app.

- **In the app:** Settings > Delete account, or Settings > the account >
  Delete account. The screen says what is deleted and what stays, then opens
  the hub's deletion page in a Custom Tab. The hub asks for a fresh sign-in on
  its own origin and for `DELETE` to be typed before anything is deleted. When
  the user comes back, the app checks with the hub, and once the hub no longer
  accepts the phone it removes the account from the phone. Signed-out users
  get the web page from the same Settings link. Code:
  `app/src/main/java/com/hobbycoders/aishuffle/account/AccountDeletion.kt` and
  `ui/AccountActivity.kt`.
- **On the web:** <https://hobbycoders.com/delete-account>. It is served by
  the account hub (`android/hub/src/account-deletion.ts`, `deletion-page.ts`)
  and must keep working at this URL when the marketing site moves to its own
  Worker (master plan W1 keeps `/delete-account` on the hub).

What deletion removes, as the page and the in-app screen say: the profile and
sign-in identity (the hosted sign-in provider's user is deleted too), trusted
devices, pairing codes, worker registrations, hosted network membership,
recovery information, sync records, and the remote-access tunnels and DNS
records for the account's workers. What stays: anything on the user's own
workers and devices, and anything their AI providers hold. A hashed deletion
receipt is kept for 24 hours to stop old sign-in tokens being replayed;
rate-limit records expire on their own.

In Play Console, App content > **Data safety** > Data deletion:

- "Do you provide a way for users to request that their data is deleted?" Yes.
- **Delete account URL:** `https://hobbycoders.com/delete-account`
- Users can also delete some data without deleting the account: say No unless
  per-device revocation (Account > Trusted devices > Revoke) should count; it
  removes that device's access, not stored data.

Before submitting, open the URL in a private browser window on a desktop and
check that it loads, signs in, and shows the steps without the app installed.
The page's support link is `support@hobbycoders.com` (report 03, SEC-20);
check that address receives mail (Email Routing, `android/site/README.md`)
before submitting. Tests in `android/hub` and `android/site` fail if a
personal Gmail address reaches their files or responses.

## Data safety form (draft answers for the owner to confirm)

This is a starting point from reading the code on 2026-09-23, not legal
advice. Re-check it against the privacy policy rewrite (website item W8).

Data is encrypted in transit: **Yes** (HTTPS to the hub; HTTPS or the managed
tunnel to workers). Independent security review: No.

| Data type | Collected | Shared | Why, and notes |
| --- | --- | --- | --- |
| Personal info > Name / User IDs | Yes, required for an account (the app works without one) | No | Account management. The hub stores a username and the sign-in provider's subject ID; the hosted sign-in provider (Clerk) processes the email and name on HobbyCoders' behalf. |
| Personal info > Email address | Yes, by the sign-in provider | No | Account management; not stored in the hub database. |
| Device or other IDs | Yes | No | Account management and security: a random install ID and the device model name identify each trusted device. |
| Messages / other user-generated content | Yes, optional | No | App functionality. Prompts, replies and files go to the user's own worker. Over remote access they pass through the Cloudflare tunnel on HobbyCoders' account in transit only; nothing is stored by HobbyCoders. Mark as processed ephemerally. |
| Audio > Voice or sound recordings | Yes, optional | No, transfer is user-initiated | Voice chat streams microphone audio to the AI provider the user configured on their worker, using a short-lived session from that worker. |
| Location > Approximate / Precise | Yes, optional | No | Only when the user turns on phone tools and approves an agent's request; sent to the user's own worker. |
| App activity, web history, contacts, photos, files on the phone, financial or health data | No | No | |
| Crash logs and diagnostics | No | No | The app has no analytics or crash-reporting SDK. |

Other App content declarations this app needs:

- **Accessibility API:** the optional app-control service
  (`ShuffleAccessibilityService`) needs the Accessibility declaration and a
  short video of the consent flow.
- **Foreground service (microphone):** voice chat, declared as
  `FOREGROUND_SERVICE_MICROPHONE`.
- **Camera:** QR pairing only.
- **Privacy policy URL:** `https://hobbycoders.com/privacy`.
- **Target audience:** 18+; not designed for children.

## Store listing

The listing text lives in `play/listing/en-US/` (`title.txt`,
`short-description.txt`, `full-description.txt`); paste it into Grow users >
Store presence > Main store listing. The app name, launcher icon and listing
use only the AI Shuffle brand: no third-party AI vendor's name or logo in the
title, descriptions, icon, feature graphic or screenshots, including
screenshots of model pickers. `StoreBrandingTest` fails if one appears in the
app name, resources or listing files, and checks Play's length limits.

Checked on 2026-09-23: the app name is "AI Shuffle", the launcher icon is the
shuffle mark (`res/mipmap-*/ic_launcher_foreground.png`), and no string or
resource in the app names a vendor.

## Before promoting to production

1. Data safety form and the other declarations above submitted and approved.
2. Deletion URL checked from a browser without the app.
3. Privacy policy covers the hub, hosted sign-in and the Cloudflare tunnel
   path (website item W8).
4. Store listing, a feature graphic (1024x500) and at least two phone
   screenshots uploaded, all without vendor names or logos.
5. A closed test with real testers for as long as Play requires for new
   personal developer accounts, if the account is a personal one.
