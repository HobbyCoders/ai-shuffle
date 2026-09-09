# Contributing to AI Shuffle

Thank you for your interest in contributing to AI Shuffle! This guide will help you get started.

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

### Fork and Clone

1. Fork the repository on GitHub
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ai-shuffle.git
   cd ai-shuffle
   ```
3. Add upstream remote:
   ```bash
   git remote add upstream https://github.com/HobbyCoders/ai-shuffle.git
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
git pull upstream main
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

### Before Committing

Run all checks locally:

```bash
# Python linting
ruff check app/ run_local.py

# Python tests (see Testing below — the suite needs fluid importable)
PYTHONPATH=fluid pytest tests/ -v

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

### 1. Ensure CI Passes

Checks run only for the packages changed by a PR, using the existing backend and JavaScript workflows:

- Backend: the isolated API/core/DB/middleware suite, packaging/backup regressions, and pip-audit for public Python dependencies (local editable Fluid excluded).
- Frontend: type check, tests, build, contract consistency, and npm dependency audit.
- Tools: a clean TypeScript build and npm dependency audit.
- Browser MCP: TypeScript build, unit tests, optional isolated browser fixtures, and npm dependency audit.

Local Ruff and pre-commit checks remain part of contributor validation. Coverage reporting is opt-in; CI does not enforce a coverage percentage.

### 2. PR Description

Include in your PR description:
- **What**: Clear description of the change
- **Why**: Motivation or issue being fixed
- **How**: Brief explanation of your approach
- **Testing**: How you tested the changes

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
# Run all tests (POSIX)
PYTHONPATH=fluid pytest tests/ -v

# Run with coverage
PYTHONPATH=fluid pytest tests/ --cov=app --cov-report=term-missing

# Run specific test file
PYTHONPATH=fluid pytest tests/test_auth.py -v

# Run specific test
PYTHONPATH=fluid pytest tests/test_auth.py::test_login -v
```

```powershell
# Windows / PowerShell
$env:PYTHONPATH = "fluid"; python -m pytest tests/ -v
```

### fluid Tests

The harness has its own suite under `fluid/tests`. Install it with the dev
extras and run the unit tests from the repository root:

```bash
pip install -e './fluid[dev]'
python -m pytest fluid/tests/unit -q
```

Run it on Linux as well as Windows before opening a PR that touches `fluid/`;
CI (the fluid step of `ci-backend.yml`) runs it on Linux.

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
│   ├── hub/               # Cloudflare Worker + D1 account hub
│   └── ci/                # Emulator and instrumentation helpers used by android.yml
├── tools/                 # AI tools (TypeScript)
├── tests/                 # Python tests for the backend
├── plugins/               # Plugin system
└── docs/                  # Documentation
```

---

## Questions?

- Open an issue for bugs or feature requests
- Start a discussion for questions or ideas
- Check existing issues before creating new ones

Thank you for contributing!
