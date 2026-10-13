# Private beta readiness

Historical checklist: superseded by the [open-source plan](open-source-plan.md).
The app, installers and updates are public downloads with no invite or license
key. The registry is removed, and the owner must make the GHCR server package public.

One list from today to the private beta, then to the first paid sale.
Checked on 2026-10-09 against `main` at `bef0ecfc6` and the live public
URLs (curl only; nothing was deployed or changed). Older lists are
08-owner-actions (review in the private archive)
and 09-status-and-handoff (review in the private archive);
where they disagree with this page, this page is newer. The last section
re-checks every item in them.

Updated on 2026-10-10 for the automatic beta invites: the license server and
the registry now answer (`/v1/license/health` returns `{"ok":true}`,
the retired registry health endpoint returns 200), so Docker works with a pull
token. The invite pipeline is built but not switched on; see
"Turning on beta invites".

**Who:** Owner = needs the owner's accounts, money or decision. Agent = an
agent can do it in a PR. **Status:** Done, Needed, or In progress (another
agent is on it now; do not start it twice).

Work in progress elsewhere on 2026-10-09:

- (a) accounts, sign-in and Stripe checkout (now no trial; open-source plan D4)
- (b) the per-license worker cap
- (c) worker in-app update reliability, including the Docker restart policy ([#2112](https://github.com/HobbyCoders/ai-shuffle/issues/2112))
- (d) legal and provider compliance audit
- (e) the standalone terminal
- (f) git clone from a GitHub repository picker
- (g) Cloudflare cost for 1,000 users: first baseline merged as
  cloudflare-cost-1000-users-2026-10.md (review in the private archive);
  total cost and break-even for paid licenses in
  total-cost-and-break-even-2026-10.md (review in the private archive)

## How a tester gets AI Shuffle today

| Platform | Today (2026-10-09) |
| --- | --- |
| Docker (the main install) | **Available to key holders.** The compose file pulls from the retired per-token image path; the registry and the license server that mints pull tokens are both live (checked 2026-10-10). Each founder key comes with a pull token, and the invite email carries both. |
| Windows desktop | Built (0.9.1 is listed at `/v1/desktop/installers`), but downloads are paused (`"downloads":"paused"`) and the installer is unsigned, so Windows shows a SmartScreen warning. |
| Linux desktop | The docs call it a beta, but nothing is published: `/v1/desktop/installers` has `"linux":null` and the downloads page says "Coming soon". |
| macOS desktop | Not built for release. |
| Android | 0.9.1 is on the Play internal testing track. A tester must be on the track's tester list (a Google Group once the owner sets it up; see "Turning on beta invites"), and needs a worker to connect to. The public APK download is paused; a key holder gets it from the downloads page. |
| Updates | Once installed, updates work: the signed stable manifest at `/v1/desktop/manifest` is 0.9.1, and Docker installs update in the app or by changing `AI_SHUFFLE_VERSION`. Android updates come from Play. |

## The beta path, in order

The simplest safe path keeps the app in **beta mode** (every install counts
as licensed, because `LEASE_PUBLIC_KEYS` in `app/core/license.py` is empty)
and uses the license server to mint pull tokens and to check invitees'
license keys for the beta downloads. Stripe, Resend, the lease signing key,
the license gate and the tunnel caps are **not** needed for the beta.

1. **License server: done.** `/v1/license/health` returns `{"ok":true}`.
2. **Registry: done.** the retired registry health endpoint answers.
3. **Owner, with an agent walking through it: one clean-box install.** Mint
   your own founder key (`POST /v1/license/admin/keys`
   `{"kind":"founder","email":"…","days":365}`), or approve your own address
   on the waitlist once invites are on, then follow
   [Install with Docker](../content/getting-started/install-docker-compose.md)
   word for word on a machine that never ran AI Shuffle, using
   [acceptance-clean-box.md](../internal/acceptance-clean-box.md). Fix what the
   docs got wrong before inviting anyone.
4. **Owner: turn on beta invites** (the next section). After that, nobody
   mints or sends keys by hand. A person joins the waitlist, you press
   Approve (in the sign-up email or on `/account/waitlist`), and they get an
   email with a free founder key (365 days by default), their Docker pull
   token, the downloads page for Windows, Linux and the Android APK, the Play
   internal-testing links, the account page, the beta terms and the support
   address.
5. **Owner: desktop and APK downloads** for key holders
   ([#2155](https://github.com/HobbyCoders/ai-shuffle/issues/2155); details in
   [hobbycoders-cloud/hub/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/hub/README.md#beta-invite-downloads)).
   This is step 4 of "Turning on beta invites". Invitees enter their key on
   the downloads page under "Have a beta invite?" and get a link that works for
   15 minutes and 3 downloads. Optional, to serve the files from R2 instead of
   GitHub (the cost review's recommendation): create the bucket, set
   `R2_INSTALLER_BUCKET` and the R2 token secrets, redeploy the hub and upload
   the current release, as in
   [hobbycoders-cloud/hub/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/hub/README.md#installer-downloads-from-r2).
   Windows testers will see the unsigned-installer warning. Do not turn on
   `PUBLIC_APP_DOWNLOADS`; it opens downloads to everyone.
6. **Owner: Android internal testing** through one Google Group (step 6 of
   "Turning on beta invites").
7. **Owner: open a support channel.** Mail to support@hobbycoders.com reaches
   the Google mailbox today. Create the Discord server or invite and set
   `SITE_DISCORD_INVITE_URL`, or remove the "ask in the Discord" line from the
   downloads page ([#2155](https://github.com/HobbyCoders/ai-shuffle/issues/2155)).
8. **Updates for testers:** Docker testers update in the app or by changing
   `AI_SHUFFLE_VERSION`; desktop testers update in the app; Android testers
   through Play. Releases go out through `release.yml` as today
   ([release-channels.md](../ci/release-channels.md)).

## Turning on beta invites

The pipeline is in `hobbycoders-cloud/site/src/beta.ts` (site) and
`hobbycoders-cloud/license/src/beta-invites.ts` (license server); the design is in
[hobbycoders-cloud/site/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/site/README.md#beta-invites). Billing
stays off throughout: invite keys are free founder keys, and nothing here
touches Stripe or `SITE_PRICING_PAGE`. Do these in order. Each deploy is a
manual workflow run from `main` after this work is merged.

1. **Deploy the license server.** Run **Deploy license server**. It applies
   D1 migration 0008 (`beta_invites`) and ships the `BetaInvites` entrypoint.
   No new secret or variable; `KEY_ENCRYPTION_KEY` must already be set (the
   account page needs it too). Check
   `curl -s https://hobbycoders.com/v1/license/health`.
2. **Set the website variables.** In GitHub, repository variables:
   - `SITE_ACCOUNTS` = `enabled` (the owner page signs in through the hub; the
     account page steps in hobbycoders-cloud/site/README.md, "Account page", apply).
   - `SITE_BETA_INVITES` = `enabled`.
   - `SITE_BETA_OWNER_EMAILS` = the verified email of your hobbycoders.com
     account, comma-separated if more than one.
   - Optional: `SITE_BETA_KEY_DAYS` (default 365),
     `SITE_BETA_ANDROID_GROUP_URL` and `SITE_BETA_PLAY_OPT_IN_URL` (step 6;
     you can add them later and redeploy).
3. **Set the link secret** on the website Worker, once:
   `npx wrangler secret put WAITLIST_LINK_SECRET --name hobbycoders-site`
   with a random value of 32 characters or more (for example the output of
   `openssl rand -base64 48`). Deploys keep it. Without it the owner page
   works but sign-up emails carry no Approve and Decline links.
4. **Turn on key-gated downloads in the hub.** In the `account-hub-production`
   environment set `HUB_LICENSE_BINDING` = `keys` (not `on`, which also turns
   on the tunnel caps) and run **Deploy account hub**. Check that
   `curl -s https://hobbycoders.com/v1/desktop/invite` shows
   `"available":true`.
5. **Deploy the website.** Run **Deploy website**. The generated config adds
   the `BETA_KEYS` binding (`shuffle-license`, entrypoint `BetaInvites`) and
   the beta variables. It refuses to deploy invites without `SITE_ACCOUNTS`.
6. **Android internal testing, once.** Play Console has no API for editing a
   tester list, but a track can use a Google Group as its list, and a group
   can let anyone with the link join:
   1. In Google Groups create a group (for example
      `ai-shuffle-beta@googlegroups.com`). Under **Who can join group**, choose
      **Anyone on the web can join**. Under **Who can view members**, choose
      group managers only.
   2. Play Console, AI Shuffle, **Test and release > Testing > Internal
      testing > Testers**: add the group's email address as a tester list and
      save.
   3. Copy the **opt-in URL** from the same page
      (`https://play.google.com/apps/internaltest/...`).
   4. Set `SITE_BETA_ANDROID_GROUP_URL` to the group's page
      (`https://groups.google.com/g/<group>`, where a signed-in Google user
      presses **Join group**) and `SITE_BETA_PLAY_OPT_IN_URL` to the opt-in
      URL, then run **Deploy website** again.

   While they are unset, the invite email offers only the APK from the
   downloads page. Anyone with the group link can join, so the opt-in URL is
   the only gate on the Play build; the APK still needs a key, and the app
   needs a worker. Remove someone from the group to drop them from the track.
7. **Smoke test.** Join the waitlist from `https://hobbycoders.com/ai-shuffle`
   with an address you control. The support notice should carry Approve and
   Decline links. Open Approve: the page must only show a button. Press it,
   then check the invite email, `/account/waitlist` (approved, sent), the key
   on `/account` after signing in with that address, one download from the
   downloads page, and a Docker pull with the token.

**Off switch:** set `SITE_BETA_INVITES` to anything other than `enabled` (or
delete it) and run **Deploy website**. Links stop working, the owner page and
its API answer 404, and sign-up emails carry no links. Keys already issued
keep working. To stop one tester, revoke their key with the license admin API.

## 1. Blockers for private beta

| Item | Who | Status | Evidence |
| --- | --- | --- | --- |
| License server deployed (minimal: D1, `ADMIN_TOKEN`, `KEY_ENCRYPTION_KEY`, route) | Owner | Done | `/v1/license/health` returns `{"ok":true}` (2026-10-10) |
| Registry deployed (machine account, credential, KV id, environment) | Owner | Done | the retired registry health endpoint returns 200 (2026-10-10) |
| Invites without hand-minted keys: approve on the waitlist, key and links emailed | Owner + Agent | Built; owner turns it on | "Turning on beta invites" above |
| One clean-box install from the docs alone, pull through the registry included | Owner + Agent | Needed | [acceptance-clean-box.md](../internal/acceptance-clean-box.md), 08 section 5 items 1 and 3 |
| A way for invitees to get the desktop installer and APK | Owner + Agent | Built; owner deploys ("Turning on beta invites", step 4) | [#2155](https://github.com/HobbyCoders/ai-shuffle/issues/2155): license-key downloads on the downloads page; needs `HUB_LICENSE_BINDING` = `keys` and a hub deploy |
| Support channel the site points to: Discord invite, or stop pointing to it | Owner | Needed | `/discord` says "opens with the beta"; downloads page says "ask in the Discord" |
| In-app **Report a problem** proven end to end (needs the worker signed in to a hub account) | Owner | Needed | [#1784](https://github.com/HobbyCoders/ai-shuffle/issues/1784) (one real report by mail and to the support inbox); sign-in is area (a) |
| Beta clause in the terms (as-is, may change, data may need a reinstall, founder price) | Owner + Agent | In progress (d) | `https://hobbycoders.com/terms` has no beta wording |
| Privacy page names support reports (logs, kept up to a year, Google mailbox, GitHub support inbox) | Owner + Agent | In progress (d) | `https://hobbycoders.com/privacy` lists GitHub only for release files |
| Docker restart policy so a stalled worker comes back | Agent | In progress (c) | [#2112](https://github.com/HobbyCoders/ai-shuffle/issues/2112) |
| Agent shells can read the install's database and key files: disclosed to users | Agent | Done in this PR | [#1957](https://github.com/HobbyCoders/ai-shuffle/issues/1957); [profiles.md](../content/features/profiles.md#permission-modes) "What a shell can reach". Not a beta blocker for invited, trusted testers once disclosed. |
| Backup and restore commands in the install guide | Agent | Done in this PR | [install-docker-compose.md](../content/getting-started/install-docker-compose.md#back-up) |
| How beta users send logs | Agent | Done | [troubleshooting.md](../content/getting-started/troubleshooting.md) "Sending logs to support" |
| Version and release notes visible to users | Agent | Done (in the app) | Release notes show in the app's update flow; the public changelog page was retired on purpose ([#1494](https://github.com/HobbyCoders/ai-shuffle/issues/1494)) |
| Owner test checklists worked through | Owner | Needed | [#1732](https://github.com/HobbyCoders/ai-shuffle/issues/1732), [#1834](https://github.com/HobbyCoders/ai-shuffle/issues/1834) |

## 2. Before first paid sale

| Item | Who | Status | Evidence |
| --- | --- | --- | --- |
| Accounts, sign-in, Stripe checkout without a trial, pricing page (`SITE_ACCOUNTS`, `SITE_PRICING_PAGE`, Clerk) | Owner + Agent | In progress (a) | `/pricing` 404; `/account` says sign-in opens with the beta; 08 sections 4b, 4e |
| Founder prices in Stripe: $14/month and $140/year (decided); regular $20 and $200 | Owner | Needed | 08 section 4b item 1; [hobbycoders-cloud/license/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/license/README.md) |
| Beta users move to paid: their founder key is their code, checkout with the same email gets the founder price; tell them before beta mode ends | Owner + Agent | Needed | `hobbycoders-cloud/site/src/account/pages.ts` "Beta users: your founder key is your code" |
| End beta mode: lease key pair, `LEASE_PUBLIC_KEYS` filled in a release, beta users enter their key (Settings → System → License) before that release | Owner + Agent | Needed | 08 section 4a item 1 and section 6; `app/core/license.py` `LEASE_PUBLIC_KEYS = {}` |
| Hub license binding, then `LICENSE_GATE`, in that order | Owner | Needed (cap logic in progress (b)) | 08 section 6 |
| Signed Windows installer (Azure Trusted Signing or PFX) | Owner | Needed | No signing secrets in the repository (`gh secret list`); [desktop/RELEASING.md](../../desktop/RELEASING.md) |
| Linux desktop published, or the docs stop calling it a beta | Owner + Agent | Needed | `/v1/desktop/installers` `"linux":null`; [installation.md](../content/getting-started/installation.md) |
| Legal name of the LLC and court venue in the terms; Resend and support processors on the privacy page | Owner | In progress (d) | 08 section 7 |
| Play production: data safety, store listing, closed test if the account is personal | Owner | Needed | [android/RELEASING.md](../../android/RELEASING.md) "Before promoting to production" |
| A written plan for a bad stable release | Owner + Agent | Needed | [#2154](https://github.com/HobbyCoders/ai-shuffle/issues/2154) |
| Old public image still pullable without a license | Owner | Needed (decide: delete or keep) | [#2157](https://github.com/HobbyCoders/ai-shuffle/issues/2157) |
| Export of all chats, or FAQ wording that matches what exists | Agent | Needed | [#2158](https://github.com/HobbyCoders/ai-shuffle/issues/2158) |
| Account deletion URL works | Owner | Done (page live) | `https://hobbycoders.com/delete-account` 200; set it in Play Console |
| In-app updates cover everything after the first install | Agent | In progress (c) | [#1821](https://github.com/HobbyCoders/ai-shuffle/issues/1821) |
| Agent shells cannot read the install's key files (Docker uid split), or the owner accepts the limit | Owner decision + Agent | Needed | [#1957](https://github.com/HobbyCoders/ai-shuffle/issues/1957); [agent-shell-isolation.md](../internal/security/agent-shell-isolation.md) |

## 3. Production hardening

| Item | Who | Status | Evidence |
| --- | --- | --- | --- |
| Uptime alerts for the hub, license server and registry; a status page | Owner + Agent | Agent part done; owner steps needed | [#2156](https://github.com/HobbyCoders/ai-shuffle/issues/2156). Worker `shuffle-uptime` ([hobbycoders-cloud/uptime/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/uptime/README.md)) checks every 5 minutes, emails after 3 failures in a row and once on recovery, and serves a status page on workers.dev. Owner: (1) create the KV namespace `shuffle-uptime`; (2) create a sending-only Resend API key; (3) create the `uptime-production` environment with `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `UPTIME_KV_ID`, `UPTIME_ALERT_EMAIL_TO` and `UPTIME_EMAIL_FROM`; (4) run **Deploy uptime monitor**; (5) `wrangler secret put RESEND_API_KEY` (optionally `ALERT_WEBHOOK_URL`, `HEARTBEAT_URL`); (6) add `license` and `registry` to `UPTIME_SERVICES` once they answer. Optional: custom domain `status.hobbycoders.com`. |
| Hub logs and capacity alerts somewhere the owner looks (Logpush or Workers Logs alerts) | Owner | Needed (agent part done) | 08 section 4c item 5; [hobbycoders-cloud/hub/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/hub/README.md#logs-and-alerts) "Logs and alerts". Owner: set `HUB_WORKERS_LOGS=on` in `account-hub-production` and run **Deploy account hub** (keeps the hub's own lines, not request URLs), check one event for a URL, then read `hub_capacity_alert` in Observability weekly or add a Logpush job without the `Event` field to a destination that alerts. |
| Rate limits on public endpoints | Agent | Done | Hub and license server `[[ratelimits]]` in `wrangler.toml`; registry 300/min per IP, 600/min per token |
| Hub daily cron within Cloudflare limits at scale; cost for 1,000 users | Agent | In progress (g) | 08 section 5 item 6; cost baseline (review in the private archive) |
| Worker hostnames in their own zone | Owner | Needed before scale | 08 section 4c item 6 |
| macOS build, signing and notarization | Owner | Needed (later) | 08 section 3 |
| Physical-device Android check (S9) and live hub smoke (C12) | Owner | Needed | 09 "Remaining work" |
| Rotate the registry machine token before it expires (calendar reminder) | Owner | Needed once deployed | [android/registry/README.md](../../android/registry/README.md) step 3 |

## Re-check of the older lists

**08-owner-actions** (checked 2026-10-09):

| Item | Status now |
| --- | --- |
| 1 Credential model; 2 GitHub settings and environments | Done. Environments exist for hub, license, registry, website, release signing and Play internal. |
| 2 Repository variables `SITE_ACCOUNTS`, `SITE_PRICING_PAGE`, `SITE_DISCORD_INVITE_URL`, `SITE_PLAY_LISTING_URL`, `HUB_LICENSE_BINDING`, `DESKTOP_REQUIRE_SIGNING` | Still needed. Only `SITE_ROUTES`, `SITE_WAITLIST_KV_ID` and `ANDROID_WORKERS_READY` are set. |
| "Round 7 release boundary" (0.8.19 candidate) | Obsolete. Releases through `v0.9.1` are published; `docs/ci/release-channels.md` now says so. |
| 3 Desktop signing | Still needed (Windows before paid sale, macOS later). |
| 3 Play account-deletion URL | Page live; setting it in Play Console not checked. |
| 4a License server | Done for the beta (live 2026-10-10). |
| 4b Stripe | Still needed; area (a). Founder price decided ($14/$140), so 08 section 7 item 1 is closed. |
| 4c Hub: migrations deployed | Done (stable manifest serves 0.9.1). `LICENSE_PUBLIC_KEYS`, `HUB_LICENSE_BINDING`, Clerk user-read and hub logs (Workers Logs or Logpush, section 3) still needed. |
| 4d Registry | Done (live 2026-10-10). |
| 4e Website: routes cut over | Done (`SITE_ROUTES` set; hub paths answer on hobbycoders.com). |
| 4e `www` record | Done (`www` redirects 301 to the apex). |
| 4e Email Routing for support@ | Obsolete as written: the domain's mail is on Google and support@ works; report email goes through Cloudflare Email Sending instead ([#1784](https://github.com/HobbyCoders/ai-shuffle/issues/1784)). |
| 4e Showcase video re-render | Done (#2125 replaced it). |
| 4e `SITE_ACCOUNTS`, `SITE_PRICING_PAGE` | Still needed; area (a). |
| 5 Real-box checks 1 and 3 (registry pull, token revoke) | Still needed, after the registry is deployed. |
| 5 Item 2 Unraid template | Obsolete: the template is no longer in the repository. |
| 5 Items 4 to 7 | Still needed (see sections 2 and 3). |
| 6 Switch order | Still correct. Beta mode stays on for the private beta. |
| 7 Open decisions | Founder price decided; open-source plan D4 replaces the trial with 1 free remote worker. LLC name, refused-worker behaviour, and Linux/mac upload still open; `PUBLIC_TRIALS` is removed. |

**09 "Remaining work"** (checked 2026-10-09):

| Item | Status now |
| --- | --- |
| S9 physical-device acceptance; C12 live smoke | Still needed. |
| Release prerequisites | Partly done (stable channel works); the rest is in sections 1 and 2 above. |
| Open decisions: OpenAI/xAI-only wizard, hub deletion revoking licenses, loop-guard sharing, `host_path`, wizard preselect | Not re-decided; still open. |
| `rateLimit.markStale` dead code | Still there (`frontend/src/lib/stores/rate-limit.ts`, used only by its test). |
| Harbor README uses an OAuth token | Still there (`fluid/benchmarks/harbor/README.md`); a benchmark note, not buyer-facing. |
| "connected OAuth account" comments | Done (none left in `app/`). |
| Unraid template Claude volume description | Obsolete (template removed). |
| Unused hub `/desktop-download.js` | Still served (`hobbycoders-cloud/hub/src/desktop-updates.ts`). |
