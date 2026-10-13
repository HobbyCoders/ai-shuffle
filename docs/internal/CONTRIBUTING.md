> **Internal: HobbyCoders team only.** AI Shuffle is proprietary software. This
> guide is for HobbyCoders team members with access to the private repository;
> it does not invite outside contributions.

# Contributing to AI Shuffle

This guide covers setup, conventions and the review process for the AI Shuffle
repository.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Process](#pull-request-process)
- [Code Style](#code-style)
- [Testing](#testing)
- [Commit Messages](#commit-messages)

---

## Code of Conduct

Please be respectful and constructive in all interactions. We're building this together.

---

## Getting Started

### Prerequisites

- **Python 3.12+**
- **Node.js 22.12+**
- **Git**
- **GitHub CLI** — [Download](https://cli.github.com/)

### Clone

Clone the repository directly; team members push branches to it rather than
to a fork:

```bash
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle
```

---

## Development Setup

### Backend (Python)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt ./fluid
pip install -r requirements-dev.txt
# Optional: computer-use (host desktop control), native runs only
pip install -r requirements-native.txt

# Install pre-commit hooks
pip install pre-commit
pre-commit install
```

### Frontend (SvelteKit)

```bash
cd frontend
npm install
```

### Tools (TypeScript)

```bash
cd tools
npm install
npm run build
```

### Environment Configuration

```bash
cp .env.example .env
# Edit .env and set: LOCAL_MODE=true
```

### Running the Application

```bash
# From project root
python run_local.py
```

Open `http://localhost:8000` in your browser.

---

## Making Changes

### Branch Naming

Create a branch from `main` with a descriptive name:

```bash
git checkout main
git pull origin main
git checkout -b type/description
```

**Branch types:**
- `feat/` — New features
- `fix/` — Bug fixes
- `docs/` — Documentation changes
- `refactor/` — Code refactoring
- `test/` — Adding or updating tests
- `chore/` — Maintenance tasks

**Examples:**
- `feat/add-session-export`
- `fix/websocket-reconnect`
- `docs/update-readme`

### Harness scratch and state

Use the absolute per-session scratch directory designated in the agent's turn
context (`~/.fluid/projects/<project-slug>/scratch/<session>/`, or the configured
Fluid projects root), not `<checkout>/.fluid/`. Ledger, notes and Task reports
also live under that global project root. Project `.fluid/` is a load scope
(skills, agents, commands, plugins and settings) and the host's worktree area;
explicit skill/plugin authoring remains supported. Do not delete legacy owner
strays as part of a fix: old ledger/notes are copied only when canonical state is
absent, and old scratch/reports are not migrated or cleaned automatically.

### Before Committing

Run all checks locally:

```bash
# Python linting
ruff check app/ run_local.py

# Python tests for the files you changed (see Testing below; the suite
# needs fluid importable). scripts/local_ci.py runs the rest before a push.
PYTHONPATH=fluid pytest tests/core/test_example.py -q

# Frontend type check and build
cd frontend
npm run check
npm run test
npm run build
cd ..

# Tools build
cd tools
npm run build
npx tsc --noEmit
cd ..
```

Pre-commit hooks will also run automatically on `git commit`.

---

## Pull Request Process

### 1. Pass the local checks

No CI runs on pull requests. Public-app release builds and owner-dispatched
checks use standard GitHub-hosted Linux, Windows and macOS runners, free in
public repositories; this does not re-enable PR or branch-push CI. Docker
builds use the GitHub Actions layer cache rather than a paid sticky disk.
See [where tests run](../ci/pr-checks.md) for the runner and publication policy.
The checks run on your machine before a push:

```bash
python scripts/local_ci.py install   # once per clone: turns on the pre-push hook
python scripts/local_ci.py           # the suites your branch's changes reach
```

It covers what the PR workflows used to: the backend tests the change can
reach, fluid, the frontend, mobile, tools and browser-mcp packages (type
checks, unit tests, builds and the contract check), docs, desktop, the
Android app and download bridge, the workflow policy tests and the in-app update
reach check. A pass on a clean checkout is recorded, and the pre-push hook
reuses that test pass, but always scans outgoing commits for leaked secrets. Do not bypass the
hook with `--no-verify`.

For PR screenshots, commit images to `pr-assets` (or `pr-assets/*`) and push
normally through the hook, from a code checkout containing the hook's scripts
(for example, `git push origin pr-assets:pr-assets`). The hook accepts the range
without a local test-pass record only when every touched path is an image
(lowercase `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) or the root `README.md`,
after the secret scan passes. Only regular files qualify, not symlinks or
submodules; `README.md` cannot be a directory. For a new branch it checks the
whole history and tip tree; for an existing branch it checks every outgoing
commit and the remote-to-local tip difference, including rewinds and files
later deleted or renamed. Other files still require the usual local checks. GitHub Contents
API uploads are not allowed: they bypass the hook and its secret scan.

The run ends by listing what this machine could not cover (Windows when not
on a Windows worker, Mac-only checks, the container image). Mention those in
the PR description. See [docs/ci/pr-checks.md](../ci/pr-checks.md) for edge
publication and the checks a stable release waits for.

Ruff and pre-commit stay part of contributor validation. Coverage reporting is
opt-in; nothing enforces a coverage percentage. Every explicit local `check`/`full` runs
an offline whole-tree secret scan and networked `pip-audit`/`npm audit` reports.
Dependency findings warn until triaged; `--strict-audit` makes Python advisories,
npm high/critical findings and unavailable audits fail. The audit installs pinned
pip-audit into ignored `venv/local-audit/`, never the global Python, and does not
upgrade dependencies. The hook scans every outgoing commit even with a cached
pass, so adding then deleting a token does not hide it. Automatic pre-push checks
never run the network dependency audit, even on an uncached tree; run an explicit
`check` to refresh it. Secret scans have a 120-second deadline (15 seconds per Git
subprocess) and fail closed on timeout. The installed hook also refuses branches
missing the required security-gate version rather than delegating to old code;
update the branch, and rerun `install` when upgrading an older installed hook.

For a false positive, use a reasoned, narrowly anchored path **and** fake-value
allowlist in `.gitleaks.toml` (`condition = "AND"`, `regexTarget = "match"`), never
a directory-wide exclusion. Add a regression test and run
`python scripts/secret_scan.py`. Revoke real leaked keys and remove them from all
outgoing commits instead of allowlisting them. See the
[local security checks](../ci/pr-checks.md#local-security-checks) for the exact
coverage, offline fallback, first-use setup and allowlist format.

### 2. PR Description

Include in your PR description:
- **What**: Clear description of the change
- **Why**: Motivation or issue being fixed
- **How**: Brief explanation of your approach
- **Testing**: How you tested the changes

A PR whose description says it was "not tried on real workers" also adds a
row to `scripts/live_smoke/scenarios.json` that would catch the bug on
the real workers, so the pre-release live smoke covers it (see
[live-smoke.md](live-smoke.md)).

### 3. PR Template

```markdown
## Summary
Brief description of changes.

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactoring
- [ ] Other (describe)

## Testing
- [ ] I ran the test suite locally
- [ ] I added tests for new functionality
- [ ] I tested manually in the browser

## Checklist
- [ ] My code follows the project's style guidelines
- [ ] I have updated documentation if needed
- [ ] My changes don't introduce new warnings
```

### 4. Review Process

- A maintainer will review your PR
- Address any feedback by pushing additional commits
- Once approved, a maintainer will merge your PR

---

## Code Style

### Python

We use **Ruff** for linting and formatting:

```bash
# Check for issues
ruff check app/ run_local.py

# Auto-fix issues
ruff check --fix app/ main.py run_local.py

# Format code
ruff format app/ main.py run_local.py
```

**Key conventions:**
- Follow PEP 8
- Use type hints for function parameters and return values
- Docstrings for public functions and classes
- Max line length: 100 characters

### Frontend (Svelte/TypeScript)

**Key conventions:**
- Use TypeScript for all new code
- Follow Svelte 5 patterns (runes, snippets)
- Use Tailwind CSS for styling
- Keep components focused and reusable

### General

- Use meaningful variable and function names
- Keep functions small and focused
- Comment complex logic, but prefer self-documenting code
- Remove console.log statements before committing

---

## Testing

### Python Tests

The suite imports the fluid harness (`fluid/`), which is not on
`sys.path` by default — without it collection fails before a single test runs.
Either install it once (`pip install -e ./fluid`) or point `PYTHONPATH`
at it for the run:

```bash
# Run specific test file (POSIX)
PYTHONPATH=fluid pytest tests/test_auth.py -v

# Run specific test
PYTHONPATH=fluid pytest tests/test_auth.py::test_login -v

# Run a file with coverage
PYTHONPATH=fluid pytest tests/test_auth.py --cov=app --cov-report=term-missing

# The whole tree: python scripts/local_ci.py full does this, at low
# priority. On a shared machine run it only when a release needs it.
python scripts/local_ci.py full
```

```powershell
# Windows / PowerShell
$env:PYTHONPATH = "fluid"; python -m pytest tests/test_auth.py -v
```

#### Running the tests

The full backend suite is about 21,500 tests and takes over 15 minutes. While
working, run only the test files that cover your change; `local_ci.py` picks
the rest before a push:

```bash
PYTHONPATH=fluid pytest tests/core/test_pg_shim.py -m "not slow and not integration" -q
```

Use `-m "not slow and not integration"` for local runs, and keep `pytest -n`
at 2 workers or fewer. Only one `pytest -n` run happens at a time on a
machine: `tests/conftest.py` makes the xdist controller wait for a lock file
in the system temp directory (`ai-shuffle-pytest-xdist.lock`) and prints a
notice every 15 seconds while another run holds it. It gives up after 20
minutes and runs anyway. The gate is off in GitHub Actions and for runs
without `-n`; set `AI_SHUFFLE_PYTEST_GATE=0` to bypass it.

### fluid Tests

The harness has its own suite under `fluid/tests`. Install it with the dev
extras and run the unit tests from the repository root:

```bash
pip install -e './fluid[dev]'
python -m pytest fluid/tests/unit -q
```

`local_ci.py` runs it when `fluid/` changes. Run it on Linux as well as
Windows when the change is platform-specific; `ci-fluid.yml` is a manual
Linux run for when no Linux worker is available.

### Android Tests

```bash
cd android
./gradlew testDebugUnitTest lintDebug   # Windows: gradlew.bat
cd hub && npm ci && npm test            # account hub Worker
```

**Test file structure:**
```
tests/
├── conftest.py          # Shared fixtures
├── test_auth.py         # Authentication tests
├── test_api_*.py        # API endpoint tests
└── ...
```

### Frontend Tests

```bash
cd frontend

# Run tests
npm run test

# Run in watch mode
npm run test:watch

# Run with coverage
npm run test:coverage
```

#### Browser regression for the Git panel

`frontend/tests/git-panel.html` mounts the real Git header panel with stub git,
operation and GitHub stores; `frontend/tests/verify-git-panel.mjs` drives it
with Playwright at 375, 768 and 1280px wide. It clicks every inline editor
open and closed, checks Escape, outside clicks, the busy lock, operation
feedback (including an operation started from outside the panel), which
handler arguments a row click and an editor submit pass, touch target sizes
and overflow, then repeats the in-flow checks in
the mobile shell (`?shell=mobile`). Run it after changing anything under
`frontend/src/lib/components/git/`: jsdom does not flush Svelte between click
listeners the way a browser does, so some panel bugs only show up here. It
needs `browser-mcp`'s `playwright-core` (`npm ci --ignore-scripts --prefix browser-mcp`)
and Chrome or Chromium (`CHROMIUM_PATH`).

```sh
cd frontend
npx vite dev --config tests/vite.git-panel.config.js --host 127.0.0.1 --port 5193 --strictPort
node tests/verify-git-panel.mjs   # in a second terminal
```

#### Browser check for the private-access approval card

`frontend/tests/chat-access.html` mounts the real `ChatAccess` button and
dialog; `frontend/tests/verify-chat-access.mjs` answers the session and
private-access API in the browser and checks the one-shot command card for a
writable folder, a read-only folder and an SSH machine at 1280x800 and 390x844
(touch): title, effect line, verbatim command, fit, and the approve request.
Same requirements as the Git panel check.

```sh
cd frontend
npx vite dev --config tests/vite.chat-access.config.js --host 127.0.0.1 --port 6917 --strictPort
node tests/verify-chat-access.mjs   # in a second terminal
```

#### Browser check for Assistant setup

`frontend/tests/assistant-setup.html` mounts the real `AssistantHome` with
stub app-data, layout and voice modules; `frontend/tests/verify-assistant-setup.mjs`
answers `/api/v1/assistant` from an in-script fake backend and drives the
setup at 1440x900 and 390x844. It checks the defaults (name, dedicated agent,
one preset avatar), the 4x4 avatar grid drawn from the per-avatar SVG files,
that no introduction form, research switch, pause button, usage panel or limit
numbers remain, the save payload, the per-source opt-in and its one status
line, the read-only "About you" details with their correction form and payload,
the Gmail/GitHub suggestion cards and their links, and overflow. Same requirements as the Git panel
check; set `SCREENSHOT_DIR` to keep screenshots.

```sh
cd frontend
npx vite dev --config tests/vite.assistant-setup.config.js --host 127.0.0.1 --port 5741 --strictPort
node tests/verify-assistant-setup.mjs   # in a second terminal

`frontend/tests/one-time-branch.html` mounts the real composer worktree chip
(`ExecutionModeBar` and `CreateBranchModal`) over the real session and
composer-preference stores, with a stub git store and a local transport that
can hold the session-create request. `frontend/tests/verify-one-time-branch.mjs`
checks that a branch choice (made with **New branch...** or picked from the
list) is spent by the session created on it, including a New session opened
while that create is in flight and a failed create. Same requirements as above.

```sh
cd frontend
npx vite dev --config tests/vite.one-time-branch.config.js --host 127.0.0.1 --port 5401 --strictPort
node tests/verify-one-time-branch.mjs   # in a second terminal

`frontend/tests/create-branch-modal.html` mounts the real new-session composer
with a project selected. `frontend/tests/verify-create-branch-modal.mjs` opens
"New branch..." from the worktree chip and the local branch chip at 1440x900
and 390x844 and checks that nothing in the composer paints above the dialog.
Same setup as the Git panel harness:

```sh
cd frontend
npx vite dev --config tests/vite.create-branch-modal.config.js --host 127.0.0.1 --port 5301 --strictPort
node tests/verify-create-branch-modal.mjs   # in a second terminal

#### Browser check for Settings > Plugins

`frontend/tests/settings-plugins.html` mounts the real `SettingsCard` the way
a "Manage plugins" link opens it (`?tab=plugins`). `frontend/tests/verify-settings-plugins.mjs`
answers the plugin API from an in-script fake backend at 1280x800 and 390x844.
It checks that the Plugins section opens with the plugin hub, that an
agent-authored plugin is marked as needing review, that its review lists each
component with the MCP server's command line, that approving sends the reviewed
content hash, and that nothing overflows. Same requirements as the Git panel check.

```sh
cd frontend
npx vite dev --config tests/vite.settings-plugins.config.js --host 127.0.0.1 --port 6401 --strictPort
node tests/verify-settings-plugins.mjs   # in a second terminal
```

#### Browser regression for the turn timeline

`frontend/tests/turn-navigator.html` mounts the real `ChatView` with a seeded
transcript (`?turns=N`) and no network. `frontend/tests/verify-turn-navigator.mjs`
runs 10 and 80 turns at 1440x900 and at 390x844 with touch. It checks that the
same rail renders at every length, that it scrolls by wheel or touch drag
without moving the transcript, that the first, middle and last turns can be
reached by click, tap and Home/End/arrow keys, that the current turn stays in
view, and that the hover preview is not clipped. Run it after changing
`TurnNavigator.svelte` or the turn tracking in `MessageArea.svelte`.

```sh
cd frontend
npx vite dev --config tests/vite.turn-navigator.config.js --host 127.0.0.1 --port 5283 --strictPort
node tests/verify-turn-navigator.mjs   # in a second terminal

`frontend/tests/usage-rows.html` does the same for the usage popover rows and
the Loop guard and Media & tools settings panels, with canned usage and API
answers, at 1440x900 and 390x844. Run it after changing `ContextUsageBar`,
`LoopGuardPanel` or `MediaToolsSection`:

```sh
cd frontend
npx vite dev --config tests/vite.usage-rows.config.js --host 127.0.0.1 --port 5291 --strictPort
node tests/verify-usage-rows.mjs [screenshot-dir]   # in a second terminal

#### Browser check for a call from the new-session composer

`frontend/tests/fresh-call.html` mounts the desktop composer with voice on and
stubs every request; the microphone is always denied, so no call gets past
the attach request. `frontend/tests/verify-fresh-call.mjs` checks at 1280px that the call
button shows on a fresh composer, that a click (even a double click) creates
one session and then attaches the call to it, that a failed create shows its
error and starts no call, and that a runner on another device disables the
button. The mobile shell's version of these checks is in
`mobile/tests/verify-composer-gesture.mjs`.

```sh
cd frontend
npx vite dev --config tests/vite.fresh-call.config.js --port 5932 --strictPort
node tests/verify-fresh-call.mjs   # in a second terminal
```

#### Browser check for a first message on a restored New Chat

`frontend/tests/composer-send-guard.html` mounts the real `AppPage` startup
around the real `ChatView`, in a fresh tab whose saved layout says New Chat
while the server still remembers an older session (#1636). Every request and
the WebSocket are answered by the fixture. `frontend/tests/verify-composer-send-guard.mjs`
runs at 1280x800 and 390x844 and checks that a first message sent while startup
is still loading never goes to the older session, that the session it creates
stays open, and that a refused send (a failed create on a staged or auto-named
branch, no connection, a created session whose first message cannot go out)
keeps the text and shows the reason.

```sh
cd frontend
npx vite dev --config tests/vite.composer-send-guard.config.js --host 127.0.0.1 --port 6101 --strictPort
node tests/verify-composer-send-guard.mjs   # in a second terminal

#### Browser check for private access on a mirrored conversation

`frontend/tests/chat-access-remote.html` mounts the real `ChatAccess` for a
session that runs on another device. `frontend/tests/verify-chat-access-remote.mjs`
answers this worker's session and private-access API in the browser and checks,
at 1440x900 and 390x844, that the owner's request card, saved connection,
one-shot approval and revocation work from the viewer and name the owning
device. The forwarding itself is covered by
`tests/core/network/test_chat_access_routing.py`.

```sh
cd frontend
npx vite dev --config tests/vite.chat-access-remote.config.js --host 127.0.0.1 --port 6600 --strictPort
node tests/verify-chat-access-remote.mjs   # in a second terminal
```

### Writing Good Tests

- Test one thing per test function
- Use descriptive test names: `test_login_fails_with_invalid_credentials`
- Use fixtures for common setup
- Test both success and failure cases
- Mock external dependencies

---

## Commit Messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

[optional body]

[optional footer]
```

### Types

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, no logic change)
- `refactor`: Code refactoring (no feature/fix)
- `test`: Adding or updating tests
- `chore`: Maintenance tasks, dependencies
- `ci`: CI/CD changes

### Examples

```bash
feat(chat): add session export to JSON format

fix(websocket): handle reconnection on network change

docs(readme): update installation instructions

refactor(api): extract authentication into middleware

test(auth): add tests for login rate limiting

chore(deps): update svelte to v5.2.0
```

### Guidelines

- Use imperative mood: "add feature" not "added feature"
- Keep first line under 72 characters
- Reference issues: `fix(auth): resolve token expiry (#123)`

---

## Project Structure

```
ai-shuffle/
├── app/                    # Backend (FastAPI)
│   ├── api/               # API routes
│   ├── core/              # Business logic
│   ├── db/                # Database models
│   └── middleware/        # Request middleware
├── frontend/              # Frontend (SvelteKit)
│   └── src/
│       ├── lib/           # Components, stores, utils
│       └── routes/        # Pages
├── fluid/                 # The fluid agent harness (own pyproject; package is fluid/fluid)
│   └── tests/unit/        # fluid's own unit suite
├── android/               # Android app (Kotlin/Gradle)
│   └── ci/                # Emulator and instrumentation helpers used by android.yml
├── tools/                 # AI tools (TypeScript)
├── tests/                 # Python tests for the backend
├── plugins/               # Plugin system
└── docs/                  # Documentation
```

---

Cloud services (hub, license, website, uptime) and their deploy workflows live
in [HobbyCoders/hobbycoders-cloud](https://github.com/HobbyCoders/hobbycoders-cloud).
They have their own tests and owner-only deployment checklist. App contributors
do not need that private checkout.

## Questions?

- Open an issue for bugs or feature requests
- Start a discussion for questions or ideas
- Check existing issues before creating new ones

Thank you for contributing!
