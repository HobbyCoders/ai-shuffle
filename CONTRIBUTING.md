# Contributing to AI Shuffle

Outside contributions are welcome. For a bug, use [Report a problem](SUPPORT.md) in the app, then include the support report ID in a GitHub issue. For a larger change, open an issue first so maintainers can discuss the scope. Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Before you change code

Work from current `main` on a separate branch in your fork. Keep a pull request focused on one change, follow existing conventions, and add a test that catches the problem. Keep `docs/content` and `frontend/src/lib/docs/content` in sync when you change behavior they describe.

The team’s [internal contribution guide](docs/internal/CONTRIBUTING.md) and [development guide](docs/internal/DEVELOPMENT.md) cover setup and repository internals. They remain team references; this page describes the outside contribution process.

## Sign your commits

Contributions use the [Developer Certificate of Origin 1.1](https://developercertificate.org/), not a CLA. By signing off, you certify that you have the right to submit the contribution under the project’s license. Add a sign-off to every commit with `git commit -s`; Git adds a `Signed-off-by: Your Name <your-email>` line from your configured identity. This is a DCO sign-off, not a cryptographic signature.

## Run checks locally

Before every push, run only the affected tests plus the offline secret scan. Every pull request, including drafts and fork contributions, runs the full hosted suites in parallel. Merge requires green CI and review.

Once per clone, run `python scripts/local_ci.py install` to install the pre-push hook. It selects a Python with pytest and PyYAML; pass `--python PATH` if you need to choose one. Worktrees share that hook.

While working, run only the tests covering your change:

```sh
PYTHONPATH=fluid pytest tests/<path> -m "not slow and not integration" -q
```

In Windows PowerShell, set `$env:PYTHONPATH="fluid"` first, then run the same `pytest` command. For Fluid’s own tests, use `python -m pytest fluid/tests/unit/<files> -q`. Do not run the whole `tests/` tree by hand, or use more than two pytest workers.

For frontend work, run `npm ci` and `npx svelte-kit sync` in `frontend/` once per checkout, then run a targeted test (`npx vitest run <file>`) or the type check (`npx svelte-check --tsconfig ./tsconfig.json`) rather than a full build. Do not copy dependencies or credential files from another checkout.

Before a push, commit your changes and run:

```sh
python scripts/local_ci.py fast
```

It maps changes against the remote main merge-base to affected tests, falling back to the whole area suite when a mapping is unknown. Docs-only changes run docs checks. It records a pass for a clean committed tree; the hook always scans outgoing commits, even with a cached pass. Never bypass it with `--no-verify`. Read its “Not covered here” lines and repeat them in your pull request. Fix local failures before pushing, then fix any full-suite failures reported by CI before review and merge.

`check`, `full` and `full --step --attest` remain available for broader local validation. Before edge/nightly publication, maintainers still run the attested full suite locally on main; PR CI does not replace that publication gate. See [where tests run](docs/ci/pr-checks.md).

## Open a draft pull request

Open pull requests as drafts against `main`. Describe the problem, the change, the issue it addresses, and the exact local commands and results. Say what you exercised in the running app and what you tested only with fixtures. Include screenshots for a visible change, and call out checks you could not run. Do not include credentials, private logs or personal data.

Maintainers review the change and decide when it is ready. See [LICENSE](LICENSE) for AGPL-3.0 terms and [TRADEMARKS.md](TRADEMARKS.md) before distributing a modified build.
