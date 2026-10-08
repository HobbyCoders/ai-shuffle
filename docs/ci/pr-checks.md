# What CI runs, and when

Most test suites run on paid Blacksmith runners, so pull requests get only the
fast checks. The browser editor smoke, license server and registry checks use
standard GitHub `ubuntu-24.04` runners; their paths, tests and manual runs are
unchanged. These consume GitHub Actions allowance rather than Blacksmith
minutes (runner overages follow the platform billing policy). The slow platform suites run
when someone asks for them, and before a release.

## On every pull request (path-filtered)

- CI Backend: the backend test subset
- CI JavaScript: the frontend, mobile, tools and browser-mcp packages that changed
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
  update checks, license server, registry, Android Play)
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

## Edge publication of a combined main tree

When a PR merges while its branch is behind `main` (another PR merged during
its CI), the tree on `main` is one no PR CI tested. Edge publication then waits for the owner to run CI on
`main`. The gate job stays green but adds an "Edge publication refused"
warning to the "Publish desktop source update" run that lists the exact
commands, for example `gh workflow run ci-backend.yml --ref main`. DeployStatus
repeats them in its "publish edge first" next step.

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
