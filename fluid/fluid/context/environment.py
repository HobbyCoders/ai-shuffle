"""Environment-context gathering for the prompt compiler.

Claude Code injects an environment block (cwd, platform, OS, date, git state)
into its system prompt; fluid gathers the same facts itself so every consumer
gets that context without assembling it app-side. All gathering is best-effort:
a missing git binary, a non-repo cwd, or a slow subprocess degrades to omitting
that line, never to an exception.
"""

from __future__ import annotations

import os
import platform as _platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from fluid.tools.shell import describe as describe_shell

_GIT_TIMEOUT = 3.0  # seconds; env gathering must never stall a session start


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _os_version() -> str:
    try:
        if sys.platform == "darwin":
            return f"macOS {_platform.mac_ver()[0]}"
        return f"{_platform.system()} {_platform.release()}"
    except Exception:
        return _platform.system() or "unknown"


def _parse_porcelain2(porcelain: str) -> tuple[dict[str, str], dict[str, int]]:
    """``git status --porcelain=2 --branch`` → (branch facts, change counts)."""
    branch: dict[str, str] = {}
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicts": 0}
    for line in porcelain.splitlines():
        if line.startswith("# branch.head"):
            branch["head"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.upstream"):
            branch["upstream"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.ab"):
            parts = line.split()
            if len(parts) >= 4:
                branch["ahead"] = parts[2].lstrip("+")
                branch["behind"] = parts[3].lstrip("-")
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(maxsplit=2)[1]
            if len(xy) == 2:
                if xy[0] != ".":
                    counts["staged"] += 1
                if xy[1] != ".":
                    counts["modified"] += 1
        elif line.startswith("u "):
            counts["conflicts"] += 1
        elif line.startswith("? "):
            counts["untracked"] += 1
    return branch, counts


def git_context(cwd: str) -> str | None:
    """Branch (with upstream/ahead-behind), change counts, recent commits.

    Returns ``None`` when ``cwd`` is not inside a git work tree (or git is
    unavailable), so callers can omit the section entirely.
    """
    inside = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return None
    lines: list[str] = []
    porcelain = _run_git(cwd, "status", "--porcelain=2", "--branch")
    branch, counts = _parse_porcelain2(porcelain or "")
    head = branch.get("head", "")
    if head and head != "(detached)":
        line = f"Current branch: {head}"
        if branch.get("upstream"):
            line += f" → {branch['upstream']}"
            ahead, behind = branch.get("ahead", "0"), branch.get("behind", "0")
            if ahead != "0" or behind != "0":
                line += f" (ahead {ahead}, behind {behind})"
        lines.append(line)
    elif head == "(detached)":
        lines.append("Current branch: (detached HEAD)")
    else:
        fallback = _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if fallback:
            lines.append(f"Current branch: {fallback}")
    if porcelain is not None:
        changed = [
            f"{n} {label}"
            for label, n in (
                ("staged", counts["staged"]),
                ("modified", counts["modified"]),
                ("untracked", counts["untracked"]),
                ("conflicted", counts["conflicts"]),
            )
            if n
        ]
        lines.append(
            "Working tree: clean" if not changed else "Working tree: " + ", ".join(changed)
        )
    recent = _run_git(cwd, "log", "--oneline", "-5")
    if recent:
        lines.append("Recent commits:\n" + recent)
    if not lines:
        return None
    return "\n".join(lines)


# ─── project facts: manifests, package managers, verify commands ─────────────

# Manifest filenames that mark a project root and are worth naming.
_MANIFESTS = (
    "pyproject.toml", "setup.py", "requirements.txt",
    "package.json", "tsconfig.json", "deno.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml",
    "CMakeLists.txt", "Makefile", "Dockerfile",
)

# Lockfile → package manager, in priority order per ecosystem.
_PY_LOCKFILES = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile.lock", "pipenv"))
_JS_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
    ("yarn.lock", "yarn"), ("package-lock.json", "npm"),
)

# package.json scripts / Makefile targets worth surfacing as verify commands.
_VERIFY_TARGETS = ("test", "tests", "lint", "typecheck", "check", "build", "fmt", "format")
_MAX_VERIFY_COMMANDS = 8
_MAX_FACT_FILE_BYTES = 256 * 1024


def _read_small(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FACT_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def project_facts(cwd: str) -> str | None:
    """Manifest, package-manager, and verify-command lines for the prompt.

    Hands the model its verification loop up front — which manifests exist,
    which package manager the lockfiles imply, and the exact test/lint/build
    entry points — instead of making it rediscover them every session. A few
    stats plus two small file reads; never raises, ``None`` when nothing was
    detected.
    """
    import json as _json

    root = Path(cwd)
    manifests = [m for m in _MANIFESTS if (root / m).is_file()]
    managers = list(dict.fromkeys(
        pm for lock, pm in (*_PY_LOCKFILES, *_JS_LOCKFILES) if (root / lock).is_file()
    ))

    verify: list[str] = []
    if (root / "scripts" / "run_tests.sh").is_file():
        verify.append("scripts/run_tests.sh")
    if (root / "package.json").is_file():
        try:
            scripts = _json.loads(_read_small(root / "package.json") or "{}").get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        js_pm = next((pm for lock, pm in _JS_LOCKFILES if (root / lock).is_file()), "npm")
        verify.extend(f"{js_pm} run {name}" for name in _VERIFY_TARGETS if name in scripts)
    if (root / "pytest.ini").is_file() or "[tool.pytest" in _read_small(root / "pyproject.toml"):
        verify.append("uv run pytest" if (root / "uv.lock").is_file() else "pytest")
    makefile = _read_small(root / "Makefile")
    if makefile:
        import re as _re

        verify.extend(
            f"make {name}" for name in _VERIFY_TARGETS
            if _re.search(rf"^{_re.escape(name)}\s*:", makefile, _re.MULTILINE)
        )

    lines: list[str] = []
    if manifests:
        line = f"Project manifests: {', '.join(manifests[:6])}"
        if managers:
            line += f" ({'/'.join(managers)})"
        lines.append(line)
    if verify:
        deduped = list(dict.fromkeys(verify))[:_MAX_VERIFY_COMMANDS]
        lines.append(f"Verify commands: {'; '.join(deduped)}")
    return "\n".join(lines) if lines else None


def environment_block(cwd: str | os.PathLike[str] = ".") -> str:
    """The compiled prompt's environment section (always renders).

    Facts that cannot be gathered are omitted line-by-line; the block itself is
    always non-empty (cwd + date at minimum).
    """
    cwd_str = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
    shell = describe_shell()
    lines = [
        "# Environment",
        f"Working directory: {cwd_str}",
        f"Platform: {sys.platform}",
        f"OS: {_os_version()}",
        f"Shell: {shell}",
        f"Today's date: {datetime.now(UTC).strftime('%Y-%m-%d')}",
    ]
    git = git_context(cwd_str)
    if git is not None:
        lines.append("Git repository: yes")
        lines.append(git)
    else:
        lines.append("Git repository: no")
    facts = project_facts(cwd_str)
    if facts:
        lines.append(facts)
    if git is not None or facts:
        lines.append(
            "(git and project lines are a snapshot from session start — "
            "re-check before relying on them)"
        )
    return "\n".join(lines)
