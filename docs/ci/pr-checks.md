# Where tests run

Tests run on GitHub CI. The local push gate is the offline secret scan (plus the
update-reach check); it runs no test suites. Every pull request, including drafts
and forks, runs the hosted gates in parallel. Backend
shards, real contracts and security all run unconditionally, regardless of paths.
Merge requires green CI and review. Edge/nightly publication remains separate:
a hosted full-suite pass on the exact main revision or the owner's local
attested full pass authorizes the read-only publication gate.

PR workflows use only free standard GitHub-hosted `ubuntu-latest` and
`windows-latest` runners. No larger/paid runners and no macOS PR jobs are used.
macOS and iOS native checks remain manual. Docker uses the official Buildx and
build-push actions with branch-isolated GitHub Actions layer caching.

## Every pull request

These workflows retain manual dispatch and also run on unfiltered
`pull_request` events (no path filters or draft skips):

- `ci-backend.yml`: four backend shards, parallel real PostgreSQL/boot contracts,
  unconditional security checks and the stable fail-closed `Backend tests` gate.
- `ci-fluid.yml`: the complete fluid unit suite in its isolated environment.
- `ci-frontend.yml`: four full frontend Vitest shards, production build (including
  mobile postbuild), frontend/mobile typechecks, mobile tests, contracts/docs/audit,
  tools/WebCrypto vectors and Browser MCP run in parallel. The stable
  `JavaScript package checks` aggregate requires every branch to succeed.
