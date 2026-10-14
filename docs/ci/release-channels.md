# Release channels and update signing

How a release reaches customers, and the one-time repository settings that
make the guarantees real. Security review finding SEC-06 and decisions D7, D8
and D22 are the background.

## Repository boundary

Hub, license, website, uptime and deploy/withdraw workflows live in the private
[cloud repository](https://github.com/HobbyCoders/hobbycoders-cloud), whose README
lists the environments/secrets/variables the owner must recreate. Finish pending
deploys from the old repository (hub → site → license) before merging the split.

The source publishers remain here deliberately: archive bytes, app revision,
current-main rechecks, release assets and the stable image/buyer-install gates
belong to one app tree/run. Moving just signing or the D1 write would need a
cross-repository artifact/attestation protocol, not a floating checkout. The
exact-tree gate and `update_reach_readiness` remain intact. A pinned Wrangler
CLI in `scripts/source-publisher` removes the dependency on cloud source. Retain
this repository's `account-hub-production` and `release-signing` signing settings;
they are not replaced by the cloud deployment environments. No secret-bearing
source publication workflow runs on a pull request.

## Fresh public history and publication identity

The public root must have the complete tree of the merged private export. The
bridge only verifies that transition for local update-reach attestation; it does
not bypass archive exact-tree verification, signatures, protected-main status,
merged-PR evidence or owner acceptance labels. It never fetches private objects
into the public checkout. The local attester needs a separate complete private
checkout (`AI_SHUFFLE_PRIVATE_CHECKOUT` or the helpers' `--private-checkout`).

After freezing/draining publishers and inventorying the maximum container
sequence across all distributed/withdrawn sources and images, open the final
private export PR. On its clean branch run:

```sh
python scripts/release/prepare_cutover.py --export-pr "$EXPORT_PR" --last-shipped "$MAX_SHIPPED" --last-desktop-sequence "$MAX_DESKTOP_SEQUENCE"
```

Review/commit `release/public-cutover.json` and `release/container-sequence-offset`
in that PR. The mapping pins the private numeric repository identity, export PR,
signed old publication envelope hash/revision and inventory maxima. The desktop
maximum covers all signed source sequences and installer-bundled commit-time
baselines, including withdrawn/historical deliveries. Do not
publish this export from old history. Copy its exact complete tree to the single
public root; retain the signed envelope privately. The offset is committed data,
read from the target revision, never a GitHub variable or environment override.
It is immutable with the mapping for the entire public history epoch.

On the first public release PR, run `python scripts/release/repin_catalog.py
--public-root "$S"`; commit and merge it before a stable tag. The initial root
itself is not eligible for stable. Use `bridge_attestation.py --private-export
"$E" --public-root "$S" --private-checkout "$PRIVATE" --out "$RECEIPT"` for an
audit receipt; `--verify "$RECEIPT"` repeats the live evidence checks, rather than
trusting the receipt as an authorization. Run `cutover_check.py` with those E/S
and checkout arguments plus `--old-repo HobbyCoders/ai-shuffle-private --new-repo
HobbyCoders/ai-shuffle`. Failures are stop points, not bypass switches. The normal
full-suite exact-main attestation remains required after this read-only check.

Every publisher now requires `RELEASE_GITHUB_REPO_ID` (the current publication
repository's numeric GitHub ID); `RELEASE_GITHUB_REPO` defaults to
`HobbyCoders/ai-shuffle`. Configure both in the existing signing environments.
The cloud provenance migration/allowlist must be deployed **first**. All writes
and read-backs bind assets to this identity; retries preserve signed bytes and
cannot move an existing row to a different repository. Withdrawn source rows
remain part of the signing-clock floor. Full-release staging of source rows,
installer pointers and Play artifacts is tracked separately in
[#2335](https://github.com/HobbyCoders/ai-shuffle/issues/2335); Docker candidate
staging is not a claim that these other surfaces are staged.

## The path of a release

`release.yml`, on an owner manual dispatch **from an existing `vX.Y.Z` tag**
(never `main`; pushing the tag does not start this workflow):

1. **preflight** runs read-only checks before any release, image or source is
   published. The tag must match the checkout, its commit must be on `main`,
   `main` must be protected, and the commit must come from a merged PR. It
   also checks version surfaces and all connector source bytes locally, at
   the pinned Git commit and on GitHub. **create-release** then requires the immutable owner's trigger and
   the tags-only `release-signing` environment, makes the GitHub release and
   decides whether this tag
   is the newest final release (`promote_latest`). Legacy milestone tags such
   as `v6-phase2` and prereleases do not participate in that comparison.
2. **docker** (`docker-publish.yml`) builds the image from the tag, runs the
   full container verification (`scripts/verify_container_postgres.py`), and
   only then pushes immutable version and full-SHA tags. Optional `candidate_tag`
   must start with `candidate-`; the builder never moves `latest`, major, minor,
   main or nightly aliases (including metadata-action's automatic latest).
   An existing version tag must be reused, not overwritten: dispatch `release.yml`
   with `image_digest=sha256:...` to verify the image's baked exact revision, full
   Git tree, committed container sequence and capability contract without a
   rebuild. The lifecycle and clean buyer-install checks still run. Only the
   final **promote-image** job, after buyer-install, source signing, Linux release
   attachment and Android APK publication, moves `X.Y`, `X` and `latest` by that
   exact digest and reads them back. Windows must also succeed when requested;
   it may be skipped only when `build_desktop` was not requested. Failed or
   cancelled artifact jobs never permit alias promotion.
3. **buyer-install** pulls that image on a fresh GitHub-hosted runner and
   installs it with `docs/install/compose` exactly as a buyer would
   (`scripts/ci/verify_buyer_compose.py`), then checks the first session
   through the API: `/health`, signed-out requests refused until an owner
   password exists, the password set and used, the beta license, no CLI login
   on a fresh container, and an agent turn refused without a credential. The
   manual half of the clean-box test is `docs/internal/acceptance-clean-box.md`.
4. **source-stable** runs in the protected `release-signing` environment,
   checks that the tag's commit is on `main`, builds the runtime-only source
   archive (`scripts/source_archive.py`), signs a manifest with
   `channel: "stable"`, the version, the annotated tag message as release notes
   and the source tree digest, and points `/v1/desktop/manifest` at it.
   Prereleases (`vX.Y.Z-rc.N`) and older final releases skip this step. The
   publisher independently refuses prereleases and skips an older final tag.
5. **android** builds and publishes to Play internal testing only when its
   existing signing/worker-readiness gates pass. This is not a public Play
   production release. The Windows installer is still opt-in on dispatch.
6. **linux-build** calls the existing Linux workflow on the exact tag; **linux-release**
   verifies its source revision/version, AppImage and deb hashes, and checksum
   file, then attaches both packages, `SHA256SUMS-linux` and provenance to the
   GitHub release with digest/size read-back. The one-day Actions artifact is
   transport only, not the release download location.

Complete release workflows are serialized without cancelling an active
publish. A failed gate stops its dependent jobs; a partial release must not
be described as available on every platform.

Write release notes in an annotated tag, under 4,000 characters. The tag
must point to the merged release PR commit, not an unreviewed follow-up.
Creating/pushing the tag is an **owner-only action after all prerequisites
below**, not part of source preparation. Publication requires a separate owner
dispatch. A retry/optional
installer dispatch must select that existing tag as its workflow ref and
supply the same version; a dispatch from `main` fails preflight, because a
checkout does not change GitHub's tags-only environment authorization.

Accepted nightly Docker candidates have a separate explicit promotion lane:
`python3 scripts/release/promote_image.py --digest sha256:HEX --tags nightly`.
Run it only after that digest's canary acceptance, from a clean checkout of the
current public main. It verifies the pulled image's revision/tree/sequence and
requires the existing owner-authored successful exact-main full-suite status on
protected main. It rechecks main after the image pull and reads the nightly alias
back. Missing/stale/failed evidence refuses promotion; this lane never moves
`latest`, major or minor aliases, and mixed nightly/stable requests are rejected.
The pre-canary cutover check requires Actions disabled and all runs drained in
**all three** repositories: old private app, new public app and cloud. Enable the
public candidate publisher only after that checkpoint as the runbook describes.

The **nightly** source update is scheduled once a day at **06:00 UTC** from
protected `main`, with an owner-only manual dispatch for retries. Pushes and
status events do not publish. Before setting up dependencies, the workflow
compares main's revision with the published manifest and exits if unchanged.

Publication still requires the owner's successful `local/full-suite` commit
status on that exact current main revision, posted by
`python scripts/local_ci.py full --attest` on a clean checkout. GitHub Actions
runs no tests. A pass on an older revision, a pending or failed status, or a
successful-but-skipped publication workflow is not permission to publish.
If the pass is missing, the previous nightly stays available.

Only the current `main` revision can publish. The read-only gate runs before
signing secrets are available and is repeated immediately before publication.
The publisher checks `main` again before writing the signed pointer. These
checks use trusted main code, not PR artifacts or a PR checkout.

Stable releases still require an owner-created release tag. Existing workers
stay on their selected channel: publication never switches, installs or restarts
them. Docker accepts `UPDATE_CHANNEL=stable|nightly`; desktop accepts
`{"channel":"nightly"}` in `updates/update-channel.json` under its user-data
directory. Both default to stable. Manual `docker-publish.yml` runs on `main`
produce the `nightly` image tag. Returning from nightly to stable needs a
reinstall, because database migrations do not run backwards.

### Two-step edge transition

1. Keep `SOURCE_CHANNEL: edge` in `desktop-source.yml` and publish this
   compatibility build first. Existing edge workers reject a manifest signed
   nightly until they have installed code that recognizes both names. Deploy
   the hub aliases before updated clients request `/v1/desktop/manifest/nightly`.
   Older Docker images without `source-supervisor` also need a compatible
   image: a source-only update cannot replace their supervisor. Images with
   that capability hand over to the updated supervisor after a restart.
2. After the affected workers have that code, change the workflow's one
   `SOURCE_CHANNEL` value to `nightly`. This PR deliberately leaves it `edge`.
   `/v1/desktop/manifest/nightly` and `/v1/desktop/manifest/edge` serve identical
   signed bytes from the existing D1 `edge` lane; the publisher keeps that
   storage key even when it signs `nightly`. The hub never rewrites a signature.

For one release, `edge` settings normalize to `nightly` with a deprecation log.
New clients accept either signed name on that lane, but stable still accepts
only stable. Remove the alias only after the compatibility release has reached
workers that used it.

## Cutting a release

This section was written for the 0.8.19 candidate, the first protected stable
release. Releases have been published since (`v0.9.1` was the newest tag on
2026-10-09), so read `0.8.19` below as the version you are cutting. Release
builds derive their version from the actual tag; the source fallbacks,
Compose pins and their website/in-app doc copies only need to agree with it,
which preflight checks. Re-check tags immediately before release and choose
another version if one has been used. Do not update public install assets
ahead of the image.

### Executable source checks (safe before owner setup)

From a full-history checkout, with Git, authenticated `gh`, Python 3.12 and
Node 22 available:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt Pillow
PYTHONPATH="$PWD:$PWD/fluid" .venv/bin/python scripts/ci/release_preflight.py --remote-catalog
PYTHONPATH="$PWD:$PWD/fluid" .venv/bin/python -m pytest -o addopts="" tests/core/test_install_files.py tests/core/test_version_source.py tests/test_connector_catalog.py tests/test_connector_packages.py tests/core/test_release_download_instructions.py tests/core/test_android_release_gate.py tests/core/test_source_archive.py tests/test_buyer_docs.py tests/scripts/test_release_preflight.py
(cd frontend && npm ci && npm run check && npm run build)
(cd desktop && npm ci --ignore-scripts && npm test)
```

Website checks run separately in `HobbyCoders/hobbycoders-cloud` with npm 11
or newer; its README explains refreshing docs/install snapshots from this app
checkout via `AI_SHUFFLE_CHECKOUT`. Install dependencies inside each checkout;
never copy `node_modules` or `.env`. Run app tests and `fluid/tests` separately.
The source check without `--tag` cannot prove release ancestry, GitHub gates,
signing credentials, deployment access or hardware behavior. A shallow clone
must fetch history containing the catalog pin before running it.

The connector pin is `4c5b610b50f7953ea4a1d93f558b3f191d795991`. The preflight
checks all 28 listed files against that Git commit and, with
`--remote-catalog`, GitHub's authenticated raw contents API. Local digest
agreement alone is insufficient. After any catalog change, set `release` to
the first 16 hex characters of SHA-256 of the catalog **without `release`**,
encoded as JSON with sorted keys and separators `(',', ':')`. This makes an
installed older package take the update path instead of accepting its old
spec under the same release ID. The digest test and preflight enforce it.

### Owner prerequisites (not satisfied by the checks above)

On 2026-09-25, owner-approved main/tag rules and deployment-ref restrictions
were configured and read back. `main.protected=true`; `release-signing` accepts
only `v*` tags. This private repository is on GitHub Team, which does not support
native required-reviewer environments. The owner chose explicit owner-triggered
production workflows instead of upgrading billing. This is **not** a native
reviewer gate: anyone trusted to change protected workflow code must be trusted
with this policy. Secrets were not read, moved or changed.

Cloud deployment jobs live in `HobbyCoders/hobbycoders-cloud`; app publication
jobs remain in `HobbyCoders/ai-shuffle`. Both require the immutable owner actor
ID configured in the workflows and the same triggering actor on reruns. Hub,
license, website and uptime accept only manual dispatch from `main`; a merge
never deploys them. Nightly source publication is the narrow scheduled
exception, subject to the exact-tree CI gate above; pushes do not publish.
Its manual retry remains owner-only. This exception does not grant automatic
stable releases, Play uploads, worker installation, or restarts.
The stable release workflow accepts only an explicit owner dispatch from an
existing `v*` tag. Reusable signing/publishing workflows also enforce
the owner identity and protected ref; another maintainer cannot rerun an owner's
old deployment to obtain its authority. Existing worker-readiness and publish
opt-ins remain required. CI/unsigned builds stay automatic. Changes take effect
when the reviewed workflow revision reaches `main`; run nothing before then.

- Merge the reviewed candidate through protected `main`, with the applicable
  suites green on the PR head. Confirm required review, no bot bypass, no
  force pushes/deletions and owner-only `v*` tag rules in GitHub settings. Preflight checks protection is enabled
  and the merged PR; it does not audit every ruleset setting.
- Configure the `release-signing` environment below. Both release creation
  and stable signing require an owner trigger and its tag-ref restriction. Before any
  publication, the creation job checks the presence of its Cloudflare values,
  D1 ID and signing key and tests the private key against
  `desktop/build/application-update-key.pem`. Presence is not proof of token
  permissions or a deployed/migrated D1 database. Never print those secrets.
- Deploy/migrate the hub and license server and configure their bindings/keys.
  The owner must make `ghcr.io/hobbycoders/ai-shuffle-server` public, then test
  an anonymous version-pinned pull. No digest is supplied by source preparation:
  record the **actual** published image digest after the build and pull succeed.
- Complete `desktop/RELEASING.md` signing/notarization and first-run checks.
  Confirm Play signing continuity, internal tester access and
  `ANDROID_WORKERS_READY=true` only after compatible workers are verified.
  Desktop/Play artifacts or approval are not implied by a Docker success.
- Confirm commercial terms, legal/vendor identity, support destinations and
  license enforcement rollout with the owner; do not fill them with guessed
  values. Finish the real-box matrix in
  `docs/internal/acceptance-clean-box.md` before calling the candidate sale-ready.
- Official connector source snapshots ship with the app and update archive;
  buyers no longer need a GitHub account or private repository access. Source
  digests and bounds are still enforced, and dependency installation still
  needs PyPI. Repeat clean-buyer install/update acceptance on the final artifact;
  do not confuse the owner's remote catalog check with that buyer path.

Only after those prerequisites may the owner create/push the annotated
`v0.8.19` tag at the merged PR commit. On that tagged checkout, the equivalent
read-only gate is:

```sh
PYTHONPATH="$PWD:$PWD/fluid" .venv/bin/python scripts/ci/release_preflight.py --tag v0.8.19 --github --remote-catalog
```

If any job fails, keep sales/CA publication paused, preserve sanitized build
and buyer-install evidence, and diagnose the failing gate. Do not retag a
published commit, substitute an unsigned manifest or bypass verification.
A fixes-only follow-up needs a new reviewed patch version when artifacts
have already been published. Re-running from an existing tag is for an
unchanged retry, not a way to replace its source.

### Draft buyer release notes for 0.8.19 (historical)

AI Shuffle 0.8.19 prepares the first protected stable release channel:
version-pinned Compose install files, verified source updates,
owner sign-in, a guided setup with license and credential checks, and the
stock CLI's own sign-in flow. Provider usage is billed by your provider;
an AI Shuffle license is not a provider subscription. Back up persistent
data before updating and keep existing volume mappings. Desktop signing,
Play track availability, registry access and real-box acceptance must be
confirmed in the final notes; do not describe pending artifacts as downloads.

## What the worker checks

`docker/application_updates.py` (inside the image, so an update cannot change
it) accepts a manifest only if the Ed25519 signature verifies against the key
baked into the image, the signed channel equals the worker's channel, a tree
digest is present, and the container sequence and runtime fingerprint fit the
image. It checks the archive's size and SHA-256, recomputes the tree digest
while extracting and refuses to build on a mismatch, installs Python packages
only from the hash-pinned `requirements.lock` (`--require-hashes`), and
re-hashes every source file against the signed listing before every launch.

Not covered: build outputs (venv, `node_modules`, `dist`, `app/static`) are
produced on the worker and are not in the signed listing. A separate uid for
agent tools (SEC-04) is the fix for those. Node packages are pinned by their
lockfiles' integrity hashes through `npm ci`.

The desktop updater (`desktop/src/main/source-updates.ts`) follows the same
rules: it accepts only a manifest signed for its channel (`stable` unless the
owner writes `{"channel": "nightly"}` to `update-channel.json` in the install's
`updates` folder) that carries a tree digest, keeps the signature, size,
checksum and sequence checks, and recomputes the tree digest over the
extracted files before building. Its build (`desktop/src/main/source-build.ts`)
and the bundled runtime (`desktop/scripts/build-worker.mjs`) install Python
packages only from `requirements.lock` and `requirements-native.lock`, both
with `--require-hashes`, then fluid with `--no-deps --no-build-isolation`.
`scripts/lock_requirements.sh` regenerates both locks; the native lock is
constrained by `requirements.lock` so a shared package never pins differently.

Optional Cloud lease: when its own worker has a lease, the desktop adds it as
`X-Shuffle-Lease` to the manifest and archive requests, read from the worker's
token-protected `GET /api/v1/internal/desktop/source-lease`. The header is
attached only after the URL is checked to be a hub update endpoint, and
redirects are refused. Without a lease, requests go out without that header.
Downloads, local use and updates require no account, license key or lease;
the Cloud lease controls remote-worker capacity, not access to updates.

Release notes: the desktop's update state carries the manifest's `version`
(as `release`) and `notes`, and its "Update ready" notification names the
release and summarizes the notes. The sidebar update button shows the full
notes for any worker whose status carries them: container workers today, and
desktop workers once `DesktopUpdateState` in `app/api/internal.py` accepts
`release` and `notes` (it currently drops fields it does not declare).

## A bad stable release

Stable workers are offered the newest stable release the hub has. A release
cannot be rolled back on a worker: its database migrations have already run,
and they do not run backwards. So a bad release is never undone by sending
workers an older version. You **withdraw** it, so that nobody else is offered
it, and then ship a **forward fix**, a new patch release.

### 1. Stop the spread: withdraw it

In **HobbyCoders/hobbycoders-cloud**, run **Actions → Withdraw stable release**
on `main` with the version (`0.9.2`,
no `v`), action `withdraw` and a one-line reason. Owner only. The job summary
says what the feed serves now. Without GitHub Actions, the same call:

```sh
curl -s -X POST https://hobbycoders.com/v1/releases/stable/0.9.2 \
  -H "Authorization: Bearer $RELEASE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"withdrawn":true,"reason":"Sessions fail to load after the migration"}'
```

Check it with `GET https://hobbycoders.com/v1/releases/stable` (same header).
It lists the stable releases with their `withdrawn` state, plus `serving`, the
release the feed offers now, and `held`, which says why the feed offers
nothing. The call works while the hub is read-only (`HUB_MODE=readonly`). If
the hub itself is down, run the same change directly on D1 with the publish
path's Cloudflare token:

```sh
npx wrangler d1 execute DB --remote --config <deploy config> --command \
  "UPDATE source_releases SET withdrawn_at=strftime('%s','now')*1000, withdrawn_reason='...' WHERE channel='stable' AND version='0.9.2'"
```

What the hub does (`hobbycoders-cloud/hub/src/release-admin.ts`, `servedRelease` in
`hobbycoders-cloud/hub/src/desktop-updates.ts`, migration 0027):

- `/v1/desktop/manifest` stops serving the withdrawn release. Its source
  archive is no longer served, and installers of that version are hidden from
  `/v1/desktop/installers` and the website's download page. Their download
  links return 404.
- A worker on an **older** version is offered the newest release that is not
  withdrawn, which is a release it already has or one before the bad one. It
  is never offered the bad one.
- A worker **already on** the bad release is offered that older release too,
  but it never installs it. Docker workers compare the signed main commit
  count and desktop workers the signed sequence, and both treat a lower one as
  "You are up to date" (`tests/test_application_updates.py`,
  `test_withdrawn_release_fallback_is_never_applied_backwards`). It stays on
  the bad release until the fix ships.
- There is one exception. A desktop installed from the bad release's own
  installer compares against the installer's build time, not a signing time,
  so an older release signed after it would look newer. So while any desktop
  installer newer than the fallback is published, the feed serves **nothing**.
  The manifest returns 404 "The newest release was withdrawn", workers keep
  what they run, and `held` names the installer. This is the usual case,
  because every release publishes installers.
- Edge is not affected.
- Re-running `release.yml` for a withdrawn tag does not bring it back. The
  publisher's upsert leaves the withdrawal as it is.

What it cannot do: a worker that **already downloaded** the bad release shows
"Update ready" and installs it when someone chooses **Update and restart**.
Nothing on the hub can recall that download. Tell users not to choose it
(step 3).

### 2. Ship the forward fix

1. Fix it on a branch and merge the PR the usual way, with CI green.
   Typically 30 to 90 minutes, depending on the fix and CI queue.
2. Cut the next patch tag (`v0.9.3`) from that merge commit
   ([Cutting a release](#cutting-a-release)). Pushing the tag does not publish.
3. The owner dispatches `release.yml` with that tag as **both** the workflow
   ref and the `version` input, for example:
   `gh workflow run release.yml --repo HobbyCoders/ai-shuffle --ref v0.9.3 -f version=v0.9.3`.
   This is an owner publication action, not part of preparing the source fix.
   The release run has taken 10 to 15 minutes, and the optional installer run
   another 10 to 15.
4. Verify the run's required jobs actually completed (not skipped), the public
   image's clean-buyer install, and the signed stable manifest/feed for the
   patched version. Check that an installed worker is offered the fix before
   declaring it live; a tag or green workflow alone is not publication proof.
   Once published, the newest fix replaces the withdrawn release in the feed.
   Workers on the bad release and on older ones are both offered it. Docker
   and desktop workers check about once a day, or at once from the update button.

Realistically: withdraw within minutes of the report, and the fix is
available one to two hours later. Leave the bad release withdrawn after that.
There is no reason to un-withdraw it.

### 3. Meanwhile, for users

- **Docker, not yet updated:** keep `AI_SHUFFLE_VERSION` in `.env` at the
  version you run, and do not choose **Update and restart** while the update
  button shows the bad version. **Settings → General → Allow updates** turns
  in-app updates off entirely until the fix ships.
- **Docker, already on the bad release:** wait for the fix. Never set
  `AI_SHUFFLE_VERSION` back to an older image after a newer one has run,
  because the database has already migrated. If the image itself is bad (not
  just the in-app update), the fix ships as a new image tag, and `X.Y`, `X`
  and `latest` move to it with the release.
- **Desktop:** the same. Do not choose **Update and restart** for the bad
  version. A desktop already on it waits for the fix. Do not reinstall an
  older installer over it.
- **Android (Play internal testing):** the app loads its UI from the worker
  it connects to, so a bad worker release reaches phones only through the
  worker, and the worker steps above cover it. A bad Play build cannot go
  back, because Play refuses a lower `versionCode`. Ship a fixed build with a
  higher `versionCode` (`android-play.yml`); until then, tell testers not to
  update the app from Play.
- **Telling users:** post in Discord and email the affected testers with what
  is wrong, "do not choose Update and restart for 0.9.2", and when the fix is
  expected. If the bug produces a support fingerprint, mark it with
  `SUPPORT_ADMIN_TOKEN` (`hobbycoders-cloud/hub/README.md`, "Support reports") so
  everyone who hits it sees the note in the app. Name the withdrawn version in
  the fix's release notes.

### Un-withdraw

Withdrawing by mistake is undone with the same workflow and action `restore`,
or `{"withdrawn":false}` in the POST. The release keeps its original signed
sequence, so if it is still the newest, the feed serves it again at once; if
a fix has shipped since, the fix stays served.

## One-time steps for the owner

1. **Protect `main`.** Settings → Rules → Rulesets → New branch ruleset for
   the default branch: require a pull request with at least one approval,
   block force pushes and deletions. Give bypass only to yourself, not to bot identities.
2. **Protect release tags.** New tag ruleset for `v*`: restrict creation,
   update and deletion to yourself (repository admin), so only you can cut a
   release.
3. **Create the `release-signing` environment.** Settings → Environments →
   New environment `release-signing`:
   - On this private Team repository, required reviewers are unavailable.
     The owner explicitly chose immutable-actor/rerun checks in the protected
     workflows. Do not claim the environment itself requires approval. If
     Enterprise is adopted later, add native reviewers as an additional gate.
   - Deployment branches and tags: **Selected branches and tags**, add the tag
     pattern `v*` only.
   - Secrets: `DESKTOP_APPLICATION_SIGNING_KEY`, `CLOUDFLARE_API_TOKEN`,
     `CLOUDFLARE_ACCOUNT_ID` (the same values `account-hub-production` has
     today). Variable: `ACCOUNT_HUB_DATABASE_ID`. Optional, for installer
     copies in R2: `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` with the
     repository variable `R2_INSTALLER_BUCKET`
     ([hobbycoders-cloud/hub/README.md](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/hub/README.md#installer-downloads-from-r2)).
4. **Keep nightly signing separate from stable authorization.**
   `desktop-source.yml` uses the key in `account-hub-production` only after the
   exact-main local attestation gate. Stable publishing uses the protected
   release environment. Current stable workers reject nightly and transition
   edge manifests; very old clients that predate channel checks need upgrading.
   Changing the trust key requires an installer/image rollout, not just a hub
   or workflow edit.
5. **Make `ghcr.io/hobbycoders/ai-shuffle-server` public** (owner action).
   The app no longer uses a private registry; the old-image issue #2157 is moot.
6. **Deploy the hub** so migration 0018 runs. Until the first stable release
   is signed, installed apps get a 404 from the update check.
7. **Set up withdrawal** ([A bad stable release](#a-bad-stable-release))
   before you need it. Generate a token (`openssl rand -hex 32`) and set it
   as the Worker secret `RELEASE_ADMIN_TOKEN`
   (`npx wrangler secret put RELEASE_ADMIN_TOKEN` in `hobbycoders-cloud/hub`) and as an
   `account-hub-production` environment secret of the same name. Then deploy
   the hub so migration 0027 runs. Until then the endpoint answers 503 and the
   workflow fails.
