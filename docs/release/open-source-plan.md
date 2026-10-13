# Going open source: the plan

Written 2026-10-11. This replaces the "sell the app as a license" plan in
[private-beta-readiness.md](private-beta-readiness.md) and
[billing-and-accounts.md](billing-and-accounts.md); where they disagree, this
page wins. Cost figures come from the total-cost and Cloudflare cost reviews
kept in the private archive (`ai-shuffle-private`).

## The shape of it

**The app is free and open source.** The server (`app/`, `fluid/`, `docker/`),
the web UI, the desktop app, the Android app and the iOS project are published
under one open license. Anyone can run AI Shuffle on their own hardware with
their own AI accounts, on their LAN or over their own VPN, with no account and
no key. Updates (stable and nightly) are free.

**AI Shuffle Cloud is the paid part.** It is the HobbyCoders account and
everything that only works because HobbyCoders runs a service:

- remote access to your workers from anywhere, through managed Cloudflare
  tunnels (the `shuffle-<id>.hobbycoders.com` address), from the Android app or
  a browser;
- worker-to-worker sync across different networks (same-LAN sync stays free);
- public endpoints for connectors that need one (Telegram, Discord, Slack
  webhooks and similar);
- the OAuth relay for one-click plugin sign-ins (#2197 is the first);
- in-app support reports and the support inbox.

The free account that exists today keeps its one remote worker. That is the
free tier: one machine reachable from your phone, no card. A paid plan gives
3 workers and the rest of the list. Nothing in the app stops working when a
plan ends; the hub stops serving the extra tunnels.

**What this changes in money terms.** Hosting is a small fixed cost on
Cloudflare. A public repository gets free GitHub-hosted runners (Linux,
Windows and macOS) and free secret scanning, so the paid runner and
secret-scanning subscriptions go away; the remaining fixed cost is hosting,
the domain and the support mailbox, which a handful of paying users covers.
The AI spend for development is separate and is set by how much gets built,
not by this plan.

## Decisions

The owner confirmed D1 to D8 on 2026-10-11, with the open-source work
delegated to subagents; the owner's session itself does only the Play Console
work and security-sensitive steps (the scrub review, secrets, repository
settings).

| # | Decision | Default | Why |
| --- | --- | --- | --- |
| D1 | Open license | **AGPL-3.0** for everything published | Lets anyone use, modify and self-host; a company that runs a modified AI Shuffle as a service must publish its changes. This is what Plausible, Coder and Grafana chose for the same open-core shape. Apache-2.0 or MIT would let a competitor host a closed fork of the app with its own hub. |
| D2 | What stays private | `android/hub`, `android/license`, `android/site`, `android/uptime` move to a private repository `HobbyCoders/hobbycoders-cloud`. `android/registry` is deleted. | The hub is the product being sold. Keeping it private also keeps deploy configuration, GitHub environments and secrets out of a repository that accepts outside pull requests. The app's client code for the hub (`app/network/hub.py`, `app/core/license.py`) stays public; the API it talks to is documented in `docs/content/api-reference`. |
| D3 | History | **A fresh public repository with a single initial commit.** The current repository is renamed `ai-shuffle-private` and kept as the archive. | The history has 2,280 pull requests, owner usage measurements, cost reviews, session audits and a branch of redacted transcripts (`claude/fluid-harness-review-c8a2a5`). Scrubbing all of that from history and proving it is gone costs more than it is worth: after a force-push GitHub keeps the old commits reachable by SHA until support purges them. Issue and PR numbers referenced in docs will point at the private repository; the public one starts at #1. What this breaks, once: the GitHub environments, secrets, variables and rulesets are recreated on the new repository (owner, with the checklist from Phase 1); every checkout on the owner's workers is re-cloned, because the history is new (agents); the hub setting that names the release repository is updated. Installed workers keep updating, because they trust the signing key, not the repository. |
| D4 | Paid plan and trial | **Free account (1 remote worker, no card) plus the paid plan at the approved prices: founder $14/month or $140/year, regular $20/month or $200/year. No trial.** | A free tier replaces the 14-day card trial: people try the product for as long as they like on one worker and pay when they want 3. That removes the trial-reminder email, the accidental-charge dispute risk and the Resend dependency from the launch path. The owner approved a card-required trial on 2026-10-09 for the paid-app model; this is the open-core equivalent of the same decision and needs an explicit yes. |
| D5 | Contributions | **DCO sign-off** (`Signed-off-by` on commits), no CLA. "AI Shuffle", "HobbyCoders" and the shuffle mark stay trademarks of HobbyCoders; the README says so. | A CLA deters contributors and the owner has no plan to relicense. The trademark note is what lets the owner stop someone shipping a modified build under the same name. |
| D6 | Nightly | **One signed build a day at 06:00 UTC from `main`, only when `main` changed, published as channel `nightly`.** The per-merge `edge` publication stops. | Today the edge gate ran 1,845 times in nine days (60 to 70% of all GitHub-hosted minutes). One scheduled run a day is what "nightly" means to users and is what the free runner budget can carry. |
| D7 | Android distribution | **Google Play production**, plus the APK on every GitHub release for sideloading. | Internal testing cannot be the way the public gets the app. Open testing would be a stepping stone with the same listing work, so go straight to production, through a closed test first if Play requires it for this developer account (section 4). |
| D8 | Linux desktop, macOS, iOS | Linux desktop ships with the first open release (the artifact workflow exists). macOS builds become possible for free on public-repo runners, but signing and notarization wait for the $99 Apple Developer account, which waits for revenue. iOS waits for the same account. | Unsigned macOS builds show a warning and are not worth publishing. |

## What stays in the plan from before

- The business entity behind HobbyCoders is handled by the owner; the terms
  and privacy pages name the seller of record, and that is updated if the
  entity changes. None of this is legal advice.
- Stripe products at the approved prices, Clerk sign-in, the seat cap in the
  hub (merged as #2161) and the license server's Stripe webhook all stay.
  They move to the private repository unchanged.
- The release signing, withdraw and forward-fix process in
  [release-channels.md](../ci/release-channels.md) stays. The signing key stays
  in the owner's hands; a public repository does not change who can sign.

## Phases

"Owner" means it needs the owner's accounts, money or a decision. "Agent"
means an agent does it in a pull request or, for console work, drives the
browser with the owner watching and approving each submission.

### Phase 1: get the tree ready to publish (agent, in the private repository)

1. **Split the cloud out.** Move `android/hub`, `android/license`,
   `android/site` and `android/uptime` with their workflows
   (`deploy-*.yml`, `ci-license.yml`, `ci-uptime.yml`, `withdraw-release.yml`,
   the publish half of `desktop-source.yml` and `release.yml`) to
   `hobbycoders-cloud`. Delete `android/registry`, `ci-registry.yml`,
   `docs/install/registry.md` and the pull-token path in the compose files
   (#2157 closes with it).
2. **Remove the local license enforcement.** `app/core/license.py` stops
   refusing agent runs; the lease stays, because the hub reads it to decide
   how many tunnels an account gets. **Settings → System → License** becomes
   **Settings → Cloud** (account, plan, seats). Delete the beta-key download
   gate (`invite-downloads.ts`, `PUBLIC_APP_DOWNLOADS`, `LICENSE_GATE`): every
   installer, APK and source archive is a public GitHub release asset. The
   waitlist and automatic invites (#2207) become a newsletter sign-up or are
   removed.
3. **Make the image public.** `ghcr.io/hobbycoders/ai-shuffle-server` becomes a
   public package; `docs/install/compose` pulls it by version tag with no
   token.
4. **Scrub.** Remove from the public tree: `docs/reviews/`, `mockups/`,
   `videos/` (keep only the tour assets the README links), `.fluid/skills/`
   with owner infrastructure details (`live-test`, `ship-release`,
   `play-internal-release`), `.claude/`, `Claude Code.bat`, and any test
   fixture with a real email address or hostname. Run `gitleaks` and
   `trufflehog` over the final tree, not the history, and keep the report in
   the private repository. Read `THIRD_PARTY_NOTICES.md` against the lockfiles
   once more.
5. **License files.** `LICENSE` becomes AGPL-3.0. Each published package
   (`desktop/package.json`, `frontend/package.json`, `android/app`, `ios`)
   says `AGPL-3.0-only`. Add `CONTRIBUTING.md` for outsiders (DCO, how to run
   the checks locally, what a good PR looks like), a `SECURITY.md` with the
   support address, and a short `TRADEMARKS.md`.
6. **CI on free runners.** Replace every `blacksmith-*` label with the
   GitHub-hosted equivalent. Keep the path filters and the draft rule from
   #2073; keep the full suites on `main` only. Nothing with a secret runs on
   `pull_request` from a fork. Cancel Blacksmith.
7. **README.** Rewritten for someone landing from GitHub (section 6).

### Phase 2: nightly channel (agent)

1. Rename the channel everywhere: `UPDATE_CHANNEL=stable|nightly` for Docker,
   `{"channel":"nightly"}` for desktop, the hub manifest at
   `/v1/desktop/manifest/nightly`, image tag `nightly`. `edge` is accepted as
   an alias for one stable release so workers already on it keep updating,
   then removed.
2. A channel picker in **Settings → System → Updates** with one line of text
   under each: stable ("tested releases"), nightly ("yesterday's `main`; may
   break; cannot go back to stable without a reinstall, because migrations
   do not run backwards").
3. `desktop-source.yml` runs on a schedule (06:00 UTC) and on manual dispatch
   only; it exits early when `main` has not moved since the last nightly.
   The exact-tree CI gate stays.
4. Docs: `updates.md` describes both channels.

### Phase 3: Android to Google Play production (agent drives, owner approves)

Done in the browser against Play Console, with the owner authorizing each
submission. In order:

1. **Confirm the account type** (Play Console → Account details). If it is a
   personal account created after Play's closed-testing rule took effect,
   production access needs a closed test first; the agent reads the current
   requirement from the console page at that time (it was 12 testers opted in
   for 14 days when last checked) and reports it. The Discord and the
   newsletter list are where the testers come from.
2. **Store listing:** title, short and full description (same value statement
   as the README), 512 px icon, 1024 × 500 feature graphic, at least two phone
   screenshots. No vendor names or logos anywhere in the listing.
3. **Policy forms:** data safety (the hub stores a username and sign-in
   subject; Clerk processes email and name; no prompts leave the worker),
   content rating questionnaire, target audience, ads declaration, privacy
   policy URL (live), account deletion URL (live), and **app access**
   instructions: the app needs a worker, so the owner keeps one demo worker
   linked to a reviewer account for the review period.
4. **Closed test** if step 1 says so: promote the current internal build to a
   closed track, publish the opt-in link, wait out the period.
5. **Production release** from the same bundle lineage (`android-play.yml`
   already signs with the original key). First rollout at 100%; there are
   too few users for staged rollouts to tell anything.
6. **APK on GitHub releases:** `release.yml` attaches the signed APK and its
   SHA-256 next to the installers.

### Phase 4: publish (owner, with the agent preparing everything)

1. Decide the seller line for the terms (the owner's name, doing business as
   HobbyCoders) and check the fictitious-name registration.
2. Rename `HobbyCoders/ai-shuffle` to `HobbyCoders/ai-shuffle-private`.
   Create the public `HobbyCoders/ai-shuffle` from the Phase 1 tree as one
   commit. Move the GitHub environments and secrets that the private repo
   still needs (release signing, Play) and delete the rest. Turn on free
   secret scanning and push protection on the public repository. Downgrade
   the org from Team to Free if nothing left needs Team.
3. Deploy `hobbycoders-cloud` from its new repository. Set
   `HUB_LICENSE_BINDING=on` so seats are enforced, and the Stripe prices.
4. Website: the downloads page lists every platform with no key; the pricing
   page sells Cloud, not the app; every page links the repository; the
   license, terms and refund pages are rewritten for a subscription to a
   service rather than a software license.
5. Cut `v1.0.0` from the public repository: Docker image, Windows and Linux
   installers, APK, Play production.
6. Announce: the HobbyCoders Discord and Reddit, r/selfhosted, r/unRAID,
   r/LocalLLaMA, Hacker News "Show HN". The README is the pitch; the post is
   three sentences and a link.

### Later, once there is revenue

- Apple Developer account: macOS signing and notarization, then the iOS app.
- Cloudflare Pro zone for `hobbycoders.com` at about 150 worker DNS records
  (about 60 paid accounts), and the tunnel limit request at about 250.

## 5. Order and effort

| Order | Work | Who | Size |
| --- | --- | --- | --- |
| 1 | D1 to D8 confirmed | Owner | an hour |
| 2 | Phase 1 items 1 to 3 (split, enforcement, public image) | Agent | the biggest piece; several PRs across app, hub, compose and docs |
| 3 | Phase 1 items 4 to 7 (scrub, license files, CI, README) | Agent | a day of agent work, then owner review of the scrub report |
| 4 | Phase 2 nightly | Agent | one PR each for app, desktop, hub, workflow |
| 5 | Phase 3 Android | Agent + owner | a few console sessions; the closed-test wait, if required, runs in parallel with everything else |
| 6 | Phase 4 publish | Owner + agent | an afternoon once 2 to 5 are done |

Phases 2 and 3 do not depend on Phase 1 and can run at the same time.
Phase 3 step 1 should happen first, because a 14-day wait, if there is one,
decides the publish date.

## 6. The README

The current README is written for a buyer. The public one is written for
someone who found the repository and has thirty seconds. Its sections, in
order:

1. **One line:** run AI coding and automation agents on your own machines,
   from anywhere, with any model, paying only your AI provider.
2. **The tour video**, then three screenshots: a session with the folded work
   log, the worker list on a phone, the automations page.
3. **Why this and not Claude Code or Codex in a terminal:** AI Shuffle runs
   those same tools. It adds the part they do not have: a fleet. Several
   machines (a NAS, a desktop, a laptop) in one account, hand a session from
   one to another, start work from your phone, keep sessions, projects,
   worktrees, pull requests and CI in one place, run on a schedule or from a
   Telegram message, and switch between Claude, OpenAI, OpenRouter, xAI or a
   local model per profile. A short comparison table against Claude Code,
   Codex, OpenHands and Coder on: own hardware, multiple machines, phone app,
   any provider, subscription sign-in (not only API keys), automations,
   connectors, open source.
4. **Install:** three commands for Docker Compose; links for the Windows and
   Linux installers and the Android app on Play; the APK on the release page.
5. **Free and Cloud:** the one-paragraph version of "The shape of it" above,
   with the pricing link.
6. **Contributing**, **security**, **license** (AGPL-3.0, trademark line),
   **not affiliated** with any AI provider.
7. Links: website, docs, Discord, FAQ.

The website gets the same comparison table and the same one line.

## 7. Risks

- **Someone hosts the hub themselves.** They cannot: it is private. They can
  write their own; the app's hub client is small and documented. That is the
  AGPL trade-off and is accepted.
- **Fork PRs and secrets.** No workflow that holds a secret may run on
  `pull_request`. Release, Play and deploy workflows run from the private
  repository or on `workflow_dispatch` only. Phase 1 item 6 checks every
  workflow for this before publication.
- **The scrub misses something.** The fresh-repository decision (D3) limits
  the exposure to the final tree, which two scanners and the owner read. If
  a secret is found after publication, it is rotated, not hidden.
- **Support load.** Open source brings issues from people who did not pay.
  Issue templates that ask for the in-app support report, and a stated
  "best effort, Cloud customers first" line in `SUPPORT.md`, keep this
  bounded.
- **The free tier is enough for most people.** It probably is. The paid plan
  is for people with more than one machine, which is the owner's own use and
  the homelab audience the product was built for. If nobody converts in 90
  days, the app is still a finished open-source project, and the hub's cost
  is $5 a month.
