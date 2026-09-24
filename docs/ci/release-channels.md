# Release channels and update signing

How a release reaches customers, and the one-time repository settings that
make the guarantees real. Security review finding SEC-06 and decisions D7, D8
and D22 are the background.

## The path of a release

`release.yml`, on an existing `vX.Y.Z` tag push or a manual dispatch **from
that same tag** (never `main`):

1. **preflight** runs read-only checks before any release, image or source is
   published. The tag must match the checkout, its commit must be on `main`,
   `main` must be protected, and the merged PR for that commit must have a
   successful **Merge gate** on its head SHA. It also checks version surfaces
   and all connector source bytes locally, at the pinned Git commit and on
   GitHub. **create-release** then waits for the owner's `release-signing`
   environment approval, makes the GitHub release and decides whether this tag
   is the newest final release (`promote_latest`). Legacy milestone tags such
   as `v6-phase2` and prereleases do not participate in that comparison.
2. **docker** (`docker-publish.yml`) builds the image from the tag, runs the
   full container verification (`scripts/verify_container_postgres.py`), and
   only then pushes `ghcr.io/hobbycoders/ai-shuffle-server:X.Y.Z`, plus `X.Y`,
   `X` and `latest` when promoted.
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

Complete release workflows are serialized without cancelling an active
publish. A failed gate stops its dependent jobs; a partial release must not
be described as available on every platform.

Write release notes in an annotated tag, under 4,000 characters. The tag
must point to the merged release PR commit, not an unreviewed follow-up.
Creating/pushing it starts publication and is an **owner-only action after
all prerequisites below**, not part of source preparation. A retry/optional
installer dispatch must select that existing tag as its workflow ref and
supply the same version; a dispatch from `main` fails preflight, because a
checkout does not change GitHub's tags-only environment authorization.

Every push to `main` still publishes an **edge** source update
(`desktop-source.yml` → `publish-source.mjs` with the default
`SOURCE_CHANNEL=edge`) at `/v1/desktop/manifest/edge`, signed with
`channel: "edge"`. Customer containers only accept `stable`; set
`UPDATE_CHANNEL=edge` on your own workers to follow main. Manual
`docker-publish.yml` runs on `main` produce the `edge` image tag.

## Prepared candidate: 0.8.19 (not released)

