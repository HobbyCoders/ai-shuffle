# Release channels and update signing

How a release reaches customers, and the one-time repository settings that
make the guarantees real. Security review finding SEC-06 and decisions D7, D8
and D22 are the background.

## The path of a release

`release.yml`, on a `vX.Y.Z` tag push or a manual dispatch from `main`:

1. **create-release** makes the GitHub release and decides whether this tag is
   the newest final release (`promote_latest`).
2. **docker** (`docker-publish.yml`) builds the image from the tag, runs the
   full container verification (`scripts/verify_container_postgres.py`), and
   only then pushes `ghcr.io/hobbycoders/ai-shuffle-server:X.Y.Z`, plus `X.Y`,
   `X` and `latest` when promoted.
3. **buyer-install** pulls that image on a fresh GitHub-hosted runner and
   installs it with `docs/install/compose` exactly as a buyer would
   (`scripts/ci/verify_buyer_compose.py`).
4. **source-stable** runs in the protected `release-signing` environment,
   checks that the tag's commit is on `main`, builds the runtime-only source
   archive (`scripts/source_archive.py`), signs a manifest with
   `channel: "stable"`, the version, the annotated tag message as release notes
   and the source tree digest, and points `/v1/desktop/manifest` at it.
   Prereleases (`vX.Y.Z-rc.N`) skip this step.
5. **android** continues as before.

Write release notes in the annotated tag:
`git tag -a v0.9.0 -F notes.md && git push origin v0.9.0`. Keep them under
4,000 characters; they appear beside the in-app update button.

Every push to `main` still publishes an **edge** source update
(`desktop-source.yml` → `publish-source.mjs` with the default
`SOURCE_CHANNEL=edge`) at `/v1/desktop/manifest/edge`, signed with
`channel: "edge"`. Customer containers only accept `stable`; set
`UPDATE_CHANNEL=edge` on your own workers to follow main. Manual
`docker-publish.yml` runs on `main` produce the `edge` image tag.

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
extracted files before building. Its build still installs from
`requirements.txt`; that belongs to `desktop/src/main/source-build.ts`.

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
