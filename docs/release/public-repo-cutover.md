# Public repository cutover: updates, release assets and private support

Status: **decided cutover procedure; execution is gated on the implementation
and rehearsal below, not authorized by this document**. App baseline:
`6b02951bd7dc517ce7fccb97ea85bccacba7fbf9`; cloud inspection baseline:
`723d71ad807046523932759a7d5b49aac874ae01`. Historical line references in the
failure inventory describe the pre-bridge implementation, not future scripts.
This supersedes the design in draft PR #2334. No rename, publication or deploy
is performed by this docs-only change.

This refines D3 and Phase 4 of [the open-source plan](open-source-plan.md)
and the three [#2290 blockers](https://github.com/HobbyCoders/ai-shuffle/issues/2290#issuecomment-6118472266).
The existing repository stays private and becomes `HobbyCoders/ai-shuffle-private`;
`HobbyCoders/ai-shuffle` starts with **one initial commit, no imported ancestors**.
Normal reviewed PRs follow that initial commit. Developer checkouts are re-cloned;
installed workers must not need a reinstall merely because Git history changed.

## Decisions and boundaries

Keep the same update origin, paths, signing key, manifest format, channel lanes
and GHCR package. Preserve old release assets in the renamed private repository.
Add an explicit, narrowly verified old-history → public-root mapping to the
**attestation reach check**, a shared monotonic container sequence calculation,
and per-asset repository provenance in the private hub. Publishers stay with the
app source and exact-main gate. Keep support pointed at
the same private repository identity, under its new name.

This PR changes documentation only. A sequence offset in isolation is not a
working migration: it leaves the history gate blocked, can disagree with image
metadata, and has no safe value until publication is frozen. Likewise a single
hub repository setting cannot serve old and new asset IDs together. Implement
the coupled changes below in reviewed app/cloud follow-ups before renaming.
No arbitrary floor, bypass switch, credentials or production mutation is included.

Do not relax signature, signed-tree, channel, capability, downgrade, protected
main, owner-status, merged-PR or update-reach checks. Do not turn an unknown old
revision into an empty diff, clear a worker's update state, reset its database
floor, switch stable users to nightly, or force-push old history into the new repo.

## A. Update continuity across the new history

### Exact paths that fail today

| Path at the baseline | Failure or invariant |
| --- | --- |
| `scripts/ci/edge_publication_gate.py:108-171`, especially `138-140` | `update_reach_readiness` refuses when the verified published edge/nightly revision is not on the candidate's first-parent history. This is the ancestry/reach part of the gate, not the archive tree digest check. |
| `scripts/local_ci.py:578-620` | `full --attest` calls that check before tests/status pending and again before status success. A new clone cannot attest while the old pointer is outside its history. Copying a status from the old SHA is not a solution. |
| `scripts/ci/edge_publication_gate.py:190-205` | Publication separately requires an owner-created success on the exact current protected-main SHA and rechecks main. Keep this unchanged. |
| `scripts/ci/nightly_changed.py:17-30`; `.github/workflows/desktop-source.yml:33-45,71-99` | The cheap comparison sees the new SHA as changed, but cannot authorize it. The publisher still needs the exact-main attestation. A green workflow with a skipped publish is not evidence of a release. |
| `desktop/scripts/publish-source.mjs:123-138` | `container.sequence` is the first-parent count; a count ≤1 or shallow history is refused. The separate top-level `sequence` is `Date.now()`. |
| `.github/workflows/docker-publish.yml:106-112,178`; `docker/update_contract.py:93-97` | Images also record the raw first-parent count through `APPLICATION_SEQUENCE`. Changing only the source publisher leaves image and source chronology inconsistent. |
| `docker/application_updates.py:651-704` | `check` rejects lower/equal container sequences at `669-672`, verifies capabilities at `673-677`, and verifies archive/tree bytes at `697-704`. Signature/format validation is at `118-150`; launch tree verification at `88-109,501-509`. Workers do not compare Git ancestry. |
| `docker/application_updates.py:206-212,345-354` | Image-versus-installed-source comparisons also depend on the same sequence scale, including staged updates and supervisor handover. |
| `desktop/src/main/source-updates.ts:113-139,226-239` | Desktop uses the top-level sequence and database migration floors, plus signature/channel/tree validation. Do not replace its time-based sequence with the container count. |
| `scripts/ci/release_preflight.py:93-110`; `.github/workflows/release.yml:73-77,269-293` | Stable needs a tag on main from a merged PR, then verified image and buyer install. The new root has no merged public PR: cut stable from a subsequent reviewed release PR, not the initial commit. |

`app/core/application_updates.py:68-110,203-218` is the app's launcher/control
bridge, not the signature or sequence authority. `app/network/hub.py:83-91`
calls the account hub's `network` API for membership; it does not choose the
GitHub release repository. Neither needs a rename-specific client setting.

### Required implementation (lands in a separate reviewed change)

**One shared sequence helper and one committed offset.** Both the source
publisher and image builder use `scripts/release/sequence_offset.py`, reading
`release/container-sequence-offset` from the target revision, not the caller's
working copy. The old history uses zero. After freezing all publication,
inventory `M`, the maximum container sequence ever distributed: stable/nightly,
withdrawn releases, installers' bundled images and images without a source-feed
row. The current pointers alone are insufficient. Keep the evidence privately.

`sequence_offset.py compute --last-shipped "$M"` prints the offset that makes
the first public publication exceed `M`. Commit it in the final export PR.
Sequence = committed offset + first-parent count. The public root therefore
exceeds the old maximum, and subsequent main commits increase it. The offset
is immutable for this history epoch, never a repository-variable, environment
or per-dispatch override. `show` reports it; `verify` must refuse missing,
malformed, negative or inconsistent offsets, shallow history and results outside
`0 < sequence < 2**53`. A root count of one is allowed only with the validated
bridge and positive offset; the old zero-offset single-commit refusal remains.
Verify image/source equality for the exact same revision on different runners.

Keep top-level signed `sequence` on its existing time scale. Before writing a
new row, check it is strictly greater than all earlier signed sequences relevant
to that channel/installer baseline, including withdrawn rows; fail on clock
regression rather than re-signing an old revision as a new update. Retrying the
same publication preserves its signed bytes and sequence. A stable tag after a
newer nightly can have a lower container sequence than that nightly: that is
correct, not an invitation to downgrade a nightly worker.

**A verified root mapping, not an ancestry exemption.** Prepare an export PR in
the old private repository after freezing both publishers. Let:

- `R` be the last signed old nightly/edge revision, verified with the existing key;
- `E` be the merge commit of that final private export PR;
- `S` be the single initial commit in the fresh public repository, with exactly
  the same complete Git tree as `E` (modes and every tracked file, not just runtime).

The export includes the reviewed migration implementation and offset. Include a
committed mapping naming the old repository's **numeric identity** and renamed
slug, the export PR number, pinned `R` and hash of its signed envelope, and `M`.
Open the export PR first to obtain its number. Do not embed `E` or `S`'s own hash
in that tree: that would be self-referential. Resolve `E` from the merged PR's
GitHub record and derive `S` from the new complete first-parent history. Read
the mapping from `S`, not an editable descendant. `bridge_attestation.py`
verifies tree equality and strict reach, rechecking GitHub/private evidence.
Its output is a diagnostic receipt, **not** portable self-authorization and
**not** an authorization of the exact-tree gate. That gate still needs a fresh
owner-created exact-SHA full-suite status and all its existing checks.

Refactor the reach walker to accept an explicit repository and checkout, then
handle only this configured transition when the published pointer is outside
new main:

1. Verify the live old pointer or reverified cached signed envelope; require the
   exact pinned envelope and `R`. On an outage with no matching signed evidence,
   refuse. The existing no-evidence bootstrap fallback must not bypass a configured
   cutover. Arbitrary other non-ancestor pointers still fail.
2. In a separate private, complete checkout, verify the old repository ID, the
   export PR is merged to its protected main, its merge SHA is `E`, and `R` is on
   `E`'s first-parent history. Run the existing strict reach walk **for every old
   main change `R..E`**, using that old repository's actual merged PRs and owner
   acceptance labels, not labels on the new repo or one catch-all unrelated PR.
3. Require `tree(E) == tree(S)` byte-for-byte and the new history has one root.
   This proves the initial public tree has not hidden an unreviewed delta. No
   private ancestors, tags or objects are pushed to the public repository.
4. Walk `S..candidate` normally using public PR evidence. Run the full suites on
   the clean exact public candidate and post a **new** owner's `local/full-suite`
   status there; recheck the mapping/reach before success as today. Do not reuse
   the old export commit's status. Publishing still checks protected current main.
5. Once the published pointer is a new-main ancestor, use the ordinary path;
   the mapping is no longer exercised. Retain historical evidence for audit,
   without granting another repository reset or changing installed trust keys.

Only the owner's local attester needs old-repository read access. Do not give
public workflows a private-history token or copy old objects into the public
clone. Missing private evidence fails closed. This bridge needs adversarial
tests and owner review; merely adding `base = S` is specifically not this design.

**Important freeze rule:** `E` contains the public offset: never publish it
from the old history. That would consume numbers above the intended new root.
Disable schedules and manual/tag publishing before
preparing `E`, drain in-flight jobs, and keep the old repo's publisher disabled.
If anything publishes after inventory, stop and rebuild the evidence/offset
before creating `S`; do not casually increase an offset after public publication.

### Installed-worker result

Compatible stable Docker workers see the next stable manifest at the same URL,
signed by the same key, with a larger container sequence; existing volumes,
credentials and database remain. Nightly workers do the same on their own lane.
Offline workers can skip the bridge revision because clients need no ancestry
mapping. Desktop Windows workers retain the time-based sequence/trust contract.
The new image uses the same offset, so replacing an image later does not revive
older code over a newer database.

Keep `SOURCE_CHANNEL=edge` until the alias-aware compatibility build has reached
old edge clients, as [release-channels.md](../ci/release-channels.md#two-step-edge-transition)
already requires. `/manifest/edge` and `/manifest/nightly` must return identical
signed bytes; never relabel signed JSON. Old images lacking required capabilities
or supervisor handover may need an owner-controlled **image replacement with
volumes preserved**, not a clean reinstall. Mac/Linux desktops do not currently
have the Windows in-place update path (`scripts/ci/update_reach.py:33-39`);
retain their installer delivery and do not promise a new updater in this cutover.
Inventory these exceptions before claiming all installed workers can migrate.

## B. Old and new release assets through the rename

All `hub/` paths in this section belong to the **private** repository
`HobbyCoders/hobbycoders-cloud`, not this app repository.

### Exact paths that fail today

- `hub/src/github-sources.ts:5-15` hardcodes `HobbyCoders/ai-shuffle` and fetches
  `/repos/{repository}/releases/assets/{assetId}` with `DESKTOP_GITHUB_TOKEN`.
  The old asset ID belongs to the renamed repository, not the new name occupant.
- `hub/src/desktop-updates.ts:18,31,46-54,68-80` reads installer/source asset IDs
  without repository provenance. `hub/src/installer-store.ts:30,59` falls back to
  `githubSource` for installer bytes (even when an R2 copy can mask the problem).
- `hub/migrations/0018_source_channels.sql:8-18` and
  `hub/migrations/0019_desktop_platform_installers.sql:13` store asset IDs, not
  which repository owns them. The legacy `desktop_installers` row also matters.
- App `desktop/scripts/publish-source.mjs:21,57,102,142-158` uploads to the
  hardcoded app repo and inserts/reads back D1 rows directly. It must write and
  check the provenance too, not just change a download URL.

### Required private-hub change

Add validated `release_repository` plus immutable `release_repository_id`
provenance to `source_releases`, `desktop_installers` and
`desktop_platform_installers`. Backfill **all existing rows**, including withdrawn
ones, to the old numeric repository ID. Initially the slug is its current name;
change those rows to `HobbyCoders/ai-shuffle-private` during the rename pause.
Keep release IDs, asset IDs, revision/channel keys, signatures, sequences,
withdrawal state, hashes and sizes unchanged. New publications explicitly store
the fresh public repository's identity. After backfill, reject absent provenance;
do not silently default unknown rows to whichever repo owns the public name.

A `RELEASE_GITHUB_REPO` setting is useful as the **new-publication default**, not
as the lookup for every historical asset. Pass the trusted row's provenance to
`githubSource`; allow only the two owner-approved repository identities, never
an arbitrary request-supplied repo or URL. Preserve HTTPS/manual redirect checks
and the existing rule that the credential is not forwarded to an asset CDN.
Keep a narrowly scoped read credential that can access the old private assets;
new public assets may use public reads or a separate read token. Do not widen
`SUPPORT_GITHUB_TOKEN` or repurpose it for downloads.

Update **every writer and reader** (source publisher, legacy Windows publisher,
platform/Android publishers, installer list/download, R2 fallback and withdrawal
views). Read back provenance and signed bytes after writes. Re-running a publish
must not attach an old row to a different repository. Do not mirror old private
zipballs to the public repository: some contain the pre-scrub tree. GitHub asset
IDs are not portable, and a repository redirect ceases to help once its old name
is reused. Keep the hub's existing download routes as the durable links.

Old direct GitHub URLs using `.../ai-shuffle/releases/...` cannot be guaranteed
once that name is reused. Inventory them separately: use explicit private-repo
URLs where authorized, or replace website links with stable hub routes before
cutover. Private source history must not be made public to preserve such a link.
Verify old source URLs and each currently advertised installer route through the
hub both before and after name reuse, including deliberately exercising the
GitHub fallback when an R2 installer copy exists.

### Alternative considered: one private store for every hub-facing asset

**Rejected.** Keep old **and all future** hub-facing source archives, installers
and APKs in `ai-shuffle-private`, and copy installers/APKs to public releases.
That would simplify the hub to one repository lookup and preserve old IDs, but
our publishers deliberately remain with the app source, exact-main gate and
image/buyer-install evidence. Moving them to a private repository requires an
authenticated cross-repository artifact/attestation protocol we do not have.
Giving public jobs a private-store write token instead broadens their authority;
duplicating every release adds copy-integrity, retry and withdrawal coordination.
It is not the simplest safe change to the existing pipeline.

**Decided:** old assets stay private; future assets live with public releases;
D1 rows route by explicit repository provenance. Update the legacy hub release
repository setting to `HobbyCoders/ai-shuffle-private`, backfill old rows before
allowing new ones, and give the public publisher only public-repository authority.
Do not rely on GitHub redirects after the old name is reused. This is the bounded
provenance change above, not an asset migration or a history import.

### Publication plumbing is also a prerequisite

At `6b02951bd`, source publication remains in the app repository and uses the
pinned Wrangler CLI in `scripts/source-publisher`, **not** deleted `android/hub`.
This supersedes the original plan's proposed publisher move; see
[the repository boundary](../ci/release-channels.md#repository-boundary).
Keep exact app revision, image/buyer-install evidence, owner-only stable triggers,
signing environments and current-main gates together. No public workflow gets
old-history read credentials. Only the local attester reads the private checkout;
the private hub holds the separate legacy-asset read credential.

Concurrency groups do not serialize across repositories: freeze the old
publisher and give exactly one new publishing path ownership of each lane.
Full source/installer staging is a separate follow-up: the hub currently selects
rows by `ORDER BY sequence DESC`, so a normal insert is immediately live. The
Docker-only canary below avoids inserting rows; its later release dispatch is
explicitly a live publication. All uploads, retry readbacks and withdrawal views
must respect provenance. No deployment or private-repository edit occurs here.

## C. Support must remain private

`hub/src/support.ts:181-195` checks repository spelling/token presence and POSTs
to `SUPPORT_GITHUB_REPO`; it does **not** check repository visibility or identity.
At `199-213`, existing `support_fingerprints.github_issue_number` values are used
for comments. A reused public slug could disclose a report or comment on an
unrelated issue with the same number.

Retain the same repository identity: set `SUPPORT_GITHUB_REPO` to
`HobbyCoders/ai-shuffle-private` and scope its fine-grained token to **only that
private repository**, Issues read/write (and metadata read). Verify the token's
actual access after rename; do not assume permission follows a string name.
Keep support and release-read credentials separate. Existing issue numbers then
remain valid. A different support repository is not part of this cutover; no
issue-number migration or reuse in a new namespace is allowed.

Private follow-up: before any issue/comment write, check authenticated GitHub
repository metadata with redirects refused: expected immutable repository ID,
expected full name, and `private: true`. Fail closed on public/mismatched repo,
metadata failure or missing configuration. Perform the check on delivery, not
only at deploy time; a visibility change after a cached check must not expose a
report. This is defense in depth, not a replacement for a private-only token.
Test both issue creation and deduplicated commenting. The existing
`SUPPORT_REPORTS=off` check at `support.ts:95` provides the maintenance pause
and returns 503 before delivery. Keep it off through the isolated rehearsal and
production identity/privacy readback; then explicitly authorize an unpause before
the real-app creation/dedup smoke. Immediately re-pause on any failure, following
the ordered support acceptance below. Never bypass the privacy guard to test.

## Ordered execution and stop points

Commands below are a **future cutover**, not commands to run while reviewing this
PR. Run in a dedicated operator shell, with `set -eu`; long tests/builds use the
worker's Monitor tool. Evidence files stay outside both repositories. No command
may print a secret. `M`, `EXPORT_PR` and receipt values below come from verified
inventory, never guessed defaults. An absent script/input, nonzero exit, missing
receipt or unproved precondition is a stop, not permission to skip the check.

### 1. Agent: land and rehearse the prerequisites

Land the bridge/shared-sequence/catalog tools, immutable Docker canary path and
release digest-reuse/final-promotion guards, plus the private hub's provenance
and support guard changes, **before** the final
export. The required CLI contract is:

```sh
python3 scripts/release/cutover_check.py --private-checkout "$PRIVATE_CHECKOUT" --private-export "$E" --public-root "$S" --old-repo HobbyCoders/ai-shuffle-private --new-repo HobbyCoders/ai-shuffle --json
python3 scripts/release/repin_catalog.py --public-root "$S" --check
python3 scripts/release/sequence_offset.py show
python3 scripts/release/sequence_offset.py verify
python3 scripts/release/bridge_attestation.py --private-checkout "$PRIVATE_CHECKOUT" --private-export "$E" --public-root "$S" --out "$EVIDENCE/bridge.json"
python3 scripts/release/bridge_attestation.py --private-checkout "$PRIVATE_CHECKOUT" --verify "$EVIDENCE/bridge.json"
```

These are implementation prerequisites, not tools supplied by this docs PR.
`cutover_check.py` reports PASS/FAIL per precondition and exits zero only when
all pass; it must fail closed for anything it cannot prove. It uses a **separate
private checkout**, never a private remote/alternate/object import in the public
clone. External facts (distribution maxima, key continuity, canary receipts and
console grants) also require read-back evidence; a source-only pass does not
prove them. Rehearse on disposable repositories/non-production D1 and retained
old installations. Required reach acceptance labels still require real platform
evidence: do not use this document as blanket acceptance.

### 2. Owner login: prepare service configuration, then freeze

In the private cloud repository, use its reviewed deployment runbook to deploy
backward-compatible provenance and the support guard while the old name still
exists. Backfill/read back every old D1 row and test legacy GitHub fallback.
Each production deployment still needs its own owner authorization; this
runbook is not a deploy grant. Preserve the existing edge alias compatibility
rollout. Inventory installed launchers/images, old signed envelopes, all retained
and withdrawn source rows/images, installers, support repository ID and issue
fingerprints. Take D1/export backups and restorable canary-worker backups.

In the hub's reviewed configuration set `SUPPORT_REPORTS=off` and apply/read back
the maintenance pause. Freeze both app and cloud publishers, including installer
and Play jobs and reruns; keep download-serving Workers running. Record the
original enabled workflow list before disabling Actions. The cloud's deployment
workflows are frozen too; deliberate maintenance can be re-enabled only for the
specific approved deployment and frozen again afterwards.

```sh
set -eu
OLD=HobbyCoders/ai-shuffle
PRIVATE=HobbyCoders/ai-shuffle-private
PUBLIC=HobbyCoders/ai-shuffle
CLOUD=HobbyCoders/hobbycoders-cloud
CUTOVER_DIR="$HOME/ai-shuffle-cutover"
PRIVATE_CHECKOUT="$CUTOVER_DIR/private"
PUBLIC_CHECKOUT="$CUTOVER_DIR/public"
EVIDENCE="$CUTOVER_DIR/evidence"
mkdir -p "$EVIDENCE"
OLD_ID=$(gh api "repos/$OLD" --jq .id)
gh workflow list --repo "$OLD" --all --json name,id,state > "$EVIDENCE/app-workflows.json"
gh workflow list --repo "$CLOUD" --all --json name,id,state > "$EVIDENCE/cloud-workflows.json"
gh api --method PUT "repos/$OLD/actions/permissions" -F enabled=false
gh api --method PUT "repos/$CLOUD/actions/permissions" -F enabled=false
for repo in "$OLD" "$CLOUD"; do
  gh api "repos/$repo/actions/permissions" --jq .enabled
  gh run list --repo "$repo" --limit 100 --json databaseId,status,workflowName
 done
```

Read back `enabled=false` for both. Drain all in-flight/queued runs before
inventorying `M`; cancel an obsolete run with `gh run cancel RUN_ID --repo REPO`
and read it back with `gh run view RUN_ID --repo REPO --json status,conclusion`.
Paginate if there are more than 100 runs. No run may publish after the snapshot;
if one does, re-inventory and redo the export before creating `S`.

### 3. Agent: prepare the final private export

Use a complete private clone, no shared object store. Open a normal export PR
first, record its number as `EXPORT_PR`, then commit the mapping/offset with DCO
sign-off. Record `M` from **all** shipped-sequence evidence, including withdrawn
and image-only publications. Compute rather than invent the offset:

```sh
gh repo clone "$OLD" "$PRIVATE_CHECKOUT"
cd "$PRIVATE_CHECKOUT"
python3 scripts/release/sequence_offset.py compute --last-shipped "${M:?inventory required}" > "$EVIDENCE/container-sequence-offset"
cp "$EVIDENCE/container-sequence-offset" release/container-sequence-offset
git add release/container-sequence-offset
```

Include the root mapping and final scrub review in that export PR. Merge via
normal reviewed policy, then resolve `E` from the merged PR, not a local guess:

```sh
git fetch origin main
E=$(gh pr view "${EXPORT_PR:?export PR required}" --repo "$OLD" --json mergeCommit --jq .mergeCommit.oid)
test -n "$E"
test "$(git rev-parse origin/main)" = "$E"
printf '%s\n' "$E" > "$EVIDENCE/private-export-sha"
```

Do not publish `E` against old history. Keep the old catalog pin unchanged in
`E`/`S`: embedding the not-yet-created `S` in its own tree is impossible. The
first public release PR below fixes the pin before any public release.

### 4. Owner login: rename, create, and read back identities

This is a rename, **not** making the old repository public. No DNS change is
needed: keep the hub origin, routes, signing key, GHCR package name and app ID.

```sh
gh repo rename ai-shuffle-private --repo "$OLD" --yes
test "$(gh api "repos/$PRIVATE" --jq .id)" = "$OLD_ID"
test "$(gh api "repos/$PRIVATE" --jq .private)" = true
git -C "$PRIVATE_CHECKOUT" remote set-url origin "https://github.com/$PRIVATE.git"
gh repo create "$PUBLIC" --public
gh api --method PUT "repos/$PUBLIC/actions/permissions" -F enabled=false
NEW_ID=$(gh api "repos/$PUBLIC" --jq .id)
test "$NEW_ID" != "$OLD_ID"
test "$(gh api "repos/$PUBLIC" --jq .private)" = false
test "$(gh api "repos/$PUBLIC/actions/permissions" --jq .enabled)" = false
```

In private cloud configuration change legacy `RELEASE_GITHUB_REPO` and
`SUPPORT_GITHUB_REPO` to `HobbyCoders/ai-shuffle-private`; update old row slugs,
retaining numeric IDs. Future rows explicitly name the public identity. Keep
distinct least-privilege asset-read and support tokens scoped to the private repo;
verify their actual access and the support metadata guard after rename. Follow
the private repo's migration/deployment commands (not ad hoc D1 edits). Keep
support paused through the isolated delivery rehearsal and production identity/
privacy readback. Then follow the explicitly authorized unpause → real-app
creation/dedup smoke → immediate re-pause on failure sequence below; live reports
cannot succeed while `SUPPORT_REPORTS=off`. Downloads must continue serving the
same signed bytes throughout the pause.

### 5. Agent: create the single root from the exact export tree

Create a **new empty directory**, not an orphan branch in a private clone. Copy
tracked tree bytes only, never `.git`, tags, remotes, credentials or `.env` files.
The export tree must already have passed the secret/scrub review.

```sh
test ! -e "$PUBLIC_CHECKOUT"
mkdir "$PUBLIC_CHECKOUT"
git -C "$PRIVATE_CHECKOUT" archive --format=tar "$E" > "$EVIDENCE/public-tree.tar"
tar -xf "$EVIDENCE/public-tree.tar" -C "$PUBLIC_CHECKOUT"
cd "$PUBLIC_CHECKOUT"
git init -b main
git add --all
git commit -s -m "Initial public source release"
S=$(git rev-parse HEAD)
test "$(git rev-list --count HEAD)" = 1
test "$(git rev-parse 'HEAD^{tree}')" = "$(git -C "$PRIVATE_CHECKOUT" rev-parse "$E^{tree}")"
git remote add origin "https://github.com/$PUBLIC.git"
```

Tree equality catches archive-attribute omissions, mode changes and missed files;
stop on any difference. Run the bridge verifier from step 1 now; its private
checkout is separate. Keep its receipt outside the tree. Run local checks and
push **only** `main` (`git push -u origin main`), never `--mirror`, `--all` or
private tags. Read back `gh api "repos/$PUBLIC/commits/main" --jq .sha` and require
`S`. Re-clone developer checkouts from the public repo; do not alter installed
worker databases/volumes or transplant the old `.git` directory.

### 6. Owner login: recreate trust and package access before enabling builds

Use these settings pages in this exact order; settings not represented by a
stable CLI are explicit console steps, not invented API calls:

```sh
gh browse --repo "$PUBLIC" --settings
gh api orgs/HobbyCoders/packages/container/ai-shuffle-server --jq '{id,visibility,html_url}'
```

1. On the public repository create protected `main` (reviewed PR, one approval,
   no force push/deletion, no bot bypass), owner-only `v*` tag rules, and the
   owner-actor/rerun gates. Enable secret scanning and push protection. Recreate
   `account-hub-production` and `release-signing` with their existing separate
   branch/tag policies (`release-signing`: `v*` tags only). Enable required owner
   review on public signing environments where supported; never weaken the
   immutable-actor checks. Read back rules and environment restrictions.
2. Open the [existing package settings](https://github.com/orgs/HobbyCoders/packages/container/ai-shuffle-server/settings).
   On **existing** `ghcr.io/hobbycoders/ai-shuffle-server`, preserve the package,
   set public visibility as D3 requires, and under **Manage Actions access → Add
   repository** grant the **new numeric repository identity** `HobbyCoders/ai-shuffle`
   write access. The old repository's grant does not transfer when its name is
   reused. Read back the granted repository and permission in that page, then
   prove it with the staged workflow push and an anonymous digest pull. Do not
   create a replacement package or change DNS.
3. Restore only required release secrets/variables from the owner's secure
   store, not from `.env` copies or log output. `gh secret list --repo "$PRIVATE"`
   and `gh variable list --repo "$PRIVATE" --json name --jq '.[].name'` inventory names, not portable secret
   values. For each environment use the interactive command, for example:

   ```sh
   gh secret set DESKTOP_APPLICATION_SIGNING_KEY --repo "$PUBLIC" --env release-signing
   gh secret set DESKTOP_APPLICATION_SIGNING_KEY --repo "$PUBLIC" --env account-hub-production
   ```

   Recreate the remaining named environment settings in
   [release-channels.md](../ci/release-channels.md#one-time-steps-for-the-owner)
   and the reviewed workflow inventory. The old application-update private key
   is not recoverable from GitHub Actions. The replacement key's public half
   must be one of the Ed25519 PEM blocks in `desktop/build/application-update-key.pem`;
   keep the old block first during the overlap and align the hub download verifier.
   Before switching publishers, use the **old repository's workflows** and its
   existing `DESKTOP_APPLICATION_SIGNING_KEY` secret to ship **one transitional
   release signed by the old key**, carrying the multi-key verifier and bundle:
   Docker image, Windows installer, Linux installer and source update. The old
   private key cannot be exported, but the old workflows can still sign with it;
   verify the secret matches in preflight before publication. The new public
   repository then signs with the new key. Source delivery alone does not migrate
   every installed verifier. Complete and verify each applicable migration:

   | Install kind | Migration before new-key-only publication |
   |---|---|
   | Docker image | Pull and recreate with the transitional image, keeping the existing data volume. A source update does not replace the image's supervisor trust bundle. |
   | Windows launcher | Apply the old-key-signed bridge with `desktop.launcher` minimum **3**, which forces replacement of older installed launchers; verify the swap. Alternatively install the transitional Windows installer over the existing install, preserving data. |
   | Source-bootstrap install | Close the app and rerun `desktop/scripts/bootstrap.ps1 -Root '<existing install root>'` **from the downloaded transitional source**, not the old shortcut. It refreshes `starter` when the shipped launcher version is newer (an unstamped legacy starter counts as 0), copies the new verifier/bundle and version stamp, and keeps the old starter in `starter-backup-*`. State, versions, profiles, runtimes and data outside `starter` stay untouched. Launch via the existing shortcut and verify before switching signers. Merely downloading the bridge does not refresh the shortcut's starter. |
   | macOS/Linux installer | Reinstall a transitional installer carrying the bundle, retaining existing data; source delivery does not replace its installed verifier. Produce a transitional macOS installer too if a macOS installation exists. |

   If old-key signing is unavailable, stop the automatic bridge: replace the
   image/installers and refresh source starters manually through trusted downloads
   before new-key-only publication. Remove the old key only after migration is
   complete. Windows clean-runner installer verification is not proof of migration
   of the owner's existing install.
   No private-history/legacy-asset/support token goes into public workflows.
4. Preserve the Android signing keystore/alias and existing Play app
   `com.hobbycoders.aishuffle`, upload key, Play App Signing certificate and
   tester/production lineage. Re-enter existing `KEYSTORE_BASE64` and the other
   signing inputs named by `android-play.yml` privately, never generate a new
   keystore/app. `versionCode` is **epoch minutes** (`date +%s` divided by 60),
   not a Git count or Actions run number: history reset does not reset it. Compare
   the candidate certificate and code to Play's existing record; a duplicate
   minute needs a later build, not code reuse. Play submission remains held.

Keep Actions disabled until the following release PR's staging guards are
present and reviewed. No blanket owner decision remains: these are login-only
execution actions for the settled design.

### 7. Agent: the first public release PR repins the catalog

The inherited catalog pins private-history commit
`73d7c591f6e51c44960d0f6198513e7e5b22c25d`. `verify_catalog()` in
`scripts/ci/release_preflight.py` requires that commit **locally** and retrieves
its connector files from `HobbyCoders/ai-shuffle`. Neither works after name reuse.
Do not fetch private ancestors to make the check pass.

**Decided:** the first public release is `v1.0.0`, from the merge of a normal
public release PR, not root `S`. Inventory all old distributed final versions;
if any is `>=1.0.0`, stop as a failed prerequisite rather than reusing a version
or silently choosing another. In that release PR:

```sh
cd "$PUBLIC_CHECKOUT"
git fetch origin main
git switch -c release/public-1.0.0 origin/main
python3 scripts/release/repin_catalog.py --public-root "$S"
python3 scripts/release/repin_catalog.py --public-root "$S" --check
python3 scripts/ci/release_preflight.py --remote-catalog
```

The repin must require every package file's bytes to match the existing digest
in the checkout **and at public root `S`**. Update only the catalog revision and
its derived identity, not connector bytes or digests to hide a mismatch.
Recompute `release` as the first 16 hex characters of SHA-256 of the catalog
without `release`, JSON sorted keys and separators `(',', ':')`. Repinning to
`S` makes the pin locally reachable and remotely readable in the new repo.
Remote verification must pass **before creating/pushing the release tag**.

Update the normal `1.0.0` version surfaces/Compose pins and their existing
`docs/content` / `frontend/src/lib/docs/content` copies in that release PR. Run
focused catalog/preflight tests and local CI, commit with `git commit -s`, push,
and merge by ordinary reviewed policy. Re-run the repin check and remote catalog
preflight on the exact merged public main. `cutover_check.py` must now pass all
checks, including a single root, bridge evidence, sequence equality and the
workflow's `image_digest` input and final alias-job ordering. Absent Docker
canary/digest-reuse/promotion guards are a hard stop.

From clean public main, run the bridge verifier and **fresh** exact-SHA full
attestation (`LOCAL_CI_PYTHON=/usr/local/bin/python3 /usr/local/bin/python3
scripts/local_ci.py full --attest`, through Monitor). Obtain actual Windows
and other required platform evidence per [pr-checks.md](../ci/pr-checks.md);
this cutover does not select a Windows exception. Never copy old statuses or
bypass update-reach with a missing label. Recheck current main before tagging.

### 8. Owner login: tag and stage the Docker-only canary

Only after step 7 and the login readbacks above:

```sh
cd "$PUBLIC_CHECKOUT"
TAG=v1.0.0
CANDIDATE_TAG="candidate-${TAG}-$(git rev-parse HEAD)"
python3 scripts/ci/release_preflight.py --remote-catalog
git tag -a "$TAG" -m "First public AI Shuffle release"
python3 scripts/ci/release_preflight.py --tag "$TAG" --github --remote-catalog
git push origin "refs/tags/$TAG"
gh workflow disable desktop-source.yml --repo "$PUBLIC"
gh api --method PUT "repos/$PUBLIC/actions/permissions" -F enabled=true
gh workflow list --repo "$PUBLIC" --all --json name,state
gh workflow run docker-publish.yml --repo "$PUBLIC" --ref "$TAG" -f release_tag="$TAG" -f promote_latest=false -f candidate_tag="$CANDIDATE_TAG"
```

Disable the nightly workflow **before** enabling repository Actions; fence any
other automatic publisher the same way. Read back disabled states before the
manual Docker dispatch. Keep the old repo disabled and record the run ID/source
SHA. Monitor the terminal status and inspect the container verification job.
No source publisher, installer publisher or Play workflow is dispatched yet.

**Why this path is mandatory:** the baseline normal `release.yml` passed
`promote_latest=true` to Docker, moving `latest`/major/minor **before** buyer-install
and source signing finished; a manual main image build moves `nightly`. Do not
run either baseline path during canary. The reviewed `candidate_tag` path must
use immutable candidate/version tags with `promote_latest=false`, preventing
**all** moving aliases (`latest`, major, minor, `nightly`, `edge`, branch tags).
Retries reuse the same verified bytes/digest; an existing candidate/version tag
must never be overwritten with a different digest.

Capture the published multi-platform/index digest as `DIGEST` from the successful
publisher receipt. Pull `ghcr.io/hobbycoders/ai-shuffle-server@$DIGEST` anonymously,
and verify its revision/sequence. Canary the **image by digest**, not a mutable
alias, on one backed-up existing Docker worker with volumes preserved. Exercise
healthy boot, persistent data, owner sign-in, a real session and worker connectivity.
This deliberately tests image migration, not a staged source-update selector.

During this canary **installer pointers, D1 source rows/default feeds, GitHub's
release/latest marker and every Play upload/submission (including internal)**
remain held at their old values: no release workflow has run. Compare their
before/after receipts, along with every image alias, to prove that invariant.
If the canary fails, leave all defaults held; do not invoke the stable workflow.

### 9. Owner login: release the accepted digest, then verify each destination

Stable `v1.0.0` is first. The reviewed `release.yml` must accept `image_digest`
and have a final alias-promotion job that depends on successful buyer-install
**and** source signing. The same exact-tag release must invoke the Windows
installer with `build_desktop=true`, the Play-prep PR's **`android-apk` job** for
the signed sideload APK, and the reviewed Linux artifact workflow with release
attachment and checksum readback. The Play `android` job's AAB is not a sideload
APK; the baseline Linux artifact-only workflow does not attach a public download.
These are implementation prerequisites, not promises that the baseline dispatch
already fulfills. `cutover_check.py` must verify the guarded digest path and all
three exact-tag artifact publication paths (including Linux attachment/checksum
readback) before this step. Missing wiring or a failed check stops the cutover.
With those checks and the canary receipt passing, dispatch:

```sh
gh workflow run release.yml --repo "$PUBLIC" --ref "$TAG" -f version="$TAG" -f image_digest="$DIGEST" -f build_desktop=true
```

This is the **live release boundary**, not another staging command. The publisher
must resolve the digest in GHCR, verify tag revision/tree/sequence metadata and
reject a mismatch. It reuses the canary digest **without rebuilding**. Buyer-install
pulls that same digest on a clean runner. All `latest`/major/minor moves occur in
the final job, only after buyer-install and source signing succeed; normal releases
without `image_digest` must obey the same final-job ordering. The final job uses:

```sh
python3 scripts/release/promote_image.py --digest "$DIGEST" --tags latest,1,1.0
```

Do not invoke that command ahead of the workflow's acceptance gates. Promotion
moves only the verified digest, never a rebuilt image. Installer pointers and
source-row activation are held until the live release dispatch, **not** until an
unimplemented second-phase transaction. Play submission is likewise held during
canary; this dispatch may upload to **internal testing only**, with the existing
worker/signing gates. Production rollout remains a separate owner Play Console
submission of that accepted lineage under D7 (closed test first only if required).
No internal upload is evidence of Play production availability.

Full signed source/installer staging and coupled second-phase activation are
tracked in [the release-staging follow-up, #2335](https://github.com/HobbyCoders/ai-shuffle/issues/2335).
They are not implemented by the Docker-only path and are not claimed as a
prerequisite already satisfied.
Do not invent `stage_only`/`promote` inputs or insert an ordinary higher-sequence
D1 row and call it hidden. In this first cutover, normal stable source signing
can advance the live feed before the final image aliases move. That risk is
explicit: pre-dispatch bridge/catalog/full-suite/platform gates must pass, and
post-dispatch source-update acceptance below is required before announcement.
The lack of full source staging does not waive any existing release gate.

Read back every image alias digest, signed feed hash/sequence, installer hash,
GitHub release state and Play track/code. Inspect job-level publication receipts;
a green workflow with skipped jobs is not proof of publication. Any partial
failure stops announcement and later platform rollout. Use a higher-sequence
forward fix for workers already migrated; never downgrade an image/feed.

Before declaring the release available, collect these **ordered, gated artifact
receipts**. Use `jq` and `sha256sum` on the operator host; save files under the
private evidence directory, not the checkout. A checksum mismatch, missing asset,
skipped publisher or wrong source revision is a stop, not partial completion.

1. Select the run ID from this dispatch (never blindly take an unrelated latest
   run), verify the exact tag SHA, then wait via Monitor. Read job-level results
   and require successful Windows build/verify/publish, Linux build/publish and
   `android-apk` build/publish, not just successful Play AAB generation.

   ```sh
   RELEASE_SHA=$(git rev-parse "refs/tags/$TAG^{commit}")
   gh run list --repo "$PUBLIC" --workflow release.yml --limit 20 --json databaseId,headSha,event,status,createdAt
   test "$(gh run view "${RELEASE_RUN_ID:?select this dispatch}" --repo "$PUBLIC" --json headSha --jq .headSha)" = "$RELEASE_SHA"
   gh run watch "$RELEASE_RUN_ID" --repo "$PUBLIC" --exit-status
   gh run view "$RELEASE_RUN_ID" --repo "$PUBLIC" --json jobs
   RECEIPTS="$EVIDENCE/release-$TAG-$RELEASE_RUN_ID"
   mkdir -p "$RECEIPTS/win" "$RECEIPTS/linux" "$RECEIPTS/android"
   gh api "repos/$PUBLIC/releases/tags/$TAG" > "$RECEIPTS/release.json"
   jq -e --arg tag "$TAG" '.tag_name == $tag and .draft == false' "$RECEIPTS/release.json"
   ```

2. **Windows:** `build_desktop=true` must have produced the verified installer,
   published it and updated its advertised hub pointer. Download the public
   release bytes and check both SHA-256 and the build revision:

   ```sh
   gh release download "$TAG" --repo "$PUBLIC" --pattern "AI-Shuffle-Desktop-${TAG#v}-win-x64.exe" --pattern SHA256SUMS --pattern desktop-build.json --dir "$RECEIPTS/win"
   (cd "$RECEIPTS/win" && sha256sum --check SHA256SUMS)
   jq -e --arg sha "$RELEASE_SHA" '.revision == $sha' "$RECEIPTS/win/desktop-build.json"
   ```

3. **Linux:** require release-attached AppImage **and** deb packages, their
   provenance and the platform-specific checksum file, not just a short-lived
   Actions artifact. The Linux publisher must preserve `SHA256SUMS-linux` rather
   than overwrite Windows' `SHA256SUMS` on the same release.

   ```sh
   gh release download "$TAG" --repo "$PUBLIC" --pattern '*.AppImage' --pattern '*.deb' --pattern SHA256SUMS-linux --pattern linux-build-provenance.json --dir "$RECEIPTS/linux"
   for suffix in AppImage deb; do
     set -- "$RECEIPTS/linux/"*."$suffix"
     test "$#" -eq 1 && test -s "$1"
   done
   (cd "$RECEIPTS/linux" && sha256sum --check SHA256SUMS-linux)
   jq -e --arg sha "$RELEASE_SHA" --arg version "${TAG#v}" '.sourceCommit == $sha and .version == $version' "$RECEIPTS/linux/linux-build-provenance.json"
   ```

4. **Android sideload:** require the Play-prep PR's exact-tag `android-apk` job
   to publish the signed APK, checksum and provenance. Do not substitute the
   Play bundle or a main/debug APK. Read back the release files:

   ```sh
   gh release download "$TAG" --repo "$PUBLIC" --pattern AI-Shuffle-Android.apk --pattern AI-Shuffle-Android.apk.sha256 --pattern android-build.json --dir "$RECEIPTS/android"
   (cd "$RECEIPTS/android" && sha256sum --check AI-Shuffle-Android.apk.sha256)
   jq -e --arg sha "$RELEASE_SHA" --arg version "${TAG#v}" '.commit == $sha and .versionName == $version' "$RECEIPTS/android/android-build.json"
   ```

5. Compare all downloaded payload hashes/sizes with the verified build/publisher
   receipts from this run and the release asset records. Fetch each advertised
   Windows/Linux/Android hub download and require the same payload hash, correct
   repository provenance and version; exercise GitHub fallback separately from
   R2. Check Windows signing/install/launch, Linux AppImage/deb installation and
   launch, and APK certificate/version-code/install continuity on their real
   platforms. Only after those readbacks may the downloads page/announcement say
   all three ship. A successful image, Play upload or checksum alone is not
   platform acceptance; keep the already-published partial-release facts explicit.

After stable acceptance, retain edge/nightly defaults until that lane has its
own compatible-worker/image acceptance and a fresh exact-main attestation.
Promote an accepted nightly image digest with `--tags nightly` only at that
lane's acceptance; no stable aliases move. Keep `SOURCE_CHANNEL=edge` until alias
compatibility is proved. Then re-enable only the once-daily 06:00 UTC schedule:

```sh
gh workflow enable desktop-source.yml --repo "$PUBLIC"
gh workflow list --repo "$PUBLIC" --all --json name,state
gh api "repos/$PRIVATE/actions/permissions" --jq .enabled
```

The final command must still print `false`. Restore private cloud deployment
availability deliberately from its recorded list, **not** an old source/image
publisher. Keep old assets/support/history and verified evidence indefinitely;
never announce a platform/channel that has not passed its own acceptance.

## Acceptance receipts: Docker before release, source before announcement

Before step 9, canary the immutable Docker digest on a backed-up existing worker,
with volumes preserved and no default feed/alias/pointer movement. Before rename,
also rehearse the old-source → public-root update on an isolated installation and
non-production hub with the real publisher and keys under the normal gates.
Neither a fixture-seeded manifest nor an image boot proves the source updater.

After live source publication in step 9, exercise an existing stable source-update
worker and Windows launcher before announcement. This **does** expose the signed
source to other clients on that lane; the first cutover has no per-worker source
selector. Record it as post-publication evidence, not a hidden source canary.

The release-staging follow-up must add opt-in candidate selection keyed by a
**verified worker lease identity**, never a query-string/user-agent bypass.
The updater's lease header (`docker/application_updates.py`) is distinct from
`app/network/hub.py` registration. Unsupported old clients cannot use that future
selector; never weaken signatures or silently publish globally to simulate it.

For each backed-up acceptance worker, record which phase/channel was exercised:

- Capture running/image revision, channel, capabilities, image and active/pending
  container sequences, current signed feed hash, and a harmless persistent test
  record. Keep other workers' default feed unchanged (compare signed bytes).
- For source acceptance after live publication, in the **real app**, check for
  updates, download, then explicitly choose Update
  and restart. Observe signature/archive/tree acceptance, healthy boot, new public
  SHA and strictly larger container sequence. Recheck the persistent record,
  owner sign-in, credentials remaining usable, sessions and worker connection.
  Check once more: it should report up to date without another download.
- Confirm a retained old archive still downloads byte-for-byte, a new archive
  downloads from the new repo, and both nightly aliases return identical signed
  bytes. Fetch every advertised installer and exercise GitHub fallback separately.
- Complete the ordered support acceptance below. A 503 while support is paused
  proves the maintenance fence, not successful private delivery.
- A canary on one channel proves only that channel/platform. Repeat Docker
  acceptance on a stable worker before stable image promotion and an edge/nightly
  worker before that lane's promotion; test an existing Windows launcher against
  the live source feed before claiming desktop continuity.
  Never move a canary backwards from nightly to stable to save a test machine.

If the Docker canary fails, stop before the live release and leave all defaults
unchanged. If post-publication source acceptance fails, stop announcement, withdraw
the bad release under the normal procedure, and do not pretend the feed never
moved. Already migrated workers need a higher-sequence forward fix,
not a lower pointer or an older image. Cached pending downloads cannot be recalled
by a hub change. Retain backups and the normal first-boot recovery path. Hub code/
provenance can be rolled back only to a version that still routes both repositories;
restoring an old D1 snapshot wholesale can lose new support reports and is not the
rollback procedure. Keep support paused if private delivery cannot be proved.

### Support acceptance: rehearse, read back, unpause, smoke, re-pause on failure

1. **Keep production `SUPPORT_REPORTS=off`.** In an isolated deployment with
   synthetic data and a dedicated private test issue destination, enable reports
   there and exercise issue creation plus a second report with the same
   fingerprint. Require the guard to verify that destination's immutable ID,
   full name and private visibility on every write. In that isolated deployment,
   public/wrong-ID/unavailable metadata must fail closed. Do not disable the
   privacy guard or route test failures through live customer traffic.
2. **Read back production identity/privacy while still paused.** Require deployed
   `SUPPORT_GITHUB_REPO=HobbyCoders/ai-shuffle-private`, expected old numeric ID,
   `private: true`, correct fingerprint namespace and private-only Issues scope
   for the service's effective support credential. Obtain metadata-only evidence
   using that credential through the reviewed private service check, without
   displaying it. Owner metadata corroborates the target but is not proof of the
   service token's access:

   ```sh
   test "$(gh api "repos/$PRIVATE" --jq .id)" = "$OLD_ID"
   test "$(gh api "repos/$PRIVATE" --jq .full_name)" = "$PRIVATE"
   test "$(gh api "repos/$PRIVATE" --jq .private)" = true
   ```

3. **Owner explicitly authorizes unpause.** Only after steps 1–2 pass, apply
   `SUPPORT_REPORTS=on` via the private configuration/deployment runbook and read
   back the deployed value. Re-enable only the necessary approved deployment
   workflow during the publication freeze, then restore its freeze. This is a
   real reopening of production intake, not an implicit approval in this doc.
   Prepare the immediate `SUPPORT_REPORTS=off` rollback before unpausing; keep
   the metadata/privacy guard active on each delivery.
4. **Now run the real-app smoke.** Submit a synthetic report with no customer
   data, then a second report with the same fingerprint. Read back the issue and
   deduplicated comment in the expected private numeric repository; require no
   public issue/comment and no unrelated issue reuse. Keep support enabled only
   when both succeed. On any failure, immediately apply/read back
   `SUPPORT_REPORTS=off`, preserve the failed receipt privately, and stop wider
   rollout. A privacy mismatch remains blocked by the guard even during the
   unpaused smoke; never bypass it or retry against a public destination.

## Verification required of the implementation

- App focused tests: `tests/scripts/test_edge_publication_gate.py`,
  `tests/scripts/test_local_ci.py`, `tests/scripts/test_nightly_changed.py`, plus
  new shared-sequence tests and existing source/updater tests covering touched
  paths. Positive case: unrelated old/new histories with equal export/root trees
  and valid per-PR acceptance. Negative cases: changed root byte/mode, wrong old
  repo identity/PR, missing label, invalid/cached-stale signature, arbitrary old
  pointer, missing evidence, shallow/partial history, main moving during attest.
- Catalog tests: the producing repin path requires matching root/checkout bytes,
  recomputes release identity, passes local and public remote verification, and
  never fetches private ancestors. Run from a genuinely fresh single-root clone.
- Image tests: candidate dispatch writes no moving aliases; digest reuse verifies
  GHCR metadata against tag/tree/sequence and never rebuilds; final promotion
  waits for buyer-install and source signing, including normal release paths.
  Negative tests cover digest mismatch, failed/skipped dependency and retry drift.
- Sequence tests: count one with/without authorized mapping, offset zero,
  malformed/negative/overflow offset, retry identity, clock regression, a
  distributed withdrawn/image-only maximum, and identical image/source metadata.
  Drive the real publisher/helper producer, not only hand-seeded manifests.
- Private hub tests: old/new assets coexist; backfill is idempotent; R2 fallback
  and all installer routes use provenance; missing/unknown repo refuses; no token
  on redirects; withdrawal remains effective; support refuses public/wrong-ID/
  unavailable metadata and dedup stays private. The future staging follow-up must
  also prove candidate rows/pointers stay hidden until authorized activation.
- Rehearse from a real last-private installation to a freshly rooted candidate,
  then collect the running-app evidence above. Fixtures and a green workflow do
  not establish this. Do not announce continuity until the rehearsal passes.

This docs PR has not renamed repositories, deployed the hub, moved a feed,
used production support credentials, run an update in the app or verified a real
worker cutover. Local checks for this PR validate documentation/repository policy,
not an implemented migration. User-facing update behavior has not changed, so no
`docs/content` / `frontend/src/lib/docs/content` mirror is changed here.

## Settled choices and completion record

D3 uses a single public root, the existing update trust key/origin/GHCR package,
a verified local bridge and a committed monotonic offset. `ai-shuffle-private`
retains support and historical assets indefinitely; new releases use public
provenance. No DNS change or Android signing-lineage reset is required. The first
public stable version is `v1.0.0`, from the reviewed catalog-repin release PR.
Stable is promoted only after acceptance; nightly follows its own acceptance and
then resumes daily, with no channel switching. The first public release requires
Windows (`build_desktop=true`), release-attached Linux AppImage/deb packages and
the `android-apk` sideload APK, with the exact-tag wiring, checksums, provenance
and real-platform acceptance in step 9. None is considered shipped merely because
the Docker/Play AAB jobs passed. Missing Linux/APK publication wiring blocks the
cutover until implemented and checked. Unsigned macOS/iOS publication waits for
the Apple signing prerequisites in D8. None is an unresolved preference.

The operator records old/new repository IDs, `R`/`E`/`S`, the export/release PRs,
sequence inventory and offset, bridge/gate receipts, remote catalog verification,
image digest, source signatures, installer hashes, Play certificate/code,
per-platform acceptance and every promotion readback. Login-only owner steps
are ordered above; missing evidence is a stop, never an invitation to guess.
This document is not that completion record.
