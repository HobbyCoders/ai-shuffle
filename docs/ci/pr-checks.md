# What CI runs, and when

Most test suites run on paid Blacksmith runners, so pull requests get only the
fast checks. The browser editor smoke, license server, registry and uptime monitor
checks use standard GitHub `ubuntu-24.04` runners; their paths, tests and manual runs are
unchanged. These consume GitHub Actions allowance rather than Blacksmith
minutes (runner overages follow the platform billing policy). The slow platform suites run
when someone asks for them, and before a release.

## Draft pull requests run no paid jobs

Every workflow that uses a Blacksmith runner skips draft pull requests.
Marking a PR ready for review starts its checks, and later pushes run them as
usual. Agents should open PRs as drafts, push as often as they like, and mark
the PR ready once the targeted local tests pass
(`gh pr ready <number>`). Converting a PR back to draft stops new runs.

When a check fails on a ready PR, reproduce it locally (the failing test file,
or the job's command) and push only once it passes there. Pushing guesses
reruns every paid job each time, and repeatedly pushing the same failure
wastes CI resources on one
branch. If the fix needs several tries, convert the PR back to draft first
(`gh pr ready --undo <number>`).

## On every pull request (path-filtered)

- CI Backend: the backend tests the change can reach. A manual full run on
  `main` records which functions each test file runs; a PR compares its
  changed Python files with the base, function by function, and runs the test
  files that ran a changed function, plus changed test files
  (`scripts/ci/backend_test_map.py`). A changed non-Python file selects the
  tests of the code that names it in a string literal; a comment, docstring
  or message that mentions it selects nothing. It runs the whole fast subset when no
  map exists yet, when dependencies, pytest settings or `conftest.py` change,
  when the change touches code a session-wide fixture runs, or when most of
  the suite is affected anyway. The job summary lists the selected files and
  why.
- CI JavaScript: the frontend, mobile, tools and browser-mcp packages that
  changed. Builds, type checks and the contract check run in full; the
  frontend and mobile unit tests run only the files the change reaches
  (`frontend/scripts/vitest-changed.mjs`: Vitest's import graph, plus tests
  that read a changed file as text). A lockfile or Vite, Vitest, Svelte or
  TypeScript config change runs every test.
- CI fluid: the fluid unit suite
- Folder confinement (macOS): the folder and upload confinement tests on a
  native Mac, only when `app/core/chat_files.py`, `app/core/confined_files.py`
  or their tests change. It is the one macOS job that runs on pull requests.
- Cheap jobs inside Android CI, Desktop CI and CI Browser Workspace: the
  Android build and unit tests when APK inputs change, the account hub and
  website tests, the desktop typecheck
  and unit tests, and the browser editor smoke test
- iOS CI, on PRs that touch `ios/`: the mobile UI build, then
  an unsigned iOS Simulator build with the unit and WebKit tests on a macOS
  runner. It never signs or uploads.
- The small policy and service checks (artifact policy, desktop source
  update checks, license server, registry, uptime monitor, Android Play)
- In-app update reach, on PRs that touch the Docker image, `docker/`, the
  desktop main process or the update archive: fails when a change cannot reach
  installed apps through the in-app update (`scripts/ci/update_reach.py`). The
  failure says what to do. The usual fix for a changed `Dockerfile`,
  `entrypoint.sh` or `docker/install-claude-cli.sh` is
  `python docker/update_contract.py --pin`. A release that really needs a new
  Docker image fails until the owner adds the label `update-reach:docker-image`
  (each failing group has its own `update-reach:GROUP` label) and re-runs the
  job; it reads the labels when it runs. Label events do not start it.
- Container PostgreSQL (`container-postgres.yml`), on PRs that touch
  `Dockerfile`, `docker/**`, `entrypoint.sh`,
  `scripts/verify_container_postgres.py`,
  `tests/integration/container_ui_smoke.mjs` or the setup wizard
  (`frontend/src/routes/setup/**`, `frontend/src/lib/components/setup/**`):
  builds the image and checks a real container, the buyer Compose install and
  the setup wizard in a browser. It is a full image build, so other PRs never
  start it. Like every PR check, edge publication waits for it when it ran.

Nothing runs on a schedule.

## Only with the `ci:full` label or a manual run

These jobs are skipped on an unlabelled PR, and the PR's merge-readiness
check still passes:

- Android CI: emulator verification on API 34 and 36
- Desktop CI: Windows shell packaging and the Windows preview install
- CI Browser Workspace: the real browser workspace test

Add `ci:full` to a PR that changes that platform in a way the fast checks
cannot see. While the label stays on, every push reruns these jobs and they
must pass. Removing the label cancels them. Other label additions/removals do
not start runner jobs or interrupt real validation. GitHub still records these
label events as all-skipped workflow runs with distinct check names. Edge
publication ignores them only after verifying the workflow marker and all
skipped job results; genuine failed, canceled or pending checks still block it.

Android PRs confined to `android/hub/`, `android/site/`, `android/docs/` or
`docs/` skip APK assembly and emulators while retaining hub, website and download
bridge tests. Native, mobile, shared frontend and unknown paths still build the
APK. Renames check both old and new paths; missing or stale file evidence fails
readiness. Manual and release runs always build and verify the APK.

## Full suites on main, and edge publication

A PR runs only part of the backend and JavaScript tests, so the full suites run
on `main`, and only when the owner starts them:

```bash
gh workflow run ci-backend.yml --ref main
gh workflow run ci-frontend.yml --ref main
```

One run covers every PR merged since the last one. Edge publication waits for
both to pass on the exact `main` revision, started by the owner
(`MAIN_SUITES` in `scripts/ci/edge_publication_gate.py`); a PR's own green run
never stands in for them. Every other check still counts from the merged PR
when the PR tested the same tree. When the PR merged while its branch was
behind `main`, the tree on `main` is one no PR CI tested, and every applicable
workflow has to run on `main`.

While it waits, the gate job stays green but adds an "Edge publication
refused" warning to the "Publish desktop source update" run that lists the
exact commands to run. DeployStatus repeats them in its "publish edge first"
next step. The backend run on `main` also records the test map that later PRs
select from, so a run after a batch of merges keeps PR selection current.

## Manual only

Run these with `gh workflow run <file> --ref <branch>`:

- `container-postgres.yml` on a PR it does not start by itself, for example
  one that changes only the Compose files or `scripts/ci/verify_buyer_compose.py`.
  Every release runs the same container verification in `docker-publish.yml`.
- `ci-instance-identity.yml` runs the identity tests on native Windows and
  macOS. Run it on PRs that change instance identity in `app/core/config.py`.
  Linux coverage is in CI Backend.

## Before a stable release

Run the platform suites once on `main` for the release commit:

```bash
gh workflow run android.yml --ref main
gh workflow run desktop.yml --ref main
gh workflow run ci-browser-workspace.yml --ref main
gh workflow run ci-instance-identity.yml --ref main
```

The release workflow itself verifies the container image, the buyer install
and the Windows installer (see [release-channels.md](release-channels.md)).

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
