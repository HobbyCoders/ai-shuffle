# Where tests run

Tests run on the machine where the change was made, before it is pushed.
GitHub Actions does not test pull requests: no workflow starts on a pull
request, a push to a branch or a schedule. CI minutes go only to work no
local machine can do: release builds, signing, publishing, deploys, and the
macOS and iOS platform checks.

## Before every push

```bash
python scripts/local_ci.py install   # once per clone: turns on the pre-push hook
python scripts/local_ci.py           # the suites this branch's changes reach
python scripts/local_ci.py list      # what each suite runs, and when
```

`local_ci.py` compares the branch with `origin/main` and runs each suite the
change reaches: backend (the same function-level selection the backend test
map gives), fluid, frontend, mobile, tools, browser-mcp, docs, desktop, the
Cloudflare services, Android, the workflow policy tests and the in-app update
reach check. It runs at low CPU priority with at most two test workers
(`LOCAL_CI_WORKERS`), because every worker host is shared with other sessions
and, on Unraid, with everything else the server runs.

A pass on a clean checkout is recorded against the commit's tree in
`.git/local-ci/passes.json`. The pre-push hook (`.githooks/pre-push`, which
`install` copies into the directory git runs hooks from, next to AI Shuffle's
managed hooks; an older `pre-push` there is kept as `pre-push.user` and still
runs afterwards; `install` also saves a Python that has pytest and PyYAML
in git config `local-ci.python`, which the hook and later runs use) lets a
recorded commit through at once. For an unrecorded commit that is the clean
`HEAD`, it runs the checks and pushes only if they pass. Anything else (a
commit other than `HEAD`, or uncommitted changes) is refused, because the
checks would not test what is being pushed. Never skip the hook with
`--no-verify`.

Agents run `local_ci.py` through the Monitor tool before pushing, so the hook
finds a recorded pass instead of running a long check inside a shell call.

The last lines of each run list what this machine did not cover:

- Windows, when the run is not on a Windows worker. Edge publication does not
  wait for Windows; a stable release does (below). When DesktopWorker is off,
  Windows-only changes still merge and reach edge, and the gap shows in the
  pass record and in the ship-release preflight.
- On Windows, the tests in `scripts/ci/windows_known_failures.txt`. They fail
  there for reasons unrelated to the change (fake `gh` shell scripts,
  symlinks, POSIX modules), so a Windows run skips them and names
  [#2215](https://github.com/HobbyCoders/ai-shuffle/issues/2215). Linux
  workers still run them, and a Windows failure that is not on the list
  still fails the run. Remove lines as the tests are fixed; never add one to
  get a push through. The desktop unit tests do not run on Windows yet
  ([#2214](https://github.com/HobbyCoders/ai-shuffle/issues/2214)); Windows
  typechecks and builds desktop, and a Linux worker runs its tests.
- macOS folder confinement and the iOS app: no Mac. The owner dispatches
  `ci-folder-confinement.yml` or `ios.yml` when a change needs them, usually
  before a release.
- The container image: the release workflow builds and verifies it, or run
  `python scripts/verify_container_postgres.py` where Docker is available.

An update-reach failure is acknowledged as before, with a pull request label
`update-reach:GROUP` (`local_ci.py` reads the PR's labels) or
`--allow GROUP` when running `scripts/ci/update_reach.py` directly.

## Full suite on main, and edge publication

Edge publication waits for a full local pass on the exact `main` revision.
On a clean checkout of the current `origin/main`, on any worker:

```bash
python scripts/local_ci.py full --attest
```

This runs every suite, rebuilds the backend test map that later selections
use, and posts the commit status `local/full-suite` (pending while it runs,
then success or failure) through `gh`. "Publish desktop source update"
starts on that status and publishes when the status comes from the owner's
account, says success, and names the current head of protected `main`
(`scripts/ci/edge_publication_gate.py`). One pass covers every PR merged up
to that revision. Until then the gate adds an "Edge publication refused"
warning naming the command, and DeployStatus repeats it.

Run it when an edge update is wanted, not after every merge.

## Before a stable release

- A full pass on Linux (`local/full-suite`, above) and on Windows: the same
  command on DesktopWorker also posts `local/windows-suite`. The ship-release
  skill checks both statuses on the release commit. If DesktopWorker is off, the
  release waits, or the owner ships it with
  `ship_release_status.py --skip-windows "<reason>"`, which puts the reason in
  the release notes. Edge never waits for Windows.
- Mac-only checks, when the release changes them: `ios.yml` and
  `ci-folder-confinement.yml`.
- The real-PostgreSQL and container contracts that only run in the paid
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