`v0.8.18` was the newest final tag when this candidate was prepared. The
source fallbacks, package and lockfile root versions, Android fallback,
Compose pins, Unraid XML, and their website/in-app doc copies now say
`0.8.19`. Release builds still derive their version from the actual tag;
changing these literals does not create an image, installer or stable feed.
Re-check tags immediately before release and choose another version if this
one has been used. Do not update public install assets ahead of the image.

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
(cd android/site && npm ci && npm run sync-install && npm run render-docs && npm run typecheck && npm test)
```

The site requires npm 11 or newer. Install dependencies inside this checkout;
never copy `node_modules` or `.env`. Run app tests and `fluid/tests` separately.
The source check without `--tag` cannot prove release ancestry, GitHub gates,
signing credentials, deployment access or hardware behavior. A shallow clone
must fetch history containing the catalog pin before running it.

The connector pin is `06fac67853c2907e4e6fb2eac7e171d63df035d8`. The preflight
checks all 28 listed files against that Git commit and, with
`--remote-catalog`, GitHub's authenticated raw contents API. Local digest
agreement alone is insufficient. After any catalog change, set `release` to
the first 16 hex characters of SHA-256 of the catalog **without `release`**,
encoded as JSON with sorted keys and separators `(',', ':')`. This makes an
installed older package take the update path instead of accepting its old
spec under the same release ID. The digest test and preflight enforce it.

### Owner prerequisites (not satisfied by the checks above)

Preparation-time read-only GitHub checks reported `main.protected=false` and
only `account-hub-production` / `google-play-internal` in the environment
list: `release-signing` was absent. This is a blocker, not a successful
release-gate check. Re-read those settings after the owner configures them;
no secret values were inspected or configured during source preparation.

- Merge the reviewed candidate through protected `main`, with the applicable
  suites and **Merge gate** green on the PR head. Confirm required review,
  required status, no bot bypass, no force pushes/deletions and owner-only
  `v*` tag rules in GitHub settings. Preflight checks protection is enabled
  and the merged PR's gate; it does not audit every ruleset setting.
- Configure the `release-signing` environment below. Both release creation
  and stable signing require its approval from a tag ref. Before any
  publication, the creation job checks the presence of its Cloudflare values,
  D1 ID and signing key and tests the private key against
  `desktop/build/application-update-key.pem`. Presence is not proof of token
  permissions or a deployed/migrated D1 database. Never print those secrets.
- Deploy/migrate the hub, license server and registry, configure their
  bindings/keys and private GHCR access, and test the licensed pull path.
  `docs/install/registry.md` is the registry runbook. No digest is supplied by
  source preparation: record the **actual** published image digest only after
  the build and registry verification succeed.
- Complete `desktop/RELEASING.md` signing/notarization and first-run checks.
  Confirm Play signing continuity, internal tester access and
  `ANDROID_WORKERS_READY=true` only after compatible workers are verified.
  Desktop/Play artifacts or approval are not implied by a Docker success.
- Confirm commercial terms, legal/vendor identity, support destinations and
  license enforcement rollout with the owner; do not fill them with guessed
  values. Finish the real-box matrix in
  `docs/internal/acceptance-clean-box.md` before calling the candidate sale-ready.
- Connector package installs still fall back to `gh api` with the user's
  GitHub sign-in for the private source repository
  (`app/core/connector_packages.py`). An AI Shuffle license does not grant
  repository read access. The pin fix does **not** solve ordinary buyers'
  connector distribution; resolve that separate distribution blocker before
  advertising those plugin installs to buyers.

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

### Draft buyer release notes (use only after release gates pass)

AI Shuffle 0.8.19 prepares the first protected stable release channel:
version-pinned Compose and Unraid install files, verified source updates,
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
owner writes `{"channel": "edge"}` to `update-channel.json` in the install's
`updates` folder) that carries a tree digest, keeps the signature, size,
checksum and sequence checks, and recomputes the tree digest over the
extracted files before building. Its build (`desktop/src/main/source-build.ts`)
and the bundled runtime (`desktop/scripts/build-worker.mjs`) install Python
packages only from `requirements.lock` and `requirements-native.lock`, both
with `--require-hashes`, then fluid with `--no-deps --no-build-isolation`.
`scripts/lock_requirements.sh` regenerates both locks; the native lock is
constrained by `requirements.lock` so a shared package never pins differently.

License lease (D8): when its own worker has a lease, the desktop adds it as
`X-Shuffle-Lease` to the manifest and archive requests, read from the worker's
token-protected `GET /api/v1/internal/desktop/source-lease`. The header is
attached only after the URL is checked to be a hub update endpoint, and
redirects are refused. Without a lease (beta mode, an attached or older
worker, first launch before any worker runs) requests go out as before.

Release notes: the desktop's update state carries the manifest's `version`
(as `release`) and `notes`, and its "Update ready" notification names the
release and summarizes the notes. The sidebar update button shows the full
notes for any worker whose status carries them: container workers today, and
desktop workers once `DesktopUpdateState` in `app/api/internal.py` accepts
`release` and `notes` (it currently drops fields it does not declare).

## One-time steps for the owner

1. **Protect `main`.** Settings → Rules → Rulesets → New branch ruleset for
   the default branch: require a pull request with at least one approval,
   require the **Merge gate** status (docs/ci/merge-gate.md), block force
   pushes and deletions. Give bypass only to yourself, not to bot identities.
2. **Protect release tags.** New tag ruleset for `v*`: restrict creation,
   update and deletion to yourself (repository admin), so only you can cut a
   release.
3. **Create the `release-signing` environment.** Settings → Environments →
   New environment `release-signing`:
   - Required reviewers: yourself. Every stable signing then waits for your
     click.
   - Deployment branches and tags: **Selected branches and tags**, add the tag
     pattern `v*` only.
   - Secrets: `DESKTOP_APPLICATION_SIGNING_KEY`, `CLOUDFLARE_API_TOKEN`,
     `CLOUDFLARE_ACCOUNT_ID` (the same values `account-hub-production` has
     today). Variable: `ACCOUNT_HUB_DATABASE_ID`.
4. **Decide what edge signing keeps.** `desktop-source.yml` still signs edge
   builds with the same key from `account-hub-production`. Customer workers
   reject edge manifests, but desktop builds and images already in the field
   do not check the channel. The strongest setup is to remove
   `DESKTOP_APPLICATION_SIGNING_KEY` from `account-hub-production`, which
   stops edge publishing (the `desktop-source.yml` run then fails on the
   missing key; that workflow belongs to the desktop work and should then be
   switched off or given a separate edge key that customer builds do not
   trust).
5. **Make the image package private** and handle the frozen public one:
   see `docs/install/registry.md`.
6. **Deploy the hub** so migration 0018 runs. Until the first stable release
   is signed, installed apps get a 404 from the update check.
