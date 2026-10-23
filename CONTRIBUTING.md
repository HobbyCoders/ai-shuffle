# Contributing to AI Shuffle

Outside contributions are welcome. For a bug, use [Report a problem](SUPPORT.md) in the app, then include the support report ID in a GitHub issue. For a larger change, open an issue first so maintainers can discuss the scope. Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Before you change code

Work from current `main` on a separate branch in your fork. Keep a pull request focused on one change, follow existing conventions, and add a test that catches the problem. Keep `docs/content` and `frontend/src/lib/docs/content` in sync when you change behavior they describe.

The team’s [internal contribution guide](docs/internal/CONTRIBUTING.md) and [development guide](docs/internal/DEVELOPMENT.md) cover setup and repository internals. They remain team references; this page describes the outside contribution process.

## Sign your commits

Contributions use the [Developer Certificate of Origin 1.1](https://developercertificate.org/), not a CLA. By signing off, you certify that you have the right to submit the contribution under the project’s license. Add a sign-off to every commit with `git commit -s`; Git adds a `Signed-off-by: Your Name <your-email>` line from your configured identity. This is a DCO sign-off, not a cryptographic signature.

## Run checks locally

Tests run on GitHub CI. Every pull request, including drafts and fork contributions, runs the hosted PR checks in parallel: the full Linux suites and native Windows desktop tests. Desktop packaging, preview installers and macOS checks remain manual; see [where tests run](docs/ci/pr-checks.md) for the exact matrix. Merge requires green CI and review.

Once per clone, run `python scripts/local_ci.py install` to install the pre-push hook. The hook is the local push gate: it runs the offline secret scan on every outgoing commit and on the working tree, plus the update-reach check for branch pushes. It runs no test suites. It selects a Python with pytest and PyYAML; pass `--python PATH` if you need to choose one. Worktrees share that hook.

While working, run only the tests covering your change:

```sh
PYTHONPATH=fluid pytest tests/<path> -m "not slow and not integration" -q
```

In Windows PowerShell, set `$env:PYTHONPATH="fluid"` first, then run the same `pytest` command. For Fluid’s own tests, use `python -m pytest fluid/tests/unit/<files> -q`. Do not run the whole `tests/` tree by hand, or use more than two pytest workers.

For frontend work, run `npm ci` and `npx svelte-kit sync` in `frontend/` once per checkout, then run a targeted test (`npx vitest run <file>`) or the type check (`npx svelte-check --tsconfig ./tsconfig.json`) rather than a full build. Do not copy dependencies or credential files from another checkout.

Then commit and push. The hook scans every outgoing commit for credentials, with no cache, and refuses the push if it finds one. Never bypass it with `--no-verify`. Fix any failures reported by PR CI before review and merge.

`fast` (affected tests), `check`, `full` and `full --step --attest` remain available for optional local validation; the push gate does not run or require them. Edge/nightly publication separately needs a hosted main-suite pass or the owner's attested local full run on the exact main revision. See [where tests run](docs/ci/pr-checks.md).

## Open a draft pull request

Open pull requests as drafts against `main`. Describe the problem, the change, the issue it addresses, and the exact local commands and results. Say what you exercised in the running app and what you tested only with fixtures. Include screenshots for a visible change, and call out checks you could not run. Do not include credentials, private logs or personal data.

Maintainers review the change and decide when it is ready. See [LICENSE](LICENSE) for AGPL-3.0 terms and [TRADEMARKS.md](TRADEMARKS.md) before distributing a modified build.
