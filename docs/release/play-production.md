# Google Play production: owner submission pack

Package: `com.hobbycoders.aishuffle`. Issue: #2289. Prepared 2026-10-12
against app revision `6b02951bd7dc517ce7fccb97ea85bccacba7fbf9` plus this PR.
Implements Phase 3 / D7 of [the open-source plan](open-source-plan.md).
This is a submission pack, **not a claim of Play approval**. No Console changes,
release dispatch, signing-key export, reviewer provisioning or production rollout
were performed while preparing it.

## 1. Check production eligibility first

In Play Console, select the developer account, open **Developer account > Account
details / About you**, and record Organization or Personal and the creation date.
If the date is not displayed, use the registration confirmation and the app's
Dashboard production-access task; do not infer the type/date from the publisher
name. Open **AI Shuffle > Test and release > Production** and record whether
production access is already granted. Complete any identity/device verification
task shown by the Console.

Decision tree:

- Production access already granted for this app: go to the listing/forms below.
- Organization, or Personal created on/before November 13, 2023: the special
  new-personal-account closed test does not apply. Still complete all dashboard,
  verification and policy tasks; the Console is the final eligibility check.
- Personal created **after November 13, 2023** and no production access: run a
  **closed test with at least 12 testers continuously opted in for at least
  14 days immediately before applying**. Internal testing does not count.
- Type/date uncertain or Console shows a test requirement: follow the Console
  task, do not assume an exemption. Record its exact requirement if it differs.

