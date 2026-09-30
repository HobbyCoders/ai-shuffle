# What CI runs, and when

CI runs on paid Blacksmith runners, so pull requests get only the fast checks.
The slow platform suites run when someone asks for them, and before a release.

## On every pull request (path-filtered)

- CI Backend: the backend test subset
- CI JavaScript: the frontend, mobile, tools and browser-mcp packages that changed
- CI fluid: the fluid unit suite
- Cheap jobs inside Android CI, Desktop CI and CI Browser Workspace: the
  Android build and unit tests, the account hub tests, the desktop typecheck
  and unit tests, and the browser editor smoke test
- The small policy and service checks (artifact policy, desktop source
  update checks, license server, registry, Android Play)

Nothing runs on a schedule.

## Only with the `ci:full` label or a manual run

These jobs are skipped on an unlabelled PR, and the PR's merge-readiness
check still passes:

- Android CI: emulator verification on API 34 and 36
- Desktop CI: Windows shell packaging and the Windows preview install
- CI Browser Workspace: the real browser workspace test

Add `ci:full` to a PR that changes that platform in a way the fast checks
cannot see. While the label stays on, every push reruns these jobs and they
must pass. Removing the label cancels them.

## Manual only

Run these with `gh workflow run <file> --ref <branch>`:

- `container-postgres.yml` builds the image and checks a real container,
  the buyer Compose install and the UI. Run it on PRs that change the
  Dockerfile, the entrypoint or the Compose files. Every release runs the
  same container verification in `docker-publish.yml`.
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