- `desktop.yml`: desktop typecheck and unit tests; packaging stays manual.
- `desktop-source-checks.yml`: source-update protocol regressions.
- `windows-suite.yml`: three native desktop shards on PRs, with a fail-closed
  `Windows desktop` aggregate. Source-update recovery cases share an isolated
  fixture across nine duration-balanced files. Actual executed-file inventories
  must form an exact disjoint union; the aggregate reports runtime test counts
  and verifies the moved test-body snapshot. The full manual Windows matrix is
  unchanged (Internal tracking: #381). Linux PR checks still run the complete
  backend suite on every PR. No assertions or failure behavior
  are weakened.
- `ci-instance-identity.yml`: native Windows identity contracts; macOS is manual only.
- `ci-artifact-policy.yml`: inexpensive artifact, runner and publication policy checks.
- `ci-browser-workspace.yml`: editor smoke plus independent `external`,
  `panel-demand` and `direct-page-demand` real Chromium/rendered-UI cases. Each
  retains breadcrumb and provisioned shadow-root auth-editor checks. The stable
  `Real browser workspace` aggregate requires smoke and all three cases.
- `container-postgres.yml`: candidate image lifecycle, isolation and install checks,
  without publishing or signing. This is broader than a small policy check but
  covers the production container path without requiring secrets.

`ci-folder-confinement.yml` stays manual because it is macOS-only; Linux folder
confinement is already covered by CI Backend. Stable release authorization is unchanged.
Each PR workflow groups concurrency by workflow plus PR number (or ref for manual
runs), cancelling superseded PR runs only. Permissions are `contents: read`;
none of these workflows references secrets or requires private checkout access.
Fork PRs use the ordinary read-only `pull_request` context, never
`pull_request_target`.

Cloud service checks and deploy workflows live in the private
[HobbyCoders/hobbycoders-cloud](https://github.com/HobbyCoders/hobbycoders-cloud)
repository. App checks do not need that checkout. When app docs/install files
change, refresh the committed website snapshots there using `AI_SHUFFLE_CHECKOUT`
and its README; the app still checks `docs/content` against the frontend copy.

## Hosted main-suite

`main-suite.yml` runs once per push to `main`, or an explicit dispatch on `main`.
A dispatch on another ref skips the suites and fails the aggregate. Superseded
main runs may be cancelled. The root workflow calls the same checked-in reusable
CI workflows; their PR triggers, matrices, full test selection and gates remain
in force. No test job has signing secrets; permissions are `contents: read`,
using only standard free hosted runners. Windows calls select the existing
three desktop shards, not the separate stable-release portability matrix.

| Local `full` stage | Hosted job / reusable workflow |
| --- | --- |
| backend | `backend` → CI Backend's four shards and security (scanner + scanner tests) |
| backend-linux | `backend` → contracts (real PostgreSQL boot/schema, serial container tests) and security (real bubblewrap) |
| fluid | `fluid` → complete isolated fluid unit suite |
| frontend | `javascript` → four Vitest shards, production build, typechecks and contracts |
| mobile | `javascript` → mobile tests/typecheck and frontend production postbuild |
| tools + network vectors | `javascript` → Tools tests and WebCrypto vectors |
| browser-mcp | `javascript` → Browser MCP build and full tests |
| docs | `android-and-docs` → exact local docs-copy helper; JavaScript contracts also compare copies |
| desktop | `desktop` → existing typecheck and all unit tests |
| android-bridge | `android-and-docs` → existing local Node download-bridge tests |
| android-play | `android-and-docs` → all six local Python/workflow-shell test scripts |
| workflows/policy | `policy` → artifact/cost scripts and the complete local seven-file policy set, plus parity regressions |
| update-reach | `update-reach` → regression set and strict signed-publication/merged-PR readiness (not advisory) |
| full preflight security | `supply-chain` → offline secret scan and identical report-only dependency audit, including source-publisher |
| android-app (SDK optional) | Explicitly not covered by the reference Linux attestation; remains separate `android.yml` Gradle/native checks |

Additional existing gates are required too: Windows desktop (three shards),
real browser workspace (three cases + smoke), container lifecycle/buyer install,
Windows identity and desktop-source protocol checks. `Main suite` uses `always()`
and requires every called workflow and parity job to succeed. Missing, failed,
cancelled or skipped dependencies fail closed; the checked-in parity inventory
also catches new local stages without a mapping. A backend test-map cache is a
local optimization, not additional test evidence.

The reference Linux full-attest log explicitly excluded Android SDK/Gradle,
iOS/macOS native tests, Windows and container image assembly; those exclusions
are not silently claimed as hosted parity. The hosted run adds Windows and
container image coverage but does not replace owner-controlled stable release
requirements. Container PostgreSQL pressure tests remain included as already
configured by CI Backend (the local run reports these as opt-in).

No ordinary stage needs private source or owner-machine material. One historical
exception remains unchanged: the initial public-root update-reach bridge needs
a complete private archive locally. Hosted reach refuses if that bridge is still
needed. Once the signed nightly pointer is in public history, all ordinary reach
checks use public Git history, signed manifests and merged-PR metadata.

After a successful main run, `desktop-source.yml` starts via `workflow_run` and
re-verifies canonical repository, main/push-or-dispatch identity, root workflow
path/ID, exact SHA, latest run/attempt and successful aggregate through the API.
The latest attempt number and URL are included in publication logs. Owner-local
`local/full-suite` successes remain accepted independently. The newest terminal
owner outcome decides, ignoring pending retries: failure/error vetoes that exact
revision even if hosted CI passed; a newer commit or later owner success
unblocks it. Without a terminal owner outcome, hosted evidence can still pass. Stable staging and Linux/Windows local release
requirements are unchanged.

## Hosted browser and PostgreSQL prerequisites

The parallel JavaScript package branches retain 20-minute job limits. Each
frontend shard proves that Vitest's configured sequencer partitions the complete
unsharded file inventory without duplicates or omissions, including dynamic docs
consumers. The five-minute aggregate fails on failed, cancelled, skipped or missing
branches. Browser MCP runs independently on another standard runner with a
15-minute job budget and feeds that aggregate. Managed Chromium uses a
lockfile-keyed browser cache, while OS dependencies are installed on every run.
The three workspace cases reuse the same browser-cache policy and always install
OS prerequisites; each retains its 25-minute limit and real-browser/UI opt-ins.
Windows desktop shards retain 60-minute limits, cache pip/npm downloads by
requirements/lockfiles, and build independently before Node's native sharding.
No runner shares mutable `node_modules`; mobile links the frontend install via
`npm run deps`.
OS prerequisites have at most two 300-second attempts; browser downloads retain
their single 90-second deadline. Prerequisite steps allow 13 minutes for both
attempts, termination grace and browser download; overall job and test deadlines
are unchanged. This increases only the package-install budget, not test or security
limits: sampled completed installs took 24–31 seconds, but slow mirrors exceeded
both old 90-second attempts. At the observed approximately 150 kB/s transfer rate,
25.9 MB alone needs nearly three minutes, before index refresh and configuration.
The OS timeout runs as root so it can reap apt children; browser downloads remain
runner-owned and cached. OS packages use the primary signed Ubuntu archive
instead of the hosted Azure mirror fallback. Browser MCP build/tests
have a separate eight-minute step and a 420-second test deadline with
an explicit error on failure. Ubuntu's AppArmor unprivileged-user-namespace
restriction is disabled only on the disposable runner so the sandboxed editor
fixture can launch. CDP fixtures honor `CHROMIUM_PATH` instead of preferring a
distro/snap wrapper over the managed browser. Production browser security
settings are unchanged. Fluid's
ripgrep install uses the same bounded policy. These installs, every
browser-workspace matrix case, and backend security's missing-bubblewrap install
share `scripts/ci/bounded_apt.py`. Root npm writes use a disposable temporary cache,
never the runner's `~/.npm`. Its apt config
installs only missing packages (`--no-upgrade` semantics), reusing runner-image
libraries rather than downloading newer candidates, and refreshes only signed
Ubuntu indexes without translations. apt still verifies and installs missing
packages normally; no system directories or package databases are cached.
Its elevated supervisor kills the entire attempt's process group (including root
apt children) with a ten-second termination grace and logs elapsed install times.
Lock waiting and `dpkg --configure -a` after an interrupted attempt are included
in the next attempt's 300-second budget, not added to it. Both failures still fail
the step; lock files are never deleted.

Frame visibility consumes any already-computed `IntersectionObserver` records
before its 250 ms watchdog disconnects the observer: a due timer may run before
the separate notification task on a busy page. The deadline and geometric
visibility checks are unchanged; no rendering result still fails closed and is
reported as a skipped frame, not readable text. Real-browser regressions retain
closed-shadow clipping assertions and cover delayed delivery and missing records.

Real PostgreSQL fixtures initialize mobile-tool provenance through the production
schema helper and include the audit retention primary key. Network pruning routes
shared retention batches into the fixture's disposable schema too. Container isolation
probes materialize lazy protected keys through application helpers before
checking access denial. These repairs cover #389, #390 and #391; those jobs are
required to pass, not temporary exceptions. Network migration and mobile contracts
run in the parallel backend-contracts job for backend/shared changes. Pool safety
and both Python dependency audits remain unconditional in backend-security.

### Unconditional backend shards and contracts

Every PR runs the full backend, including docs-only, frontend-only, desktop-only,
tools-only and workflow-only changes. Backend credential-path checks and contracts
read those trees too, and `docs/content/` is served at runtime. There is no path
classifier, skip approval, or reduced-coverage lane.

Full coverage runs four `ubuntu-latest` shards with two xdist workers apiece and
`fail-fast: false`, plus serial real boot/schema, checkpoint/crash recovery, Mobile
PostgreSQL and network migration contracts on a separate runner. Each worker
partitions the actual unsharded collection using `scripts/ci/backend_shards.py` and
proves exact union, no duplicates and no empty shard. Workers must agree on the
collection and assignment. Successful controllers also verify executed node IDs,
then upload per-shard receipts, timings and JUnit. The unchanged hermetic exclusions
still run with their stronger real-environment prerequisites. The legacy dispatch
input remains accepted; shard parallelism is fixed at two workers.

`backend-security` always runs the blocking-DB guard, real bubblewrap escapes,
required root/real-account isolation, real PostgreSQL pool/transaction cases,
Python public and voice-lock audits, pinned workflow syntax/action lint, and the
full workflow/publication/security/docs policy regression suite. Required flags,
Python permission hardening and 35-minute heavy-job timeouts are retained. Every
hosted pytest phase emits `--durations=50` and JUnit; artifacts expire in seven days.

The stable `Backend tests` gate uses `if: always()` and depends on all shards,
contracts and security. `scripts/ci/backend_gate.py` requires every dependency to
succeed and all four successful shard receipts. Failed, cancelled, missing or
skipped jobs, missing/duplicate receipts, inconsistent collection/partition
digests and incomplete counts all fail closed. No classifier output can approve
a skipped job.

`tests/backend-durations.json` is versioned scheduling data, never execution
authority. Its keys are SHA-256 hashes of node IDs: parametrized IDs can contain
synthetic credential-scanner fixtures, which must not be republished in the
manifest. Refresh validates the exact union of raw executed IDs before hashing;
collection always starts from the actual tests, not the manifest. Unknown/new
tests receive at least ten seconds or the maximum observed duration, and are
never dropped. The initial seed is taken once from a complete
successful PR CI run; subsequent refreshes accept only successful main CI Backend
runs, downloading fresh artifacts rather than trusting a PR-controlled cache:

```bash
python -m scripts.ci.backend_shards --refresh RUN_ID --download-dir NEW_SCRATCH_DIR
```

Review and commit the refreshed manifest. Timing changes can rebalance the actual
collection but cannot select away tests or satisfy the gate without execution.

## Before every push

```bash
python scripts/local_ci.py install   # once per clone: turns on the pre-push hook
python scripts/local_ci.py list      # what each optional local suite runs, and when
```

The pre-push hook is the local push gate and runs no test suites: every PR runs
them on GitHub CI (the workflows above). For every pushed ref, in order:

1. The offline secret scan of every outgoing commit (see
   [local security checks](#local-security-checks)). It runs first, on every
   push, with no cache; a finding refuses the push. Tags and pr-assets refs get
   it like any branch; deletions need no scan.
2. For branch pushes, an offline scan of the whole working tree (tracked plus
   non-ignored untracked files), as `check` and `full` run.
3. For branch pushes, the live update-reach check: a ready PR missing an owner
   acceptance label is refused; drafts are advisory. PR CI does not run this
   check, so it stays local. Its base is refreshed first from the push remote's
   main; URL pushes and push remotes without a usable main fall back to the
   configured comparison ref.

No pass record can skip or replace any of these steps. Together they take about
20 seconds of CPU on a full checkout.

The suites the hook used to select locally run in full on every PR:

| Former push-gate suite | PR CI job |
| --- | --- |
| backend | CI Backend's four shards (`tests/api` … `tests/scripts`, `tests/test_*.py`) |
| backend-linux | CI Backend contracts and security (real PostgreSQL, container contracts, bubblewrap) |
| fluid | `ci-fluid.yml` |
| frontend, mobile, tools, browser-mcp | `ci-frontend.yml` (Vitest shards, typechecks, mobile, tools, Browser MCP) |
| docs | `ci-frontend.yml` contracts/docs and `ci-artifact-policy.yml` (`main_suite.py android-and-docs`) |
| desktop | `desktop.yml` |
| android-play, android-bridge | `ci-artifact-policy.yml` (`main_suite.py android-and-docs`) |
| workflows | `ci-artifact-policy.yml` policy scripts, CI Backend shards and security policy step (`tests/scripts`) |
| update-reach regressions | CI Backend shards (`tests/test_update_reach.py`); the verdict itself stays in the push gate |

`android-app` (Gradle unit tests) never ran without a local Android SDK and stays
a manual check (`android.yml`), as before.

`fast` stays available as an optional, explicit command (affected tests plus
the offline scan). It compares against the configured public comparison ref's
merge-base and reuses the existing `SUITES` area map. Backend files use the
recorded dependency map when available, otherwise per-file test-name/import
matching. Changed test files select themselves, and helper modules under
`tests/` select their importers (including relative and bare imports) and
matching test filenames. Fluid source and non-Python files run the Fluid suite;
Fluid Python modules also select matching backend tests. An unmapped path adds
no extra local tests and prints
`No local test mapping for X; covered by the full suite on PR CI`. Unknown
top-level paths use the same note rather than running all local suites.
Requirements, `pytest.ini`, and backend `conftest.py` changes still select the
whole backend area. Other areas run their unit suites; frontend and mobile use
all area tests for dynamic source-reading coverage, without a production build.
Docs paths and Markdown/reStructuredText files run docs checks only.

`check` remains the existing changed-suite check with dependency audits, and
`full` still runs every local suite and records the backend dependency map.
Comparison overrides (`LOCAL_CI_BASE` or `git config local-ci.base`) remain
available for explicit checks. `full --attest` ignores these overrides and
requires a clean checkout at the public repository's current main revision.
Attestation format and publication authorization are unchanged.

The suites are: backend (the same function-level selection the backend test
map gives), fluid, frontend, mobile, tools, browser-mcp, docs, desktop, the
Android app and download bridge, the workflow policy tests and the in-app update
reach check. It runs at low CPU priority with at most two test workers
(`LOCAL_CI_WORKERS`), because every worker host is shared with other sessions
and with other workloads on the same machine.

Local parallel pytest controllers also reserve a machine-wide slot before
starting xdist workers. Capacity defaults to
`max(1, min(4, cpu_count // 8, total_RAM_bytes // (12 * 1024**3)))`.
CPU count is bounded by process affinity where supported. One slot per eight
usable logical CPUs and 12 GiB RAM leaves conservative headroom;
**total** RAM makes the capacity stable while other tests allocate/free memory.
If CPU or RAM detection is unavailable, capacity falls back to one. Set
`AI_SHUFFLE_PYTEST_SLOTS` to an integer >= 1 to override it consistently across
checkouts; invalid values use the machine default. For example, 22 logical CPUs
and 62 GiB RAM permit two simultaneous runs.

Each gated run is capped at **two xdist workers**, including `-n auto` and
`-n logical`: at most `2 * slots` workers (eight with the default maximum),
plus controllers and any subprocesses tests launch. The existing `-n 0`/`-n 1`
exemption is unchanged. Files `ai-shuffle-pytest-xdist.lock.0` through
`.N-1` in the system temp directory use OS locks, released automatically even
on a crash; do not delete held lock files. Waiters scan all slots, rotate their
starting slot and jitter bounded polling to avoid lockstep races. Admission is
best-effort fair, not strict FIFO. Existing progress notices, the 20-minute
fail-open timeout, and fail-open lock errors remain: the bound is not a hard
resource quota. `GITHUB_ACTIONS` and `AI_SHUFFLE_PYTEST_GATE=0` still bypass the
gate. All checkouts must use this gate version and the same slot override/temp
directory to share the budget; older unsuffixed-lock gates do not coordinate
with these slots.

### Disposable test scratch and previews

Pytest controllers allocate a unique `ai-shuffle-pytest-*` basetemp and remove
it after fixture and xdist worker teardown, including failed runs. Workers use
the controller's base rather than competing for a shared numbered directory.
Retention is explicitly `none` / `0`. An explicit `--basetemp` remains caller-owned.
Fluid's separate pytest root loads the same scratch plugin, including when tests
are invoked from `fluid/`; it does not load the app's test fixtures. A standalone
Fluid copy without the app's scripts falls back to pytest's normal temp handling.
`local_ci.py` gives each child command a unique `ai-shuffle-ci-*` TMPDIR/TEMP/TMP
and removes it after stopping failed, timed-out or interrupted children. It
preserves the original shared xdist lock directory in
`AI_SHUFFLE_PYTEST_GATE_DIR`; private scratch must not create private admission
slots. Independently owned pytest/browser scratch uses the original temporary
root (`AI_SHUFFLE_TEST_SCRATCH_ROOT`) rather than nesting beneath command scratch,
so Unix-domain socket paths stay short. `npm test` in browser-mcp similarly owns
its browser/Node scratch.

Normal exit, SIGTERM, pytest's Ctrl-C handling and local-ci command failure run
cleanup. Pytest's SIGTERM handler exits the whole session with code 143 (serial
and xdist), rather than failing one test and continuing; local-ci retains its
SystemExit-based unwind. Staged/custom stop handlers are preserved. SIGKILL and host crashes
cannot run finalizers. Approved workers periodically sweep only private (0700),
allowlisted, ownership-receipted scratch older than six hours, skipping live
owners, process cwd/open descriptors, symlinks, checkouts and separate git stores.
Set `AI_SHUFFLE_SCRATCH_REAPER=0` to disable this maintenance task.

Sweeps separately bound processes (10,000), descriptors (100,000), tree entries,
removals and elapsed time. Uninspectable unrelated non-root UIDs cannot enter
these private trees. The only uninspectable root processes exempted are known
init ancestors (`tini`, `docker-init`, or PID 1 `init`/`systemd`): they predate the
scratch, cannot inherit its descriptors/cwd, and are trusted not to open it later.
Normal app startup makes processes non-dumpable. Readable status and exact
Python entry-point argv identify same-user `app.main` instances (including
runners), `app.preview_process`, and the exact agent-isolation app shim; these
do not block sweeping other prefixes. Their own live-owner receipts protect
scratch, but an orphaned preview app can retain a dead launcher's preview tree.
Whenever the scan exempts an opaque `app.main` or `app.preview_process`, it skips
all `ai-shuffle-preview-*` trees for that sweep; pytest/CI and other prefixes
remain eligible. The non-dumpable `docker/container_runtime.py run` supervisor is also
exempt only when it is an ancestor: it starts/reaps children without opening
their scratch. Other uninspectable root or same-user processes fail closed,
logging the disabled sweep once per worker. A process retaining scratch cwd/fds
before switching all its UIDs to another user remains a known narrow gap.
These sweeps do not clean generic `tmp*`, `probe-`, `cdp-`, `ci-` or legacy
unmarked trees.

For a disposable Linux preview, build the frontend, run
`python -m app.preview stamp`, then use
`python -m app.preview run --port 18080 --ttl-seconds 3600` under a Monitor task.
It creates its own HOME, data directory and PostgreSQL without inheriting
credentials. Keep the launcher alive: parent loss, task termination, Ctrl-C or
TTL expiry closes the app and cluster. The TTL is capped at four hours. Raw
`AI_SHUFFLE_PREVIEW=1 python -m app.main` remains a caller-managed instance;
this command cannot adopt or terminate somebody else's existing preview.
SIGKILL of the launcher still relies on Monitor's process-tree teardown.

A `check` or `full` pass on a clean checkout is recorded against the commit's
tree in `.git/local-ci/passes.json`; the push gate never reads it. The pre-push
hook (`.githooks/pre-push`, which `install` copies into the directory git runs
hooks from, next to AI Shuffle's managed hooks; an older `pre-push` there is kept
as `pre-push.user` and still runs afterwards; `install` also saves a Python that
has pytest and PyYAML in git config `local-ci.python`, which the hook and later
runs use) accepts any pushed commit, including one other than `HEAD` or from a
checkout with uncommitted changes, once the steps above pass. Never skip the
hook with `--no-verify`.

Agents push directly; no local test run is required before a push. The secret
scan still runs on every outgoing push.
The installed hook is shared across worktrees and delegates to each branch's
own script; older branches retain their old gate until updated. Do not reinstall
the shared hook as part of a branch change.

For `check` and `full`, the selected Python is checked **before suites or commit statuses**: all required
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

A change to the installed desktop launcher's inputs (group
`windows-launcher`, which covers the packaged Windows and Linux apps) must also
raise `desktop/launcher.json` `version` in the same pull request. These inputs
include its import closure, runtime pages under `desktop/src/pages`, icon,
update trust key, `scripts/source-profile.cjs`, launcher policy fields, and the
production npm packages in `desktop/package-lock.json` (name, version and
integrity). Dev-only dependency changes do not count. The check also validates
`minimum <= version` and, when present, `start <= version` at both revisions. Otherwise the
check also fails `launcher-version`, and `update-reach:windows-launcher` does not
accept it. The only other way through is the owner's
`update-reach:no-launcher-change` label (or `--allow no-launcher-change`): the
change leaves what the launcher does unchanged, such as a move or a comment.
That label accepts both groups.

Changing the locked `node_modules/electron` version in `desktop/package-lock.json`
also fails `linux-electron`. Packaged Linux source updates keep the installed
Electron and its sandbox, so the owner must accept that a matching installer is
needed with `update-reach:linux-electron`. Unrelated lockfile changes do not
trigger this gate, and it does not by itself require a launcher version bump.


## Local security checks

Every `check` (including `--only`) and `full` first scans the whole working tree
(tracked files plus non-ignored untracked files) for leaked credentials. The
pre-push hook first scans the changed blobs in **every outgoing commit**, then
the whole working tree for branch pushes; no pass record can skip either. A token added and removed in separate commits
still blocks the push. Existing refs use the remote tip through the pushed tip.
New branches and tags exclude commits already on the local `main` tracking ref of
the remote being pushed to (`public/main` for `git push public ...`). If that ref
is unavailable, for example on a remote that was never fetched, they scan all
reachable history. The v4 hook marks its remote argument; ambient remote values
without that marker are ignored. The scanner checks the name against `git remote`;
unknown names, bare URLs and repository paths scan all reachable history rather
than using a tracking ref. A hook installed before v4 keeps using `origin/main`.
When `remote.X.pushurl` differs from its fetch URL, the boundary comes from the fetch side.
Tags are scanned; deletions need no scan.
The hook uses local Git objects only: it never downloads a scanner or contacts a
provider. Fetch the push remote's `main` before starting a branch so that baseline
is current. This boundary assumes a fresh fetch: if main was force-pushed back
and has not been fetched, the stale tracking ref can hide commits the remote no
longer has. That limitation also applied to the previous `origin/main` boundary.
The installed hook refuses a checkout missing the scanner/config or the required
security-gate version, including branches that predate these checks: update that
branch before pushing. Re-run `python scripts/local_ci.py install` to upgrade an
older installed hook. The installer refuses to replace a newer shared hook unless
`install --force` is given. Installers on branches predating that version check
cannot enforce it; update those branches before running install. Each scanner
invocation has a 120-second deadline and its Git subprocesses have 15-second
deadlines; timeouts block rather than pass.

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
  skill checks both statuses on the release commit. Without DesktopWorker, the
  owner can run the same suite on a GitHub-hosted Windows runner (free in public
  repositories): `gh workflow run windows-suite.yml --ref main` on the release
  commit. `ship_release_status.py` counts a successful run the owner dispatched
  on main at the candidate as `local/windows-suite`, and the release notes name
  the run. The hosted run is split into parallel parts
  (`scripts/ci/windows_parts.py`: eight backend parts, frontend, desktop, and the rest),
  so it takes the time of the slowest part. A failed part does not stop the
  others: `gh run rerun <run id> --failed` reruns only the failed parts, and the
  run's newest attempt counts. A failed pytest command there reruns just its
  failed tests once; a test that then passes is reported as flaky in the job
  summary and should still be fixed. Local gates never retry. Otherwise the release waits, or the owner ships it with
  `ship_release_status.py --skip-windows "<reason>"`, which puts the reason in
  the release notes. Edge never waits for Windows.
- Main stays open during a release. A suite run starts on main's tip and can
  keep going after main moves on:
  - the one-shot `full --attest` posts its status on the revision it tested;
  - a staged `full --step --attest` that has already started finishes its
    later chunks on that revision, as long as it is still on public main's
    first-parent history.

  Once the suites and live smoke have passed on one merged revision,
  `ship_release_status.py --candidate <sha>` checks every gate on that
  revision instead of main's tip. Tag that SHA. Later merges ship in the next
  release and never reset the gates, so no session has to hold its merges.
  One limit: a gate that is still missing on a pinned revision behind main
  can't be produced afterwards, because new runs start on the tip. Pin a
  revision that already passed, or test the tip.
- Mac-only checks, when the release changes them: `ios.yml` and
  `ci-folder-confinement.yml`.
- The Linux `backend-linux` local suite runs both container runtime and image
  contracts serially. `check` runs their plain cases for container/startup changes;
  `full` runs the plain cases and, when local PostgreSQL binaries are available,
  the real-PostgreSQL functional cases by default. Checkpoint and FAST-shutdown
  timing measurements require
  `LOCAL_CI_CONTAINER_PRESSURE=1 python scripts/local_ci.py full`.
  These create disposable clusters, not connections to a configured database.
  A full run with a requested PG/pressure opt-in but no binaries fails clearly
  (`LOCAL_CI_CONTAINER_PG=1` remains a way to require binaries). Missing coverage
  gets an explicit note; disabled measurements name #350 and their opt-in.
  Staged full receipts include both opt-ins in their identity, so changing them
  reruns the gate. Checkpoint observations use a dedicated 60-second lab command
  budget, not the product's generic 10-second SQL budget. Independently selected
  FAST-shutdown measurements dirty the database and call the unchanged production
  30-second shutdown path while pressure writers are still active. Failure to acquire
  an idle interval produces an explicit lab-precondition skip, not a product failure;
  observation deadlines and real FAST-shutdown failures remain failures with
  distinct diagnostics.
  The manual hosted backend job still covers other network-database and agent
  isolation contracts when a local run cannot answer the release question;
  do not dispatch it to discover failures.
- The release workflow itself builds and verifies the container image, the
  buyer install and the Windows installer on a clean Windows runner (see
  [release-channels.md](release-channels.md)).

## Manual workflows

Every workflow other than the release and edge publication is
`workflow_dispatch` (or called by a release). They exist for the cases above
and for platform checks a local machine cannot run, for example
`android.yml` for emulator verification, `desktop.yml` for the packaged
Windows preview (or, with `verify_stable_installer`, the stable installer's
Stable/Nightly choice in silent installs), `ci-browser-workspace.yml` and `container-postgres.yml`.
Dispatch one only when a local run cannot answer the question, never to find
out whether tests pass.

## Visual regression screenshots (local, not a CI job)

`cd frontend && npm run test:visual` renders the standalone fixtures (Git
panel, turn navigator, side panel, composer, usage rows) at desktop
(1440×900), phone (412×915) and the Galaxy Z Fold 6 inner (884×1104) and
cover (360×882) screens, opens the panel or popover under test, and compares
each with `frontend/tests/visual-baselines/`. Run it before and after chat UI
work; accept intended changes with `node tests/visual-regression.mjs --update`
and commit the new baselines in the same PR (they are test fixtures, not PR
evidence, so the no-screenshots rule in docs/internal/CONTRIBUTING.md does not
apply to them). Pass a fixture name to run one
(`node tests/visual-regression.mjs usage-rows`). It uses the browser runtime's
playwright-core and Chromium, freezes the page clock, and is deliberately not a
CI job to keep runner minutes down.