[Google's current rule](https://support.google.com/googleplay/android-developer/answer/14151465?hl=en)
was fetched on 2026-10-12 and says 12/14. Recruit 15–20 consenting testers from
the existing community/newsletter to leave headroom. Invite them to a closed
track email list or group, then send the **Console-generated opt-in URL**. Being
on the email list is not opting in. Each tester must opt in and actually exercise
the app; use their own workers or isolated test workers, never the owner's fleet.
Keep consent, feedback, fixes, build versions and the Console's eligible-tester
count. An opt-out breaks that tester's continuous period. Keep at least 12 opted
in while the application is reviewed. After the qualifying period, Dashboard >
Apply for production access; describe actual test engagement, feedback and fixes.
Do not paste invented feedback or treat the 14 days as automatic approval.

## 2. Final store listing and assets

Default language: English (United States). App, not game. Category:
**Productivity**. App price: **Free**. Support email: `support@hobbycoders.com`.
Website: `https://hobbycoders.com/ai-shuffle`.

The files below are the canonical copy/paste text (no vendor names/logos):

- Title: [title.txt](../../android/play/listing/en-US/title.txt) — **AI Shuffle**.
- Short description: [short-description.txt](../../android/play/listing/en-US/short-description.txt)
  — **Run AI coding and automation agents on your own machines from your phone.**
  (73 characters; limit 80.)
- Full description: [full-description.txt](../../android/play/listing/en-US/full-description.txt)
  (limit 4,000).
- Release notes: [changelogs/default.txt](../../android/play/listing/en-US/changelogs/default.txt)
  (limit 500 per locale). Use for the first production release only, not every
  subsequent update.

The existing listing tree uses hyphenated description filenames consumed by the
repository's branding checks. It is not an automatically configured fastlane
upload: a fastlane adapter must map them to `short_description.txt` and
`full_description.txt` under its metadata locale directory; `changelogs/default.txt`
already follows that convention. Neither Android workflow uploads this listing.

Asset requirements / capture brief:

| Console field | Deliverable |
| --- | --- |
| App icon | 512 × 512 px, 32-bit PNG, at most 1 MB; own AI Shuffle mark, no manually added Play rounded mask/shadow. Launcher density assets are not automatically a compliant store icon. |
| Feature graphic | 1024 × 500 px, JPEG or 24-bit PNG without alpha; own mark plus “Your AI agents. Your machines.” Keep important content away from edges. No store badges, prices, rankings, third-party logos or time-limited claims. |
| Phone screenshots | At least two; prepare four real portrait captures at 1080 × 1920 or 1080 × 2400. JPEG or 24-bit PNG without alpha; each dimension 320–3840 px, longest no more than twice shortest. Four captures at least 1080 px on each short side support promotional eligibility. |
| Shot 1 | Worker list, only disposable “Review worker”; no private hostnames/account email. |
| Shot 2 | Real coding session on a toy project, user request and streaming response. |
| Shot 3 | A real approval request, before approval; sanitized command/path. |
| Shot 4 | Session list / voice controls with no live personal data. |
| Other form factors | Do not claim tablet/Chromebook/TV/Wear layouts without testing and supplying requested assets. Phone-only evidence here is not tablet verification. |

Capture the release candidate, not a mockup or the offline demo pretending to be
live. Hide provider names/logos as required by the plan by using a neutral profile
label. Do not alter controls or fabricate functionality in screenshots. Asset
creation/capture remains outstanding; this PR supplies requirements, not images.

## 3. Data safety: answer from the entire app, not just the hub

**Collects or shares required user-data types? Yes.** Transmitting off the phone
counts even when the destination is the user's own worker. This includes the
worker-served `/mobile/` UI, not only native Kotlin. “Prompts never leave the
worker” is false: configured AI/speech providers receive them and remote traffic
passes through the tunnel provider. The hub does not *store* chats; that is a
different statement. The README and live website were checked for the value
statement, not used as evidence of data flows.

### Form-level answers

- Data encrypted in transit: **Yes for the supported Android paths** (HTTPS
  worker origins, TLS hub requests; no cleartext Android traffic). Not a claim
  that Cloudflare tunnels are end-to-end encrypted to the worker, nor that a
  user-configured downstream AI endpoint is always TLS. Do not submit this
  answer for a candidate that enables a plaintext Android path.
- Account creation: **OAuth** for new accounts. Do not describe account pairing
  as creating a password account. Legacy password verification on the deletion
  page is not the new Android signup method.
- Account deletion URL: **https://hobbycoders.com/delete-account**.
- Users can request deletion: **Yes**. App: Settings > Account > Delete account;
  website works without the app or an online worker. This deletes hosted account
  records, not files/conversations on the user's worker or the user's provider
  account. Optional partial-data requests: contact `support@hobbycoders.com`;
  do not claim a self-service partial-deletion feature.
- Independent security review/certification: **No** (none established here).
- Advertising/marketing, data sale, creditworthiness: **No** for the app flows
  reviewed. The separate opt-in website newsletter is not an Android SDK.

### Data-type selections

Use **Collected** for every Yes row below. “Required” means automatic on the
connected path with no independent opt-out; it does not mean the offline demo
requires an account. Use **not ephemeral** unless retention of the entire
processing chain is verified: worker transcripts, journals and logs can persist.
Do not mark voice/location ephemeral just because their native buffers are short.

For **Shared**, the prepared conservative selection is **Yes for user content,
voice, attachments, location, screen/tool activity and diagnostics sent for an
optional AI diagnosis** which can go from the worker to independently chosen AI
providers; **No for account/device records processed solely by HobbyCoders and
its contracted service providers**. This does not imply a sale. Google's service-provider and
user-initiated-transfer exceptions may permit narrower sharing declarations,
but do not use them to hide an unverified provider's independent retention/use.
No “no data collected” or blanket “no data shared” claim is prepared here.

| Play data type | Selection / requiredness / purpose | Code evidence and destination |
| --- | --- | --- |
| Personal info: Name, Email address | Yes; optional (sign-in); Account management, App functionality | `AccountOAuth.begin` requests `openid profile email`. Hosted identity provider processes name/email. Hub `oauth.ts:identityAccount` keys records by issuer/subject and initializes username from profile. No separate email column in the hub account record; `website-identity.ts:verifiedEmail` reads verified email for account services. |
| Personal info: User IDs | Yes; optional (account); Account management, App functionality, Fraud prevention/security | `AccountProfile.saveUsername` PATCHes username to `/v1/me`; `AccountStore` stores session/account IDs; cloud `oauth.ts` persists identity subject/issuer. Username belongs here even when it is not a real name. |
| Device or other IDs | Yes; required for a connected device; App functionality, Account management, Fraud prevention/security | `HubClient.device` sends install device ID, manufacturer/model display name and platform. `AccountRepository.sync/link` sends registration/ticket/device records. Worker URL/name and authorization/last-seen records support connection, not ads. |
| Messages: Other in-app messages; App activity: Other user-generated content | Yes; optional user submissions; App functionality | Worker-served chat and `frontend/src/lib/components/chat/ChatInput.svelte` submit prompts; native `ChatActivity` loads the authenticated worker UI. Stored on worker; provider/tool chain may receive content. Support notes go to HobbyCoders only on Send. |
| Files and docs; Photos; Videos; Audio files (other audio files) | Yes; optional selected attachments; App functionality | `ChatFileChooser.parseResult` returns user-selected content URIs; shared `ChatInput.svelte:uploadComposerFile` uploads to worker. Arbitrary supported files, not a bulk device-library scan. Provider processing follows the user's session. |
| Audio files: Voice or sound recordings | Yes; optional voice; App functionality | `ChatActivity` microphone permission gate; shared `frontend/src/lib/services/voiceCall*` audio path to worker/speech provider. Do not claim always-on listening or no audio leaves the phone. |
| Location: Approximate and Precise | Yes; optional; App functionality | `DeviceToolExecutor.location` requires coarse/fine OS permission AND per-action approval, returns cached latitude/longitude/accuracy; `DeviceToolsRuntime` posts result via `DeviceToolsApi` to the selected worker, then agent context. No background location permission. |
| App activity: App interactions / Other actions | Yes; optional phone-tool actions; App functionality | `ToolRegistry`, `DeviceToolsRuntime`, `CommandJournal`; tool requests/results and local journals. `ShuffleAccessibilityService` can return visible non-sensitive screen text, package and node refs after local consent, which reach the worker. No installed-app inventory upload. Screen text is also Other user-generated content. |
| App info and performance: Crash logs, Diagnostics | Yes; required on connected UI for automatic error logging; Analytics (diagnosing/fixing errors), App functionality; not ads/marketing | Shared `services/error-reporter.ts:reportClientError` posts error/message/detail/URL/session context to `/logs/client-error` on the worker. `app/core/support_report.py` builds optional previewed support reports (errors, logs, version/platform); Send forwards to HobbyCoders. Native dependencies contain no crash-reporting SDK; do not confuse that with no diagnostics. |

No dedicated collection of contacts/address book, SMS inbox/call history,
calendar database, health/fitness, financial account/card numbers or advertising
ID was found. `ToolRegistry` opens dialer/SMS/calendar editors; it does not read
those databases. User-entered text/attachments may contain personal information;
do not describe them as automatically anonymized. `CAMERA` is used for local QR
scanning; the scanner result is a pairing code, not an uploaded camera photo.
A selected photo attachment is covered above.

**Push token: none in this Android implementation.**
`AccountRepository.sync` explicitly enrolls `supports_push: false`;
`android/app/build.gradle.kts` has no Firebase/FCM SDK and the manifest has no push
service. Worker/browser notification paths elsewhere are not proof of an Android
push token. No analytics/ads SDK appears in the native dependency list.

Source index (paths relative to repo root):

- `android/app/src/main/AndroidManifest.xml` and `android/app/build.gradle.kts`:
  permissions, cleartext denial, SDK inventory, min/target SDK and signing.
- `android/app/src/main/java/com/hobbycoders/aishuffle/account/`:
  `HubClient.kt`, `AccountOAuth.kt`, `AccountProfile.kt`, `AccountRepository.kt`,
  `AccountStore.kt`, `AccountDeletion.kt` (functions named above).
- `android/app/src/main/java/com/hobbycoders/aishuffle/ui/`:
  `ChatActivity.kt`, `ChatFileChooser.kt`, `ToolSettingsActivity.kt`,
  `AccountActivity.kt` (sign-in/QR/deletion producer paths).
- `android/app/src/main/java/com/hobbycoders/aishuffle/tools/`:
  `DeviceToolExecutor.kt:location`, `DeviceToolsRuntime.kt` result POST,
  `DeviceToolsApi.kt:request`, `ShuffleAccessibilityService.kt`, `ToolRegistry.kt`.
- `mobile/src/routes/+layout.svelte` uses shared
  `frontend/src/lib/components/layout/RootLayout.svelte`, which installs global
  error handlers on the mobile path too; `frontend/src/lib/services/error-reporter.ts:reportClientError`
  sends to the worker. `app/core/support_report.py` is the opt-in hub report path.
- Private `HobbyCoders/hobbycoders-cloud`, inspected at
  `723d71ad807046523932759a7d5b49aac874ae01`: `hub/src/oauth.ts`,
  `hub/src/website-identity.ts`. These citations are for maintainers; reviewers
  should not need private-repository access.

[Google's Data safety definitions](https://support.google.com/googleplay/android-developer/answer/10787469?hl=en)
include developer-controlled WebViews, pseudonymous IDs and ephemeral transfers.
Reconcile the final Console preview with the actual candidate and live policy,
not only the summary table.

## 4. App access: isolated reviewer account and worker

Select **All or some functionality is restricted**. The offline demo is useful
without sign-in but is not a substitute for review access to real functionality.

Concrete setup, before submitting:

1. Create a dedicated disposable VM on a separate cloud host, not HomeLab,
   GamingPC or another owner worker. No owner LAN route/VPN, host mounts, Docker
   socket, SSH keys, GitHub token, personal repository or production database.
   Deny access to cloud metadata, private networks and host management interfaces.
   Treat arbitrary coding-agent execution as untrusted; use a VM boundary.
2. Install the same release worker revision as the candidate. Create only a toy
   project named `review-sandbox`, a harmless README and sample tests. Configure
   a dedicated supported AI provider project with a modest hard spending cap and
   sufficient review quota, or a tested local model. Never reuse an owner key.
   Disable shell/network privileges not needed for the toy exercise; leave
   approvals enabled. Verify streaming, downloads and an approval actually work.
3. Create a dedicated HobbyCoders reviewer identity in the existing hosted
   sign-in system, verify its email, complete username setup and link only this
   worker as **Review worker**. Its one free remote seat is sufficient. No paid
   subscription or card is needed. Use ordinary supported password sign-in that
   works without an OTP/MFA, personal inbox, QR, one-time pairing code, geographic
   restriction or owner approval. Do not weaken global authentication. If the
   configured identity service cannot provide repeatable reviewer sign-in,
   review access is blocked until a supported reviewer method is provisioned.
4. Give this worker managed HTTPS remote access. Keep it patched, online and
   funded throughout review and later re-reviews. Verify on a clean Android
   install off the owner's network. Test more than one reviewer device; do not
   use expiring enrollment tickets as permanent login instructions.
5. Store the reviewer username/email and password **only in Play Console's
   private App access fields** (or the owner's secret manager). No credentials
   in this repository, issue, PR, screenshot or release notes. Keep separate
   restricted operational access for replenishing quota/resetting the sandbox.

Paste into **Instructions** after those checks pass:

> AI Shuffle is a remote client for AI coding and automation agents running on
> a separate worker. A dedicated review account and isolated worker are provided;
> no purchase or personal server is required. Use the username/email and password
> in this App access entry. Open AI Shuffle, choose Sign in, finish sign-in in
> the browser and return to the app. Select Review worker. Start a new coding
> session in review-sandbox and send: “Read README.md and suggest one small
> improvement. Ask before changing any file.” Review the response and any tool
> approval. You can also open an existing sample session. Settings > Offline
> demo works without signing in but does not run agents. Phone tools are optional
> and are off until enabled; permission prompts can be declined for normal chat.
> Settings > Account > Delete account opens the account-deletion flow. Deleting
> this review account revokes its remote access; contact support@hobbycoders.com
> if a fresh review account is needed. The review worker contains no personal
> data. Contact support@hobbycoders.com for access problems.

Do not paste this as a statement of completed provisioning until it is true.
The reviewer account/worker was not created or exercised in preparing this pack.
Use a second disposable account to rehearse deletion rather than breaking the
account assigned to a pending review.

## 5. App content / policy forms

### Content rating and audience

Start the IARC questionnaire with `support@hobbycoders.com`; category **All other
app types / productivity**, not a game. Intended audience: **18 and over only**;
not designed for children, no Families participation. That is an audience choice,
not a guarantee that IARC will issue a particular rating or an age verification
system. Do not claim an age gate exists when it does not.

Prepared answers, with exact wording checked against the current questionnaire:

| Question family | Answer / explanation |
| --- | --- |
| Built-in violence, gore, fear, sexual/nudity content, profanity, drugs/alcohol/tobacco, gambling, simulated gambling, contests for money | No to *included/editorial* content; the bundled demo and product do not provide these. This is not a claim that arbitrary generated answers or user files can never contain them. |
| User interaction / user-to-user exchange / public user-generated content | No public social feed, matchmaking or messaging between unrelated users. Chat is with an AI agent on the user's worker. If the question instead asks whether *any user-created content* is available, answer Yes for prompts/files, not No on the strength of “not social.” |
| Online / dynamically generated content | Yes: AI responses and user-controlled files/projects; generative AI, not a fixed curated catalog. Answer any AI-specific follow-up truthfully; do not claim universal filtering or moderation controls not present in the app. |
| Unrestricted internet / browser access | Yes: configured agents and the worker browser can retrieve/show web content; URL phone tools can open a browser. Do not claim a restricted child-safe browser. |
| Shares physical location with other users | No public location sharing; if asked about *collecting or transmitting* location instead, Yes, optional per-action tool to the user's worker. |
| Digital purchases | The Android native app has no billing SDK or in-app purchase checkout. Optional Cloud subscription is a separate hosted service; disclose access to paid services if asked. Do not answer “no paid features anywhere.” No loot boxes or randomized purchases. |
| Ads | **No, my app does not contain ads.** No native advertising SDK; no ads in the worker UI reviewed. |
| News, government, health, financial services | No; general coding/automation utility, not one of these specialist services. |

Save the resulting IARC certificate/rating, not a preselected “Everyone” label.
Country availability and trader/developer identity declarations must match the
owner's real legal status and supported distribution territories; do not invent
an address, registration or verified trader status.

### Privacy and deletion URLs — live, with an identified update needed

Both were fetched successfully without sign-in on 2026-10-12:

- **Privacy policy:** https://hobbycoders.com/privacy
- **Account deletion:** https://hobbycoders.com/delete-account

The live privacy policy already describes hosted identity, device/worker records,
Cloudflare tunnel transit, provider requests, voice and opt-in support reports.
The live deletion page describes removal, remote-access revocation, retained
billing records, and the temporary 24-hour deletion receipt/identity hash.

However, the fetched privacy page does **not explicitly describe optional Android
location or AccessibilityService screen results**. Before submission, publish the
following addition in `hobbycoders-cloud`'s privacy page and review the whole page
for consistency (this app PR does not deploy or edit that private service):

> **Android phone tools and diagnostics**
>
> Phone tools are optional. When you enable them for a selected worker, that
> worker can request supported actions while the phone is available. Requests
> and results may be kept in the worker's session history. Location requires an
> Android permission and a separate approval for each request. An approved
> request sends a recent location, its accuracy and timestamp to your selected
> worker and may be included in the context sent to your configured AI provider.
> We do not collect background location.
>
> Optional app control uses Android's AccessibilityService. After you enable it,
> each screen read, tap, scroll or back action requires local approval. Approved
> screen reads can send visible text, the app package and control references to
> your selected worker and its configured AI provider. Sensitive screens are
> blocked by the app's safety checks; those checks are not a promise that all
> personal information will be removed. Do not approve a screen containing
> information you do not want the worker or provider to receive. You can turn
> app control off in Phone tools or Android accessibility settings.
>
> QR scanning is local; the pairing code, not a camera image, is used for pairing.
> Files and photos you select for a conversation are uploaded to your worker.
> The worker-served interface automatically sends error details and session
> context to that worker for troubleshooting. These are not automatically sent
> as support reports to HobbyCoders. A support report reaches us only when you
> preview it and choose Send. Managed remote access carries these requests
> through Cloudflare as described above. Remove worker records on your worker;
> deleting your hosted account is not a remote wipe of those records.

### Additional submission blockers to clear, not checkbox workarounds

- **AccessibilityService declaration:** present in the manifest and
  `res/xml/accessibility_service.xml` (`canRetrieveWindowContent=true`, no
  gestures). It is **not** a disability-support accessibility tool. Declare
  optional user-approved app control, its prominent disclosure and per-action
  consent; supply the Console's requested demonstration video showing enable,
  decline, approve and disable. `ToolSettingsActivity` and
  `ShuffleAccessibilityService` implement those paths. Get the current policy
  assessment for AI-assisted actions; never claim fully autonomous access or
  that per-action consent automatically guarantees approval.
- **AI-generated content reporting:** Google's
  [AI-generated content policy](https://support.google.com/googleplay/android-developer/answer/13985936?hl=en)
  requires in-app reporting/flagging of offensive generated content without
  leaving the app. A code search did not establish such a path; the existing
  error support report is not evidence of it. Verify a working content-report
  path and moderation handling on the candidate, or implement it in a separate
  reviewed change before submission. A separate “Report AI content” UI change
  is in progress; it remains a launch follow-up until merged and exercised on the
  candidate. Do not mark this satisfied by email alone.
- **Billing:** do not add external purchase links to the Android listing to
  bypass Play billing. Audit any worker-served Cloud purchase UI accessible in
  this client against applicable Play billing rules before rollout; native SDK
  absence alone does not prove policy compliance.

## 6. Bundle lineage, release assets and production procedure

### Build or promote the exact candidate

Prefer **promoting the already tested internal version** to closed testing / later
production in Console. No signing change and no second upload of the same version
code are needed. Verify worker compatibility and the reviewer path first.

For a new candidate from merged main, the existing build-only workflow is:

```sh
gh workflow run android-play.yml --repo HobbyCoders/ai-shuffle --ref main   -f publish_to_play=false -f workers_ready=false
gh run list --repo HobbyCoders/ai-shuffle --workflow android-play.yml --limit 5
# Identify the requested run and its head SHA; never assume an unrelated latest run.
gh run download RUN_ID --repo HobbyCoders/ai-shuffle   -n ai-shuffle-play-bundle -D /absolute/scratch/play-RUN_ID
openssl x509 -in /absolute/scratch/play-RUN_ID/signing-certificate.pem   -noout -fingerprint -sha256
```

Wait for completion through Monitor (`gh run watch RUN_ID --exit-status`), not a
foreground shell wait. The artifact contains `app-release.aab` and the **public**
certificate, not a private key. Build-only retention is seven days. This command
does not publish any track. `workers_ready=false` is safe for build-only; it is
not permission to release against incompatible workers.

`android-play.yml` requires the existing `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`,
`KEY_ALIAS`, `KEY_PASSWORD`; Gradle uses that release config for the bundle.
Compare the exported certificate with **Play app-signing certificate**, not just
upload certificate, in Setup > App integrity. Expected original SHA-256:

```text
04:D5:C1:D6:0E:C4:9B:AB:A6:1E:02:01:A0:03:C6:A0:B5:F6:8D:9A:9E:14:6C:5E:E5:CD:DB:BB:34:37:57:C2
```

Stop on mismatch; do not create a replacement keystore, use a new Google-generated
key, export a private key into artifacts, or rotate signing to get through review.
Target/compile SDK is 36, minimum 26. Version code is epoch minutes, so duplicates
must not be re-uploaded. Use the actual build metadata/Console code, not a guessed
version. Strict bundle verification and mobile-asset checks run in the workflow;
Play must still accept device/API/native-library/page-size eligibility.

There is **no automated production track input**. `publish_to_play=true` publishes
**internal only**, with the `google-play-internal` signing/readiness gates. Keep
that service account's permissions testing-only. Use Console to promote the
verified internal bundle (or manually upload the build-only AAB) to production.

### GitHub release APK — fixed by this change

Before this PR, `release.yml` called only `android-play.yml`: despite Phase 3's
statement, no signed APK attachment job was present. This PR adds `android-apk`,
calling existing `android.yml` after successful internal publication (which
checks signing enrollment and compatible workers). It passes the immutable
release tag and `publish_download: true`, with the same signing secrets.
`android.yml` builds/signs and verifies the APK, runs native/emulator gates, then
calls `android/ci/publish-apk.sh`. `prepare-apk-download.py` computes SHA-256 and
provenance; the publisher checks the checksum before uploading these assets to
the **existing versioned release**, not a replacement `android-latest` release:

- `AI-Shuffle-Android.apk`
- `AI-Shuffle-Android.apk.sha256`
- `android-build.json`

The parent release is owner-dispatched at an existing `vX.Y.Z` tag with input
`version=vX.Y.Z` (optional `build_desktop=false`). It also publishes Docker and
stable worker source; **do not dispatch the full release just to obtain an AAB**.
This PR also fixes the Play child's tag condition: a reusable call inherits
`workflow_dispatch` from `release.yml`, not `push`. The supplied tag must match
`github.ref`; owner identity and all readiness/internal-only gates remain.

APK and AAB builds use the same key, but run at different times and can have
**different version codes**. A later sideload APK cannot be replaced by an older
Play code. Test switching channels with a newer compatible code; do not promise
in-place downgrade or ask users to uninstall and lose data. After release,
download all three assets, run `sha256sum --check AI-Shuffle-Android.apk.sha256`,
verify the APK signing certificate with `apksigner verify --print-certs`, and
compare `android-build.json` source/version to the tag. A green parent with a
skipped child is not proof that assets or an internal release exist.

### Owner's Console sequence (only these steps require Console access)

1. Confirm developer type/date, verification tasks and this app's production
   eligibility; record whether the closed test is required.
2. Check App integrity signing identity against the expected certificate.
3. Enter default listing text, support details, icon/graphic/screenshots and
   Free/Productivity classification. Set distribution countries intentionally.
4. Complete App content: live privacy/deletion links, App access credentials and
   instructions, Data safety, IARC rating, 18+ audience, No ads, any requested
   accessibility/permissions declarations, and real legal/trader details.
   Do not submit until the non-Console policy/access/asset blockers above are met.
5. If required, create the closed track, select the tested internal bundle,
   add testers, submit/start the test and distribute its opt-in link. After the
   12-testers/14-continuous-days condition, apply for production access using
   real evidence and wait for approval. Internal/open testing is not a shortcut.
6. Production > Create release (or Promote release): choose the exact accepted,
   tested bundle, enter the release notes above, review device compatibility,
   pre-launch findings, declarations and country availability; resolve blockers.
7. Submit for review. Use managed publishing if the owner wants approval held
   until a coordinated launch. Approval is not publication; explicitly publish
   once approved and ready. **First production rollout: 100%**, as Phase 3
   directs (first production releases are not a useful staged-rollout baseline).
8. Read production track status and version code back; verify the public listing
   and a real non-tester installation/update. For subsequent updates, optional
   staged progression is 10% → 50% → 100%, at least 24 hours of actual crash/ANR,
   sign-in/connection and reviewer-like session evidence at each step. Halt on
   regression; fix forward with a higher code. With too little traffic, elapsed
   time alone proves nothing; use explicit device tests before increasing.

Outside Console, the owner/delegated operator still needs to provision and test
the isolated account/worker, produce assets/video, publish the privacy addition,
resolve content-reporting/billing policy gaps, approve release dispatch and set
up the website link. These cannot honestly be called completed form entry.

## 7. Website Play button

Exact production URL (no testing/opt-in URL):

```text
https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle
```

Verified in `HobbyCoders/hobbycoders-cloud` at the revision cited above:
`.github/workflows/deploy-site.yml` reads `vars.SITE_PLAY_LISTING_URL` in the
**website-production** environment and writes it as Worker `PLAY_LISTING_URL`.
`site/src/downloads.ts:playListing` accepts an HTTPS URL beginning
`https://play.google.com/store/apps/details?id=`; invalid/unset values hide the
Play button. On 2026-10-12 no `SITE_PLAY_LISTING_URL` entry was returned at either
GitHub environment or repository scope. This does not prove the live Worker
variable is absent: the workflow uses `keep_vars: true`.

After the public URL works without tester enrollment, set the environment
variable to the exact URL above in hobbycoders-cloud, explicitly approve/run
**Deploy website** from main, and read back the rendered downloads link and its
public Play target. Merge alone does not deploy. Do not deploy the account hub
for this change or set a public Play button before production is actually live.

## Verification record / remaining evidence

This pack is based on source inspection, workflow regression tests and public
HTTP reads of the website/privacy/deletion pages, not an Android/Console run.
No signing secret, reviewer password, AAB or APK was accessed; no release or
website was deployed. The real reviewer sign-in/session, Play policy acceptance,
closed-test eligibility, production download and native screenshots/video remain
unverified. Those are explicit launch gates, not implied by passing unit tests.
