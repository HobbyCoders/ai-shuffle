# Where tests run

Tests run on the machine where the change was made, before it is pushed.
GitHub Actions does not test pull requests: no workflow starts on a pull
request or a push to a branch. The daily schedule only publishes an attested
nightly; it never runs tests. CI minutes go only to work no local machine can do: release builds, signing, publishing, deploys, and the
macOS and iOS checks we have no Mac for.

Public-app workflows use standard GitHub-hosted `ubuntu-latest`,
`windows-latest` and `macos-latest` runners (some existing jobs pin an OS
version). These runners are free when the repository is public; larger paid
runners are not required. Docker jobs use the official Buildx/build-push
actions with the GitHub Actions layer cache (`type=gha`, scope `Dockerfile`).
Cache access follows GitHub branch isolation: a branch can restore the default
branch cache, not another branch's private cache. No sticky disk is required.
The cloud-service workflows are being moved separately to the private
repository; their runner migration is not part of this change. Cancelling the
Blacksmith subscription is an owner action after that migration.

Cloud service checks and deploy workflows live in the private
[HobbyCoders/hobbycoders-cloud](https://github.com/HobbyCoders/hobbycoders-cloud)
repository. App checks do not need that checkout. When app docs/install files
change, refresh the committed website snapshots there using `AI_SHUFFLE_CHECKOUT`
and its README; the app still checks `docs/content` against the frontend copy.

## Before every push

```bash
python scripts/local_ci.py install   # once per clone: turns on the pre-push hook
python scripts/local_ci.py           # the suites this branch's changes reach
python scripts/local_ci.py list      # what each suite runs, and when
```

`local_ci.py` compares the branch with `main` on the remote whose URL points to
`HobbyCoders/ai-shuffle` (`origin` is preferred when it matches), and runs each
suite the change reaches. This works even when `origin` points elsewhere and the
public repository has another remote name. For forks or another comparison
branch, set `LOCAL_CI_BASE=<remote>/<branch>` or
`git config local-ci.base <remote>/<branch>`; the environment takes precedence.
The comparison branch is fetched before changed-suite checks; fetch failures and
unrelated histories stop with an explanation. `full --attest` ignores these
overrides for publication authorization: it fetches the public repository's
`main` and requires a clean checkout at that exact revision.

The suites are: backend (the same function-level selection the backend test
map gives), fluid, frontend, mobile, tools, browser-mcp, docs, desktop, the
Android app and download bridge, the workflow policy tests and the in-app update
reach check. It runs at low CPU priority with at most two test workers
(`LOCAL_CI_WORKERS`), because every worker host is shared with other sessions
and, on Unraid, with everything else the server runs.

A pass on a clean checkout is recorded against the commit's tree in
`.git/local-ci/passes.json`. The pre-push hook (`.githooks/pre-push`, which
`install` copies into the directory git runs hooks from, next to AI Shuffle's
managed hooks; an older `pre-push` there is kept as `pre-push.user` and still
runs afterwards; `install` also saves a Python that has pytest and PyYAML
in git config `local-ci.python`, which the hook and later runs use) lets a
recorded commit through after the offline outgoing-commit secret scan. For an unrecorded commit that is the clean
`HEAD`, it runs the checks and pushes only if they pass. Anything else (a
commit other than `HEAD`, or uncommitted changes) is refused, because the
checks would not test what is being pushed. Never skip the hook with
`--no-verify`.

Agents run `local_ci.py` through the Monitor tool before pushing, so the hook
finds a recorded pass instead of running a long check inside a shell call.

The selected Python is checked **before suites or commit statuses**: all required
`requirements-dev.txt` imports (including distlib, xdist and PyYAML) must work.
An installed `claude_agent_sdk`, an external `docker` package shadowing the
checkout namespace, or inherited `PYTHONPATH` entries outside this checkout
refuse the run. Use a fresh `.venv`, install `requirements.txt` and
`requirements-dev.txt`, clear external `PYTHONPATH`, and set `LOCAL_CI_PYTHON`
to that environment's Python. Dependency directories and provisioned symlinks
(`.venv`, `venv`, `node_modules`) are ignored; force-added files still undergo
the secret scan.

### Full gates longer than one Monitor budget

On a slower machine, including Windows (about 75 minutes there), use the
supported checkpointed path. The whole sequence is one command, repeated:

```bash
python scripts/local_ci.py full --step --attest     # one Monitor job; repeat until it prints COMPLETE
```

Each invocation runs stages back to back while the next stage still fits in
`--budget` minutes (default 40, inside the Monitor tool's 45-minute budget),
judged by that stage's last recorded duration on this machine
(`full-stage-times.json` beside the backend test map; a stage never timed
counts as 10 minutes, an unseen backend batch as the slowest batch timed).
Run **one invocation per Monitor job**, inspect its completion, then repeat the
same command. On Windows that is usually three to five invocations. An
intermediate exit 0 means only that the gate made progress, not a full pass or
successful attestation. Do not wrap the commands in one long Monitor loop,
detach processes, use `serves=true`, or increase the two-worker limit. Without
publication, omit `--attest`. `--budget 0` runs exactly one stage, without a
time limit.

`--attest` accepts a completed stage set: stages run without `--attest` (for
example while main was being checked) can be attested afterwards with the same
command, or with `python scripts/local_ci.py full --attest`, as long as the
revision, tree, interpreter and machine are unchanged. That posts the status
from the completed set without rerunning anything.

Stages cover the same suites and backend tests as `full`: security, backend
batches of 20 files using the original directory collection and exclusions,
each remaining suite, then final security/release checks and test-map merge.
No tests are dropped to meet the time budget. Backend map hits accumulate across
batches. Other suites keep their original commands and platform availability.

**The budget is enforced.** When an invocation reaches `--budget`, the running
command and every process it started (pytest-xdist workers, npm's node) are
stopped, the stage gets no receipt, and the invocation exits 0 with
`Stopped at the end of the … budget` and what happens next:

- A stage that started late in the invocation, and so had only what was left
  of the budget, runs first in the next one. Only a unit that ran first, with
  the whole budget, is ever judged too long.
- A **test stage** (each backend batch, and `fluid`) that could not finish in a
  whole budget is split. Test stages run as *shards*: sorted lists of test
  files dealt round robin, stored in the state file, so a resumed run uses
  exactly the same partition. The shard that did not fit is halved; finished
  shards are kept. A stage this machine has timed above the budget starts as
  several shards, so a second run splits up front and wastes nothing; a stage
  never timed starts as one shard (up-front splits use measured durations, or
  the rate of this run's finished shards, never the 10-minute guess). A shard
  runs the suite's own command and directories, with the other files ignored
  (`scripts/ci/local_ci_batch.py`); a single shard runs the original command.
  Time spent waiting for the machine's shared pytest lock counts toward the
  shard's time, so a busy machine can make a shard look slower than it is.
- A unit that cannot be split (a non-test stage such as `frontend`, or one test
  file) and still does not fit a whole budget stops with exit 1 and `cannot be
  split further`. That is a time limit, not a test failure: no receipt and no
  failure status are recorded (a `pending` status from `--attest` stays). Run the
  same command once with a larger `--budget` that still fits the job's time
  limit (for example `--budget 44` in a 45-minute Monitor job), or with
  `--budget 0` (that stage alone, no limit) where nothing limits the run's
  time, then continue with the usual command. If it does not fit even then,
  report it.

**Interruption from outside** (the Monitor timing out, a closed terminal,
Ctrl-C, a reboot) is handled too. The running unit is written to the state
file before it starts, and `full-stages.beat` is refreshed every 30 seconds
while the invocation lives. SIGTERM, SIGHUP and Ctrl-C stop the process tree
and record how long the unit ran. After an outright kill, the last heartbeat
gives it. The next invocation reports `The previous invocation was stopped
during …` and treats a unit that ran first and used 90% of the time it had like
one stopped at the budget. A unit that had a whole budget and was killed twice
in a row is split (or stops as above, if it cannot be), whatever `--budget`
says. An invocation holds an OS file lock (`full-stages.lock`) for its whole
life, so a second invocation in the same worktree refuses to start while one
runs; the system releases the lock however the process ends, so a killed run
never blocks the next. State and receipts are written atomically and
flushed, and finished batches' map hits are kept beside them
(`full-stages-hits/`), not in the system temp directory.

Receipts live beside `passes.json` in `full-stages.json` in this worktree's Git
directory. Only a successfully completed stage or shard gets a receipt; an
interrupted or failed one is rerun. Every invocation requires a clean unchanged
checkout and runs the Python preflight. Revision/tree, interpreter path,
machine, worker count, audit mode and stage plan (including every backend
batch's and fluid's test files) must match to reuse receipts. Shards are not
part of that identity: they partition the same files. Changing any of those
starts a new run. If dependencies or external tools change in place, use
`full --step --restart` (plus `--attest` if applicable) once, then omit `--restart`
for subsequent stages. `--only` cannot attest or supply full-stage receipts.

`--attest` retains the clean current-public-main and update-reach requirements,
and checks them first, in seconds: right after the Python preflight, before
any commit status and before any suite. Partial work posts only pending/failure
statuses (one pending per invocation). Only after **all** stages pass, and the
finished shards of every test stage cover its files exactly once, does the
final stage write the full pass and post one success naming the tree (including
`local/windows-suite` on Windows). Attesting a completed set later rechecks
the same coverage. If main advances mid-run, update to it and
start over; old receipts cannot authorize the new revision.

The update-reach check walks the private archive's history across the public
root (see [release-channels.md](release-channels.md)). When
`AI_SHUFFLE_PRIVATE_CHECKOUT` is unset and this clone has a remote for
`HobbyCoders/ai-shuffle-private` (a shared checkout where `origin` is the
archive), `--attest` uses this clone's own Git directory for it, says so, and
refreshes that remote's `main`. A shallow clone is never used.

The last lines of each run list what this machine did not cover:

- Windows, when the run is not on a Windows worker. Edge publication does not
  wait for Windows; a stable release does (below). When DesktopWorker is off,
  Windows-only changes still merge and reach edge, and the gap shows in the
  pass record and in the ship-release preflight.
- On Windows, tests requiring POSIX executables or unavailable symlink
  privileges skip individually with their platform or capability reason.
  Desktop typechecks and unit tests run on Windows as well as Linux.
  `scripts/ci/windows_known_failures.txt` is empty, and a regression test keeps
  it empty. Fix failures rather than adding exclusions to get a push through.
- macOS folder confinement and the iOS app: no Mac. The owner dispatches
  `ci-folder-confinement.yml` or `ios.yml` when a change needs them, usually
  before a release.
- The container image: the release workflow builds and verifies it, or run
  `python scripts/verify_container_postgres.py` where Docker is available.

An update-reach failure is acknowledged as before, with a pull request label
`update-reach:GROUP` (`local_ci.py` reads the PR's labels) or
`--allow GROUP` when running `scripts/ci/update_reach.py` directly.

A change to the installed desktop launcher's import closure (group
`windows-launcher`, which covers the packaged Windows and Linux apps) must also
raise `desktop/launcher.json` `version` in the same pull request. Otherwise the
check also fails `launcher-version`, and `update-reach:windows-launcher` does not
accept it. The only other way through is the owner's
`update-reach:no-launcher-change` label (or `--allow no-launcher-change`): the
change leaves what the launcher does unchanged, such as a move or a comment.
That label accepts both groups.


## Local security checks

Every `check` (including `--only`) and `full` first scans the whole working tree
(tracked files plus non-ignored untracked files) for leaked credentials. The
pre-push hook also scans the changed blobs in **every outgoing commit**, before
consulting its cached test pass. A token added and removed in separate commits
still blocks the push. Existing refs use the remote tip through the pushed tip;
new branches/tags exclude commits already on local `origin/main` (or scan all
reachable history if it is unavailable). Tags are scanned; deletions need no scan.
The hook uses local Git objects only: it never downloads a scanner or contacts a
provider. Fetch `origin/main` before starting a branch so that baseline is current.
The installed hook refuses a checkout missing the scanner/config or the required
security-gate version, including branches that predate these checks: update that
branch before pushing. Re-run `python scripts/local_ci.py install` to upgrade an
older installed hook. Each scanner invocation has a 120-second deadline and its
Git subprocesses have 15-second deadlines; timeouts block rather than pass.

The vendored stdlib fallback, `scripts/secret_scan.py`, reads the deliberately
small gitleaks-compatible `.gitleaks.toml` ruleset. Gitleaks is not bundled or
available offline on all workers, so the gate needs no binary installation. It
matches AWS, GitHub, OpenAI/Anthropic/OpenRouter, Google, Cloudflare scoped-token
assignments, Telegram/Discord bot tokens, Slack bot/app tokens, Stripe live keys
and webhook signing secrets, password-bearing PostgreSQL URLs and PEM key
material. It is **not** gitleaks' full default/entropy ruleset or a proof that no
secret exists. Diagnostics show only rule, file, line and revision, never the
matching value: metadata is redacted too and control characters are escaped. Ignored local credential files are not whole-tree scan
inputs, but force-adding one makes it tracked and scanned.

For a false positive, add a `[[rules.allowlists]]` entry to the relevant rule in
`.gitleaks.toml`, with a reason, `condition = "AND"`, `regexTarget = "match"`, an
anchored `paths` regex for the specific fixture file and anchored `regexes` for
only the known fake value. Do not exclude entire test/docs directories or use a
real token as an allowlist. Spell the first character as a character class (for
example `[s]k-...`) so the config does not itself contain the matching token.
Short `sk-test`, malformed AWS placeholders and documentation preview tokens
(`v1.<uuid>.<ts>.<hex>`) do not match provider patterns. Add a regression test and
run `python scripts/secret_scan.py`; `--revision origin/main` scans the baseline.
If it is a real credential, revoke/rotate it and remove it from **all outgoing
commits**, not just the latest tree; do not print it in an issue or PR.

The dependency step is the networked part: `scripts/dependency_audit.py` installs
pinned `pip-audit==2.9.0` in checkout-local, ignored `venv/local-audit/` on first
use. It audits `requirements.txt`, any Fluid requirements files, and Fluid's
runtime dependencies from `fluid/pyproject.toml`. These are newly resolved
requirements, not an inventory of an existing worker. It also runs
`npm audit --audit-level=high --package-lock-only --ignore-scripts` for frontend,
scripts/source-publisher, browser-mcp, desktop and tools when their lockfiles exist, including
dev dependencies, without installing node_modules or changing lockfiles. Each
manifest prints one summary; PyPI advisory severity is reported as unknown
because pip-audit does not provide it. No dependency is upgraded automatically.

Findings and unavailable/timed-out audits **warn by default** until triaged.
`python scripts/local_ci.py check --strict-audit` fails on any Python advisory,
any npm high/critical finding, skipped Python packages or an unavailable audit.
Every explicit `check`/`full` refreshes the audit. Automatic pre-push checks never
run dependency audits, even without a cached test pass or when manifests changed:
they print a reminder to run `python scripts/local_ci.py check` for a fresh audit.
This prevents advisory-service/resolver stalls during an otherwise offline push;
the existing test suites may still need their own tools/dependencies. Secret
scanning is never skipped. Pass records record whether an audit was refreshed;
records from older security-gate versions are invalidated. This is local
protection, not GitHub push protection; the hook must be installed on each clone.

## Out-of-reach changes: draft first, owner acceptance before readiness

The pre-push update-reach check reads `gh pr view --json isDraft,labels`.
When the branch has no PR yet or its PR is a draft, an unreachable change is a
prominent **NOTE — OUT OF REACH**, not a push failure. The note names each group
and its required `update-reach:<group>` label. If `gh` is missing, unauthenticated
or cannot read the PR, the check says so and treats it as no PR for this local
push only. Other check failures still fail the push.

1. Push with the hook enabled and open a **draft** PR (`gh pr create --draft`).
2. Move the change into updated source/add a migration path, or ask the owner
   to accept each reported group by adding its `update-reach:<group>` label.
   An agent must not add the acceptance label on the owner's behalf.
3. Re-run `python scripts/local_ci.py check` and, only after acceptance and
   review, have the owner/release agent mark it ready. A ready PR without the
   required labels fails update-reach on subsequent checks/pushes. Cached test
   passes do not cache PR state or labels: pre-push reads them again.

**`gh pr ready` and `gh pr merge` themselves are not blocked; edge publication
is.** There is no GitHub-required PR check (PR CI remains off). The strict
backstop is `scripts/ci/edge_publication_gate.py::update_reach_readiness`, called
by `local_ci.py full --attest` before starting and again before posting success.
It verifies the signed published edge pointer, checks each first-parent main
change since that revision, and requires the matching labels on **that change's
merged main PR**, not a label on an unrelated PR. An unlabeled out-of-reach merge
cannot obtain a new `local/full-suite` success through this path; the error names
the PR and missing labels. In-reach commits need no PR API lookup. Missing,
incomplete or unavailable evidence for an out-of-reach commit refuses the
attestation. Every successful read stores the signed envelope atomically at
`~/.cache/ai-shuffle/verified-edge-manifest.json`. Both fresh and cached envelopes
use the updater's complete `verify_manifest` contract and the existing public
key; a bare cached revision is never trusted. If the feed is unavailable, the
cached envelope is reverified and used with a warning, so already-published
out-of-reach changes do not need GitHub again during an outage. Local release
tags are never publication evidence and cannot advance this baseline.

With neither live nor cached publication evidence, the check warns and examines
all first-parent changes since repository bootstrap, rather than guessing an
unpublished range. This fallback requires complete Git history: a shallow clone
refuses with `git fetch --unshallow origin` as the recovery instruction, because
its oldest visible commit is not proof of repository bootstrap. Partial clones
also need readable historical content: a failed lazy blob fetch refuses
classification, rather than treating a file as absent. Restore access to the
promisor remote or use a complete clone before retrying.
An entirely in-reach history passes without PR API calls;
out-of-reach changes still need their own acceptance labels. This cold-cache
case can conservatively recheck older history, but never skips an unpublished
out-of-reach merge. A pointer outside the candidate's first-parent history
refuses attestation.
This does not revoke an already-posted status
if labels are removed later; the existing publisher consumes the owner's status.

## Full suite on main, and nightly publication

Nightly publication waits for a full local pass on the exact revision it ships.
On a clean checkout of the current public `main` (resolved by repository URL), on any worker:

```bash
python scripts/local_ci.py full --attest
```

This runs every suite, rebuilds the backend test map that later selections
use, and posts the commit status `local/full-suite` (pending while it runs,
then success or failure) through `gh`. "Publish desktop source update"
runs at 06:00 UTC or on owner dispatch. It selects the newest owner-authored
successful status on protected main's **first-parent** history, strictly newer
than the verified published revision (`scripts/ci/edge_publication_gate.py`).
A merge during a full run no longer wastes the ancestor's pass: that exact tree
can ship, but later untested commits wait for their own attestation. One pass
covers every PR merged up to its revision, never subsequent changes. A newer
attested candidate takes precedence over an older run finishing late. Missing,
pending or failed evidence leaves the existing nightly unchanged.

The gate rechecks eligibility before signing and writing; the publisher also
atomically checks the database pointer and enforces increasing signed sequences.
An older revision cannot become a newer update merely by finishing later. See
[release channels](release-channels.md) for the selection and cutover rules.
`local_ci.py` warns when main has moved when it posts pending/success/failure,
and explains that only an eligible ancestor may ship at the next scheduled run.
This does not block bare `gh pr merge`, start publication, or waive update-reach.
Until an eligible pass exists the gate adds an "Edge publication refused"
warning, and DeployStatus repeats it.

Run it before a nightly update is wanted, not after every merge. A status
change does not start publication; the next schedule or owner dispatch reads it.

For a coordinated merge, use the optional read-only preflight first:

```bash
python scripts/ci/edge_publication_gate.py --merge-check && gh pr merge N --merge
```

It refuses a recent owner-created pending full-suite marker on current main,
unknown marker age, or main movement during the check. Markers over two hours
old warn instead of indefinitely blocking merges after a crashed run. Foreign
statuses are ignored. This is not a GitHub merge lock: direct merges and a
merge immediately after the check remain possible, which is why publication
also independently supports safe ancestors and rechecks its own pointer.

## Before a stable release

- A full pass on Linux (`local/full-suite`, above) and on Windows: on
  DesktopWorker, `python scripts/local_ci.py full --step --attest` (repeated
  until it prints `COMPLETE`, see above) also posts `local/windows-suite`. The ship-release
  skill checks both statuses on the release commit. If DesktopWorker is off, the
  release waits, or the owner ships it with
  `ship_release_status.py --skip-windows "<reason>"`, which puts the reason in
  the release notes. Edge never waits for Windows.
- Mac-only checks, when the release changes them: `ios.yml` and
  `ci-folder-confinement.yml`.
- The real-PostgreSQL and container contracts that only run in the hosted
  backend job: `gh workflow run ci-backend.yml --ref main`, when the release
  touches the container runtime, the network database or agent isolation.
- The release workflow itself builds and verifies the container image, the
  buyer install and the Windows installer on a clean Windows runner (see
  [release-channels.md](release-channels.md)).

## Manual workflows

Every workflow other than the release and edge publication is
`workflow_dispatch` (or called by a release). They exist for the cases above
and for platform checks a local machine cannot run, for example
`android.yml` for emulator verification, `desktop.yml` for the packaged
Windows preview, `ci-browser-workspace.yml` and `container-postgres.yml`.
Dispatch one only when a local run cannot answer the question, never to find
out whether tests pass.

## Visual regression screenshots (local, not a CI job)

`cd frontend && npm run test:visual` renders the standalone fixtures (Git
panel, turn navigator, side panel, composer, usage rows) at desktop
(1440×900), phone (412×915) and the Galaxy Z Fold 6 inner (884×1104) and
cover (360×882) screens, opens the panel or popover under test, and compares
each with `frontend/tests/visual-baselines/`. Run it before and after chat UI
work; accept intended changes with `node tests/visual-regression.mjs --update`
and commit the new baselines. Pass a fixture name to run one
(`node tests/visual-regression.mjs usage-rows`). It uses the browser runtime's
playwright-core and Chromium, freezes the page clock, and is deliberately not a
CI job to keep runner minutes down.
