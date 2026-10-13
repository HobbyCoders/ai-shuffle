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
(`hobbycoders-cloud/hub/README.md`, "Installers per platform"). It is published per
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
  the account hub (`hobbycoders-cloud/hub/src/account-deletion.ts`, `deletion-page.ts`)
  and must keep working at this URL when the marketing site moves to its own
  Worker (master plan W1 keeps `/delete-account` on the hub).

What deletion removes, as the page and the in-app screen say: the profile and
sign-in identity (the hosted sign-in provider's user is deleted too), trusted
devices, pairing codes, worker registrations, hosted network membership,
recovery information, sync records, and the remote-access tunnels and DNS
records for the account's workers. When the license-service binding is
configured, deletion also cancels account-linked subscriptions and revokes
linked licenses (`license/src/account-erasure.ts`); this is not cancellation
of every subscription a user may hold. What stays: anything on the user's own
workers and devices, and anything their AI providers hold. Accounting/license
records and a trial-email hash also remain in the license service; Stripe has
its own retention. Their retention needs separate public disclosure and owner
review, not the 24-hour deletion-receipt limit. The hashed deletion receipt
is kept for 24 hours to stop old sign-in tokens being replayed; rate-limit
records expire on their own. Signed-in deletion and deployed service bindings
remain unverified; the signed-out page and Clerk sign-in loaded in the review.

In Play Console, App content > **Data safety** > Data deletion:

- "Do you provide a way for users to request that their data is deleted?" Yes.
- **Delete account URL:** `https://hobbycoders.com/delete-account`
- Users can also delete some data without deleting the account: say No unless
  per-device revocation (Account > Trusted devices > Revoke) should count; it
  removes that device's access, not stored data.

Before submitting, open the URL in a private browser window on a desktop and
check that it loads, signs in, and shows the steps without the app installed.
The page's support link is `support@hobbycoders.com` (report 03, SEC-20).
The owner requires a dedicated mailbox, not forwarding. It is not ready:
provider/plan selection, private mailbox creation, DNS approval/configuration
and an actual delivery/receipt check are pending. No purchase, signup or DNS
change is authorized. Tests in `hobbycoders-cloud/hub` and `hobbycoders-cloud/site` check the
support address in source/responses, not mailbox delivery.

## Data safety form (factual draft; Console answers pending)

Updated from the 2026-10-01 source-path reviews, not legal advice or a signed
Play APK/device test. Reconcile the privacy policy (website item W8) and the
actual release before the owner chooses Console answers. Submission is on
hold; this document is not authorization to publish or promote a build.

[Google's Data safety guidance](https://support.google.com/googleplay/android-developer/answer/10787469?hl=en)
(fetched 2026-10-01) includes off-device transmission, including through
app-controlled WebViews. A user-owned worker is off-device. Ephemeral means
memory-only retention for the real-time request; even qualifying ephemeral
processing belongs in the form response. Do not mark stored chats/files as
ephemeral or assume a sharing exception from worker ownership or user consent.

**Pending owner/actual-release checks:** all-data encryption in transit
(including configured worker/provider routes), sharing exceptions and provider
retention, independent security review, financial-data classifications and
target audience. Do not preselect Yes for transit, No for sharing, or 18+.

| Data type to review | Confirmed source behavior / classification still needed |
| --- | --- |
| Personal info > Name / User IDs / Email address | Account management: the hub stores a username and sign-in subject ID; Clerk processes email/name. Confirm release scope and sharing classification. |
| Device or other IDs | Install ID and device model identify trusted devices for account management/security. Sharing classification pending. |
| Messages / other user-generated content | Optional prompts, replies and available voice transcripts produce durable worker `session_events` rows. Provider processing is possible; this is not blanket ephemeral processing. PersonaPlex persists assistant text but does not supply user transcription. |
| Photos / videos / audio / files and documents | Optional explicit picker accepts `*/*` (up to 32 selections), not automatic whole-library upload. Selected files go to worker workspace/project/session staging and remain on disk; cross-worker transfers can create durable upload blobs. Confirm the actual release's categories. |
| Audio > Voice or sound recordings | GPT Live/PersonaPlex PCM routes through the paired worker to OpenAI/fal.ai. Batch dictation goes to worker STT; fal STT first uploads bytes to fal storage. Streaming review found no durable raw-PCM write in that route, not a global retention guarantee. Provider retention and temporary buffering remain unresolved. |
| Location > Approximate / Precise | Optional phone tools send approved location requests to the worker; confirm release scope, retention and sharing. |
| App activity / other screen content | Approved Accessibility reads return app/package and screen text/descriptions/references to the worker and AI. Review applicable categories; filtering sensitive screens is not proof that every sensitive screen is detected. |
| Web history / contacts / financial / health data | No blanket No: review selected content, screen access, account/license/billing paths and actual release scope before classification. |
| Crash logs and diagnostics | Lack of an analytics/crash SDK alone does not settle all diagnostics collection; confirm the release inventory and routing. |

Voice routing evidence: `frontend/src/lib/services/voiceCall.ts` selects the
worker stream for GPT Live/PersonaPlex; `app/core/voice_stream.py` relays it.
A reachable legacy OpenAI Realtime SDP/WebRTC path exists, but Android's
`ChatActivity.kt` HTTP interception appears to block its non-worker handshake.
No automatic relay fallback was found. Device behavior is untested: do not
claim either universal relay or successful direct Android-to-OpenAI voice.
Persistence/upload producers: `app/core/turn_recorder.py`,
`app/db/session_events.py`, `app/api/sessions_uploads.py` and
`app/core/upload_transfer.py` (paths from the repository root).

Other App content declarations this app needs:

- **Accessibility API — keep the feature, hold submission:** the reviewed
  `ShuffleAccessibilityService` requires fresh on-device approval for each
  cross-app read/action, but AI proposal/target planning is not a static
  human-defined script. [Current official guidance](https://support.google.com/googleplay/android-developer/answer/10964491?hl=en)
  (fetched 2026-10-01) prohibits autonomous initiation/planning/execution and
  permits deterministic rule-based scripts. It does not establish that
  per-action approval makes this AI-directed flow eligible. Owner review and
  policy clarification are needed before any eligibility attestation. Do not
  remove the feature or declare `isAccessibilityTool` to claim a disability-tool
  exception; the general-assistant use does not establish that status.
  Before submission, establish a separate normal-use in-app disclosure of
  accessed data, use and onward worker/AI-provider processing, with affirmative
  consent; settings wording/service metadata alone is insufficient. Complete
  the Console declaration and demonstration video showing disclosure, consent,
  service enablement, an approved read/action and denial. No compliant
  disclosure/video or policy approval has been established by this review.
- **Foreground service:** the existing draft says voice declares none;
  reconcile with the signed release manifest. Cross-app Accessibility polling
  can operate while another allowed app is foreground, not only while Shuffle
  is focused.
- **Camera:** verify QR-pairing and other enabled camera paths in the release.
- **Privacy policy URL:** `https://hobbycoders.com/privacy`; reconcile voice,
  uploads, transcript persistence, provider processing and retained deletion
  records before submission.
- **Target audience:** pending explicit owner choice and actual-release review.

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

1. Resolve the pending Data safety, audience, mailbox and Accessibility
   eligibility/disclosure/video gates above before submission; only after
   owner review may declarations be submitted and approval checked.
2. Deletion URL checked from a browser without the app.
3. Privacy policy covers the hub, hosted sign-in and the Cloudflare tunnel
   path (website item W8).
4. Store listing, a feature graphic (1024x500) and at least two phone
   screenshots uploaded, all without vendor names or logos.
5. A closed test with real testers for as long as Play requires for new
   personal developer accounts, if the account is a personal one.
