# Working in this repository

## Python tests

The worker host is shared by every agent session on it. The full backend
suite is about 21,500 tests and takes over 15 minutes. While working, run
only the test files that cover your change; `scripts/local_ci.py` (below)
picks the rest before a push.

- POSIX: `PYTHONPATH=fluid pytest tests/<path> -m "not slow and not integration" -q`
- Windows PowerShell: `$env:PYTHONPATH="fluid"` first, then the same `pytest` command.
- Never run the whole `tests/` tree by hand; `local_ci.py full` does that
  when an edge or stable release needs it.
- Do not use `pytest -n` with more than 2 workers. Only one `pytest -n` run
  happens at a time on a machine: `tests/conftest.py` makes it wait for a
  machine-wide lock and prints a notice while it waits. `AI_SHUFFLE_PYTEST_GATE=0`
  bypasses the gate; do not set it to get ahead of another session.
- fluid has its own suite: `python -m pytest fluid/tests/unit/<files> -q`.
- Run anything expected to take over a minute (a test run, build or install)
  through the Monitor tool, not a foreground Bash call.

## Frontend

- Run `npm ci` and then `npx svelte-kit sync` in `frontend/` once per
  checkout. Never junction or copy `node_modules` between checkouts.
- Prefer `npx svelte-check --tsconfig ./tsconfig.json` or a targeted
  `npx vitest run <file>` over a full build, unless the change needs a build.
- `mobile/` links frontend's `node_modules` with `npm run deps`; install
  `frontend/` first.

## Testing before a push (CI does not test code)

GitHub Actions runs no tests on pull requests or branch pushes. Every check
runs on this worker before the code leaves it. Never push, mark a PR ready,
re-run or dispatch a workflow to find out whether tests pass.

- Once per clone: `python scripts/local_ci.py install` turns on the pre-push
  hook. It copies the hook into the clone's hooks directory, which every
  worktree shares, and leaves `core.hooksPath` and AI Shuffle's own git hooks
  alone. It also finds a Python with pytest and PyYAML (the `python3` on PATH
  may be the app's venv, which has neither) and saves it in git config
  `local-ci.python`; `--python PATH` picks one.
- Before pushing, commit, then run `python scripts/local_ci.py` through the
  Monitor tool. It runs the suites your changes reach at low priority with
  two workers and records the pass, so the hook lets the push through.
  Use the Python that has pytest and PyYAML (`LOCAL_CI_PYTHON` overrides).
- Never use `git push --no-verify`. If the hook fails, fix the failure.
  Never add a test to `scripts/ci/windows_known_failures.txt` to get a push
  through; that list only shrinks (#2215).
- Read the "Not covered here" lines and repeat them in the PR body (Windows
  when not on DesktopWorker, Mac-only checks, the container image).
- Open the PR, then merge it when the local pass is green; no CI will run.
- `python scripts/local_ci.py full --attest` on a clean checkout of
  `origin/main` publishes edge. Run it only when the owner wants an edge
  update or a release.

## Where the real docs are

- `docs/internal/CONTRIBUTING.md`: setup, branches, tests, pull requests.
- `docs/internal/DEVELOPMENT.md`: local development details.
- `docs/ci/pr-checks.md`: where tests run, edge publication and release checks.
