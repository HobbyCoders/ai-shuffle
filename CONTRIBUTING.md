# Contributing to AI Shuffle

Outside contributions are welcome. For a bug, use [Report a problem](SUPPORT.md) in the app, then include the support report ID in a GitHub issue. For a larger change, open an issue first so maintainers can discuss the scope. Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Before you change code

Read [AGENTS.md](AGENTS.md) for repository rules. Work from current `main` on a separate branch in your fork. Keep a pull request focused on one change, follow existing conventions, and add a test that catches the problem. Keep `docs/content` and `frontend/src/lib/docs/content` in sync when you change behavior they describe.

The team’s [internal contribution guide](docs/internal/CONTRIBUTING.md) and [development guide](docs/internal/DEVELOPMENT.md) cover setup and repository internals. They remain team references; this page describes the outside contribution process.

## Sign your commits

Contributions use the [Developer Certificate of Origin 1.1](https://developercertificate.org/), not a CLA. By signing off, you certify that you have the right to submit the contribution under the project’s license. Add a sign-off to every commit with `git commit -s`; Git adds a `Signed-off-by: Your Name <your-email>` line from your configured identity. This is a DCO sign-off, not a cryptographic signature.

## Run checks locally

CI runs no tests on pull requests or branch pushes. A push should contain work that has already passed the local checks.

Once per clone, run `python scripts/local_ci.py install` to install the pre-push hook. It selects a Python with pytest and PyYAML; pass `--python PATH` if you need to choose one. Worktrees share that hook.

While working, run only the tests covering your change:

```sh
PYTHONPATH=fluid pytest tests/<path> -m "not slow and not integration" -q
```

In Windows PowerShell, set `$env:PYTHONPATH="fluid"` first, then run the same `pytest` command. For Fluid’s own tests, use `python -m pytest fluid/tests/unit/<files> -q`. Do not run the whole `tests/` tree by hand, or use more than two pytest workers.

For frontend work, run `npm ci` and `npx svelte-kit sync` in `frontend/` once per checkout, then run the targeted tests or type check described in [AGENTS.md](AGENTS.md). Do not copy dependencies or credential files from another checkout.

Before a push, commit your changes and run:

```sh
python scripts/local_ci.py
```

It selects the checks your changes reach and records a pass for the committed tree. Read its “Not covered here” lines and repeat them in your pull request. If a check or the pre-push hook fails, fix the failure locally; never push with `--no-verify` or use GitHub Actions to find out whether tests pass. If your machine cannot run a required check, explain the limitation to a maintainer before pushing.

## Open a draft pull request

Open pull requests as drafts against `main`. Describe the problem, the change, the issue it addresses, and the exact local commands and results. Say what you exercised in the running app and what you tested only with fixtures. Include screenshots for a visible change, and call out checks you could not run. Do not include credentials, private logs or personal data.

Maintainers review the change and decide when it is ready. See [LICENSE](LICENSE) for AGPL-3.0 terms and [TRADEMARKS.md](TRADEMARKS.md) before distributing a modified build.
