"""Run the project's checks on this machine.

Tests run on GitHub CI: every pull request runs the full hosted suites, and
nightly publication accepts a hosted main-suite pass or an owner-local
attested full run on main. The pre-push hook (.githooks/pre-push, copied into
git's hooks directory by install) runs no test suites. Before anything leaves
this machine it runs the offline secret scan on every outgoing commit and on
the working tree, then the update-reach policy for branch pushes.

    python scripts/local_ci.py install         # turn on the pre-push hook for this clone
    python scripts/local_ci.py fast            # optional: affected tests + offline scan
    python scripts/local_ci.py check           # optional: changed-suite checks + dependency audit
    python scripts/local_ci.py full            # every suite, and record the backend test map
    python scripts/local_ci.py full --attest   # on main: also post local/full-suite for edge publication
    python scripts/local_ci.py full --step --attest   # the same in resumable chunks of at most 40 minutes
    python scripts/local_ci.py list            # what each suite runs and when

Suites that need hardware this machine lacks (macOS, iOS, a Windows install
when run elsewhere) are printed as not covered; the pass says so instead of
claiming them.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "HobbyCoders/ai-shuffle"
# The archive the public history was cut from. --attest walks its history for
# the public-root bridge (scripts/ci/public_cutover.py).
PRIVATE_REPOSITORY = "HobbyCoders/ai-shuffle-private"
PRIVATE_CHECKOUT_ENV = "AI_SHUFFLE_PRIVATE_CHECKOUT"
GITHUB_URL = r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
SECURITY_GATE_VERSION = 2
# Edge publication waits for this commit status on the exact main revision
# (scripts/ci/edge_publication_gate.py).
ATTEST_CONTEXT = "local/full-suite"
# A full pass on Windows also posts this; a stable release waits for it
# (the owner-local ship-release skill). Edge does not, so a Windows worker being off never
# blocks Linux and Docker work.
WINDOWS_CONTEXT = "local/windows-suite"
MAP_PATH = Path(os.environ.get("LOCAL_CI_TEST_MAP")
                or Path.home() / ".cache" / "ai-shuffle" / "backend-test-map.json.gz")
# The shared worker hosts run other sessions: never more than two test workers.
WORKERS = os.environ.get("LOCAL_CI_WORKERS", "2")
# Hosted Windows parts only (scripts/ci/windows_parts.py): a failed pytest
# command reruns just its failed tests once. Off everywhere else.
RETRY_FAILED_ENV = "LOCAL_CI_RETRY_FAILED"
MAX_RETRIED = 20
# Tests that failed and then passed on that rerun, in this process.
FLAKY: list[str] = []

# The CI Backend fast subset; scripts/ci/backend_test_map.py keeps the same list.
FAST_SUBSET = [
    "tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract",
    "tests/performance", "tests/mcp", "tests/services", "tests/unit", "tests/scripts",
]
FAST_SUBSET_EXCLUDED = [
    "tests/core/test_chat_shell.py",
    # Dedicated serial backend-linux step: supervisor globals and idle/disk
    # timing measurements must not share the parallel fast-subset workload.
    "tests/core/test_container_runtime.py",
    "tests/core/test_container_image_contract.py",
]
BACKEND_PATHS = ["app/*", "tests/*", "fluid/*", "scripts/*", "docker/*", "Dockerfile",
                 "requirements*", "pytest.ini", "conftest.py", "run_local.*"]


class Failure(Exception):
    pass


class OutOfTime(Failure):
    """A command was stopped at the staged gate's deadline (see local_ci_stages)."""


# time.monotonic() after which run() stops its command; set only by full --step.
DEADLINE: float | None = None


@dataclass
class Context:
    mode: str                      # "changed" or "full"
    base: str                      # merge base with the resolved comparison ref
    files: list[str] | None        # changed paths; None in full mode
    python: str
    log: Callable[[str], None] = print
    notes: list[str] = field(default_factory=list)
    backend_selected: list[str] | None = None  # full --step: the test files of one shard
    map_hits: Path | None = None

    def changed(self, patterns: Iterable[str]) -> bool:
        if self.files is None:
            return True
        return any(fnmatch(name, pattern) for name in self.files for pattern in patterns)


@dataclass
class Suite:
    name: str
    paths: list[str]
    run: Callable[[Context], None]
    about: str
    # A reason this machine cannot run it, or None when it can.
    unavailable: Callable[[], str | None] = lambda: None


# --- process helpers -------------------------------------------------------

def low_priority_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}

    def lower():
        os.nice(15)
        if shutil.which("ionice"):
            # Idle I/O class for this process; children inherit it.
            subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=False)

    return {"preexec_fn": lower}


# `git rev-parse --local-env-vars`: the variables that pin git to one
# repository. git exports some of them (GIT_DIR in a worktree) to the pre-push
# hook; a test fixture that inherits them runs `git -C <tmp> init/commit/config`
# against THIS repository instead of its own, rewriting branches and config.
GIT_LOCAL_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
})


def child_env(env: dict | None = None) -> dict:
    """The environment a check runs with: ours, minus git's repository pins."""
    merged = {key: value for key, value in os.environ.items()
              if key not in GIT_LOCAL_ENV and not key.startswith("GIT_CONFIG_KEY_")
              and not key.startswith("GIT_CONFIG_VALUE_")}
    merged.update(env or {})
    return merged


def _parents() -> list[tuple[int, int]]:
    """(pid, parent pid) for every process: /proc on Linux (slim images lack ps), else ps."""
    proc = Path("/proc")
    if (proc / "self" / "stat").is_file():
        pairs = []
        for entry in proc.iterdir():
            if entry.name.isdigit():
                try:  # "pid (name) state ppid ..."; the name may contain spaces
                    pairs.append((int(entry.name), int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])))
                except (OSError, ValueError, IndexError):
                    continue
        return pairs
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                                 text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [(int(parts[0]), int(parts[1])) for parts in map(str.split, listing.splitlines())
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()]


def descendants(pid: int) -> list[int]:
    """Process IDs below ``pid``, best effort (pytest-xdist workers, npm's node)."""
    children: dict[int, list[int]] = {}
    for child, parent in _parents():
        children.setdefault(parent, []).append(child)
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


def kill_tree(process: subprocess.Popen) -> None:
    """Stop a command and everything it started, so nothing outlives a stopped stage."""
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, check=False)
        else:
            below = descendants(process.pid)  # before the parent dies and they are reparented
            for pid in [process.pid, *below]:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def run(argv: list[str], cwd: Path = ROOT, env: dict | None = None,
        timeout: float | None = None, *, retry: bool = True) -> None:
    # All child scratch (Python, Node and Chromium) belongs to this command.
    # Remove it on failures, timeout and Ctrl-C, after _run has reaped children.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.core.temp_scratch import cleanup_on_sigterm, scratch_directory

    with cleanup_on_sigterm(), scratch_directory("ai-shuffle-ci-") as directory:
        scratch_env = {
            "AI_SHUFFLE_PYTEST_GATE_DIR": os.environ.get("AI_SHUFFLE_PYTEST_GATE_DIR") or tempfile.gettempdir(),
            # Independently owned pytest/browser roots must not nest beneath
            # this command's TMPDIR: AF_UNIX sockets have a short path limit.
            "AI_SHUFFLE_TEST_SCRATCH_ROOT": os.environ.get("AI_SHUFFLE_TEST_SCRATCH_ROOT") or tempfile.gettempdir(),
            **(env or {}), **dict.fromkeys(("TMPDIR", "TEMP", "TMP"), directory),
        }
        _run(argv, cwd=cwd, env=scratch_env, timeout=timeout, retry=retry)


def _run(argv: list[str], cwd: Path = ROOT, env: dict | None = None,
         timeout: float | None = None, *, retry: bool = True) -> None:
    exe = shutil.which(argv[0]) or argv[0]
    shown = " ".join(argv)
    rel = cwd.relative_to(ROOT).as_posix() if cwd != ROOT else "."
    stop_at_deadline = False
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left <= 0:
            raise OutOfTime(f"no time left in this invocation's budget for {shown}")
        if timeout is None or left < timeout:
            timeout, stop_at_deadline = left, True
    print(f"\n$ ({rel}) {shown}", flush=True)
    merged = child_env(env)
    failed_file = None
    if retry and os.environ.get(RETRY_FAILED_ENV) == "1" and argv[1:3] == ["-m", "pytest"]:
        failed_file = Path(merged["TMPDIR"]) / "failed.txt"
        merged["LOCAL_CI_FAILED_FILE"] = str(failed_file)
        argv = [*argv, "-p", "scripts.ci.record_failures"]
    process = subprocess.Popen([exe, *argv[1:]], cwd=cwd, env=merged, **low_priority_kwargs())
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        if stop_at_deadline:
            raise OutOfTime(f"stopped {shown} (in {rel}) at the end of this invocation's budget") from None
        raise Failure("local check timed out; push blocked") from None
    except BaseException:
        # Ctrl-C, or SIGTERM turned into SystemExit by full --step: leave no orphans.
        kill_tree(process)
        raise
    if returncode and failed_file is not None:
        failed = (list(dict.fromkeys(failed_file.read_text(encoding="utf-8").splitlines()))
                  if failed_file.exists() else [])
        # Nothing recorded (a collection error, a crash) or many failures: a real break.
        if failed and len(failed) <= MAX_RETRIED:
            print(f"\n{len(failed)} failed test(s); running them once more.", flush=True)
            rerun = [argv[0], "-m", "pytest", *failed, *pytest_args(argv[0], parallel=False)]
            try:
                run(rerun, cwd=cwd, env=env, timeout=timeout, retry=False)
            except Failure:
                raise Failure(f"{shown} (in {rel}) exited {returncode}; "
                              f"its {len(failed)} failed test(s) failed again") from None
            FLAKY.extend(failed)
            return
    if returncode:
        raise Failure(f"{shown} (in {rel}) exited {returncode}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def npm_ready(directory: str) -> Path:
    """Install a package's locked dependencies once per checkout."""
    path = ROOT / directory
    if not (path / "node_modules").is_dir():
        run(["npm", "ci", "--no-audit", "--no-fund"], cwd=path)
    return path


def python_env() -> dict:
    # fluid is imported from the checkout, never an installed copy.
    paths = [str(ROOT), str(ROOT / "fluid"), os.environ.get("PYTHONPATH", "")]
    if os.name == "nt":
        paths.insert(2, str(ROOT / "scripts" / "ci"))  # the windows_known_failures plugin
    return {"PYTHONPATH": os.pathsep.join(p for p in paths if p)}


def has_module(python: str, module: str) -> bool:
    return subprocess.run([python, "-c", f"import {module}"], capture_output=True).returncode == 0


# Import names for requirements-dev.txt, plus PyYAML used by the gate itself.
DEV_MODULES = (
    "pytest", "pytest_asyncio", "pytest_cov", "pytest_mock", "xdist",
    "pytest_timeout", "pytest_socket", "distlib", "httpx", "freezegun", "ruff", "yaml",
)


def python_preflight(python: str) -> None:
    """Refuse an incomplete/contaminated interpreter before any commit status."""
    hint = ("Use a fresh venv with requirements.txt + requirements-dev.txt "
            "and set LOCAL_CI_PYTHON to its Python; clear external PYTHONPATH entries.")
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry and not (ROOT / entry).resolve().is_relative_to(ROOT.resolve()):
            raise Failure(f"Inherited PYTHONPATH points outside this checkout. {hint}")
    probe = """
import importlib, json, pathlib, sys
root = pathlib.Path.cwd().resolve()
errors = []
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception:
        errors.append('cannot import ' + name)
try:
    importlib.import_module('claude_agent_sdk')
except ImportError:
    pass
else:
    errors.append('claude_agent_sdk is importable')
try:
    docker = importlib.import_module('docker')
    locations = list(getattr(docker, '__path__', []))
    if getattr(docker, '__file__', None):
        locations.append(docker.__file__)
    if not locations or any(not pathlib.Path(p).resolve().is_relative_to(root) for p in locations):
        errors.append('docker resolves outside the checkout')
except ImportError:
    errors.append('cannot import the checkout docker namespace')
print(json.dumps(errors))
"""
    try:
        result = subprocess.run([python, "-c", probe, *DEV_MODULES], cwd=ROOT,
                                capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Failure(f"Python preflight could not complete. {hint}") from None
    try:
        errors = json.loads(result.stdout) if result.returncode == 0 else ["interpreter probe failed"]
    except ValueError:
        errors = ["interpreter probe returned invalid output"]
    if errors:
        raise Failure(f"Python preflight: {', '.join(errors)}. {hint}")


def pytest_args(python: str, timeout: int = 120, parallel: bool = True) -> list[str]:
    for module in ("pytest", "pytest_asyncio"):
        if not has_module(python, module):
            raise Failure(f"{python} has no {module}. Install requirements-dev.txt into it, "
                          "or set LOCAL_CI_PYTHON to an interpreter that has them.")
    args = ["-o", "addopts=", "-q", "-p", "no:cacheprovider"]
    if parallel and has_module(python, "xdist") and WORKERS not in ("", "0", "1"):
        args += ["-n", WORKERS]
    if has_module(python, "pytest_timeout"):
        args += [f"--timeout={timeout}"]
    if os.name == "nt":
        # Skips the tests in scripts/ci/windows_known_failures.txt (kept empty).
        args += ["-p", "windows_known_failures"]
    return args


def windows_known_files() -> set[str]:
    """Whole test files listed as failing on Windows; empty elsewhere."""
    if os.name != "nt":
        return set()
    listing = ROOT / "scripts/ci/windows_known_failures.txt"
    if not listing.is_file():
        return set()
    lines = listing.read_text(encoding="utf-8").splitlines()
    return {entry for line in lines if (entry := line.split("#", 1)[0].strip()) and "::" not in entry}


# --- backend test selection ------------------------------------------------

def select_backend_tests(ctx: Context) -> list[str] | str:
    """'all', or the test files a change reaches. Uses the recorded map when present."""
    if MAP_PATH.is_file():
        result = subprocess.run(
            [ctx.python, "scripts/ci/backend_test_map.py", "select", "--map", str(MAP_PATH), "--base", ctx.base],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        sys.stderr.write(result.stderr)
        if result.returncode == 0:
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines[:1] in (["all"], ["none"]):
                return lines[0] if lines[0] == "all" else []
            return lines
        print("Test map selection failed; falling back to name matching.")
    else:
        ctx.notes.append("No backend test map on this machine yet; tests were picked by name. "
                         "`python scripts/local_ci.py full` records one.")
    if ctx.mode == "fast":
        return affected_backend_tests(ctx, use_map=False)
    return select_by_name(ctx.files or [])


def test_file_path(name: str) -> bool:
    """A changed pytest file owns itself, even outside the usual discovery roots."""
    return (name.startswith(("tests/", "fluid/tests/"))
            and Path(name).name.startswith("test_") and name.endswith(".py"))


def backend_wide_change(files: list[str]) -> bool:
    return any(fnmatch(name, pattern) for name in files
               for pattern in ("requirements*", "pytest.ini", "tests/conftest.py", "tests/*/conftest.py"))


def select_by_name(files: list[str]) -> list[str] | str:
    if backend_wide_change(files):
        return "all"
    roots = [ROOT / d for d in FAST_SUBSET]
    tests = [p.relative_to(ROOT).as_posix() for d in roots for p in d.rglob("test_*.py")]
    tests += [p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py")]
    selected = {name for name in files if test_file_path(name)}
    for name in files:
        if not name.endswith(".py") or test_file_path(name):
            continue
        module = name[:-3].replace("/", ".")
        if module.startswith("fluid.fluid."):
            module = module[len("fluid."):]
        stem = module.rsplit(".", 1)[-1]
        parent = module.rsplit(".", 1)[0] if "." in module else ""
        pattern = re.compile(rf"\b{re.escape(module)}\b|from {re.escape(parent)} import [^\n]*\b{re.escape(stem)}\b")
        if name.startswith("tests/"):
            # Helper names also appear in relative/bare imports and dynamic loads.
            pattern = re.compile(rf"\b{re.escape(stem)}\b")
        for test in tests:
            try:
                if (Path(test).stem == f"test_{stem}"
                        or pattern.search((ROOT / test).read_text(encoding="utf-8", errors="ignore"))):
                    selected.add(test)
            except OSError:
                continue
    return sorted(t for t in selected
                  if (t not in FAST_SUBSET_EXCLUDED or t in files) and (ROOT / t).is_file())


# --- suites ----------------------------------------------------------------

def affected_backend_tests(ctx: Context, *, use_map: bool = True) -> list[str] | str:
    """Pick affected tests; unmapped paths are covered by full PR CI, not this gate."""
    if backend_wide_change(ctx.files or []):
        return "all"
    if (use_map and MAP_PATH.is_file()
            and not any(test_file_path(name) or name.startswith("fluid/") for name in ctx.files or [])
            and set(changed_files(ctx.base)) <= set(ctx.files or [])):
        # The map CLI compares the whole Git diff. When other areas were
        # removed from this context (notably workflow changes), do not let
        # those unrelated paths turn a targeted backend check into "all".
        mapped = select_backend_tests(ctx)
        if mapped != "all":
            return mapped
        # "all" can also mean the recorded map has an unknown source path.
        # Only the shared pytest/dependency inputs above justify a full backend.
    selected: set[str] = set()
    for name in ctx.files or []:
        if test_file_path(name) and name.startswith("fluid/tests/"):
            continue  # The fluid suite owns these, never the backend fallback.
        if not any(fnmatch(name, pattern) for pattern in BACKEND_PATHS):
            continue
        matches = select_by_name([name])
        if test_file_path(name):
            selected.update(matches)  # A deleted test needs no execution.
            continue
        if matches == "all":
            return "all"
        if not matches and not name.startswith("fluid/"):
            ctx.notes.append(f"No local test mapping for {name}; covered by the full suite on PR CI")
        selected.update(matches)
    return sorted(selected)


def backend(ctx: Context) -> None:
    env = python_env()
    args = pytest_args(ctx.python)
    if ctx.changed(["app/*", "scripts/scan_blocking_db.py", "tests/test_scan_blocking_db.py"]):
        run([ctx.python, "-m", "pytest", "tests/test_scan_blocking_db.py", *args], env=env)
        run([ctx.python, "scripts/scan_blocking_db.py"], env=env)
    selection = ("all" if ctx.mode == "full" else
                 affected_backend_tests(ctx) if ctx.mode == "fast" else select_backend_tests(ctx))
    if selection == "all":
        targets = [*FAST_SUBSET, *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py"))]
        extra = ["--deselect=tests/test_scan_blocking_db.py", *[f"--ignore={p}" for p in FAST_SUBSET_EXCLUDED]]
    else:
        targets, extra = selection, []
    # pytest collects a file named on the command line even when a plugin
    # would ignore it, so listed Windows-only failures are dropped here.
    known = windows_known_files()
    targets = [t for t in targets if t not in known]
    if ctx.backend_selected is not None:
        # Keep directory collection hooks. Explicit root files need filtering too:
        # pytest does not call ignore_collect for explicit command-line files.
        targets = [t for t in targets if not t.endswith(".py") or t in ctx.backend_selected]
        env = {**env, "LOCAL_CI_BACKEND_BATCH": json.dumps(ctx.backend_selected)}
        extra += ["-p", "scripts.ci.local_ci_batch"]
    if not targets:
        print("No backend tests reach this change.")
        return
    print(f"Backend: {'the whole fast subset' if selection == 'all' else f'{len(targets)} test files'}")
    with ExitStack() as scratch:
        hits = None
        if ctx.mode == "full" and sys.version_info >= (3, 12):
            # Staged runs own map_hits across invocations; never remove theirs.
            hits = ctx.map_hits or Path(scratch.enter_context(
                tempfile.TemporaryDirectory(prefix="local-ci-map-")))
            env = {**env, "BACKEND_TEST_MAP_DIR": str(hits)}
            extra = [*extra, "-p", "tests._test_map"]
        run([ctx.python, "-m", "pytest", *targets, *extra, *args], env=env)
        if hits is not None and ctx.map_hits is None:
            MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            run([ctx.python, "scripts/ci/backend_test_map.py", "merge", "--hits", str(hits),
                 "--out", str(MAP_PATH), "--commit", git("rev-parse", "HEAD")], env=env)


def backend_linux(ctx: Context) -> None:
    """Steps CI Backend ran on Linux with real PostgreSQL and bubblewrap."""
    env = python_env()
    args = pytest_args(ctx.python, timeout=240)
    pg_ctl = shutil.which("pg_ctl")
    pg_bin = os.environ.get("SMOKE_PG_BIN") or (str(Path(pg_ctl).parent) if pg_ctl else "")
    pressure_optin = os.environ.get("LOCAL_CI_CONTAINER_PRESSURE") == "1"
    pg_optin = os.environ.get("LOCAL_CI_CONTAINER_PG") == "1" or pressure_optin
    if ctx.mode == "full" and pg_optin and not pg_bin:
        raise Failure("Container PostgreSQL opt-in requested but no PostgreSQL binaries were found; "
                      "install PostgreSQL or set SMOKE_PG_BIN.")
    if shutil.which("bwrap"):
        run([ctx.python, "-m", "pytest", "tests/core/test_chat_shell.py", "-rs", *args],
            env={**env, "CHAT_SHELL_REQUIRE_SANDBOX": "1"})
    else:
        ctx.notes.append("Isolated folder shell tests not run: bubblewrap (bwrap) is not installed here.")
    if pg_bin:
        run([ctx.python, "-m", "pytest", "tests/smoke", "tests/db/test_schema_lifecycle.py", "-m", "boot_smoke",
             "-rs", *pytest_args(ctx.python, timeout=240, parallel=False)],
            env={**env, "BOOT_SMOKE_REQUIRED": "1", "SMOKE_PG_BIN": pg_bin})
    else:
        ctx.notes.append("Boot smoke test not run: no PostgreSQL binaries (set SMOKE_PG_BIN).")
    # These files stay outside the parallel fast subset: they mutate supervisor
    # globals and measure idle/checkpoint timing under bounded disk pressure.
    # Each real-PG case creates its own cluster under tmp_path; no shared server
    # or TEST_POSTGRES_DSN is needed. Override inherited opt-ins/DSNs explicitly.
    # Keep check/full deterministic on busy developer machines. Until #350 is
    # fixed, even the functional crash-recovery case hits a host-sensitive
    # checkpoint budget. Both real-PG groups therefore require explicit opt-in.
    container_pg = ctx.mode == "full" and bool(pg_bin) and pg_optin
    container_pressure = container_pg and pressure_optin
    container_env = {
        **env, "ALLOW_REAL_PG": "0", "TEST_POSTGRES_DSN": "",
        "AI_SHUFFLE_ENV_FILE": "/dev/null",
        "CONTAINER_RUNTIME_TEST_PG": "1" if container_pg else "0",
        "CONTAINER_RUNTIME_TEST_PRESSURE": "1" if container_pressure else "0",
    }
    if pg_bin:
        container_env["PATH"] = pg_bin + os.pathsep + os.environ.get("PATH", "")
    if ctx.mode != "full":
        ctx.notes.append("Container PostgreSQL functional cases are full-mode only; "
                         "check runs the plain serial container contracts.")
    elif not pg_bin:
        ctx.notes.append("Container PostgreSQL and disk-pressure cases skipped: "
                         "no PostgreSQL binaries (set SMOKE_PG_BIN).")
    run([ctx.python, "-m", "pytest", "tests/core/test_container_runtime.py",
         "tests/core/test_container_image_contract.py", "-rs",
         *pytest_args(ctx.python, timeout=120, parallel=False)], env=container_env)
    if not container_pg:
        ctx.notes.append("Container real-PG coverage is opt-in pending #350: run full with "
                         "LOCAL_CI_CONTAINER_PG=1 for functional cases or "
                         "LOCAL_CI_CONTAINER_PRESSURE=1 for measurements (implies PG); "
                         "both require PostgreSQL binaries.")
    elif not container_pressure:
        ctx.notes.append("Container checkpoint measurements disabled pending #350; run full with "
                         "LOCAL_CI_CONTAINER_PRESSURE=1 to opt in (implies PG).")


FLUID_TESTS = "fluid/tests/unit"


def fluid(ctx: Context) -> None:
    env, extra = python_env(), []
    targets = [FLUID_TESTS]
    changed = [name for name in ctx.files or [] if name.startswith("fluid/")]
    if ctx.mode == "fast" and changed and all(test_file_path(name) for name in changed):
        targets = [name for name in changed if (ROOT / name).is_file()]
        if not targets:
            return  # Only deleted tests changed.
    if ctx.backend_selected is not None:
        # One full --step shard: the same directory and hooks, other files ignored.
        env = {**env, "LOCAL_CI_BACKEND_BATCH": json.dumps(ctx.backend_selected)}
        extra = ["-p", "scripts.ci.local_ci_batch"]
    run([ctx.python, "-m", "pytest", *targets, *extra, *pytest_args(ctx.python)], env=env)


def frontend(ctx: Context) -> None:
    path = npm_ready("frontend")
    run(["npx", "svelte-kit", "sync"], cwd=path)
    run(["npx", "svelte-check", "--tsconfig", "./tsconfig.json"], cwd=path)
    if ctx.mode == "full" or ctx.changed(["frontend/package*.json", "frontend/vite.config.js", "frontend/svelte.config.js"]):
        run(["npm", "run", "build"], cwd=path)
    run(["npm", "test"] if ctx.mode == "full" else ["node", "scripts/vitest-changed.mjs", ctx.base], cwd=path)
    run(["npm", "run", "contract:check"], cwd=path)


def mobile(ctx: Context) -> None:
    npm_ready("frontend")
    path = ROOT / "mobile"
    run(["npm", "run", "deps"], cwd=path)
    run(["npm", "run", "check"], cwd=path)
    run(["npm", "test"] if ctx.mode == "full" else ["node", "../frontend/scripts/vitest-changed.mjs", ctx.base], cwd=path)


def node_package(directory: str, *scripts: str) -> Callable[[Context], None]:
    def suite(ctx: Context) -> None:
        path = npm_ready(directory)
        for script in scripts:
            run(["npm", "run", script] if script != "test" else ["npm", "test"], cwd=path)
    return suite


def tools(ctx: Context) -> None:
    node_package("tools", "test")(ctx)
    run(["node", "tools/scripts/verify-network-vectors.mjs"])


def browser_mcp(ctx: Context) -> None:
    node_package("browser-mcp", "build", "test")(ctx)


def docs(ctx: Context) -> None:
    left, right = ROOT / "docs/content", ROOT / "frontend/src/lib/docs/content"

    def files(base: Path) -> dict[str, bytes]:
        # A Windows checkout may give one copy CRLF endings; the committed bytes match.
        return {p.relative_to(base).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
                for p in base.rglob("*") if p.is_file()}

    ours, theirs = files(left), files(right)
    mismatched = sorted(n for n in ours.keys() | theirs.keys() if ours.get(n) != theirs.get(n))
    if mismatched:
        raise Failure("docs/content and frontend/src/lib/docs/content differ: " + ", ".join(mismatched[:10]))


def desktop(ctx: Context) -> None:
    node_package("desktop", "typecheck", "test")(ctx)


def android_bridge(ctx: Context) -> None:
    run(["node", "--test", "app/src/test/js/chat-downloads.test.cjs"], cwd=ROOT / "android")


def workflows(ctx: Context) -> None:
    for module in ("yaml", "pytest"):
        if not has_module(ctx.python, module):
            raise Failure(f"{ctx.python} needs {module} (PyYAML, pytest) for the workflow policy checks")
    run([ctx.python, "scripts/ci/test-artifact-policy.py"])
    run([ctx.python, "scripts/ci/test-cost-policy.py"])
    run([ctx.python, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "--confcutdir=tests/scripts",
         "tests/scripts/test_owner_publication_policy.py", "tests/scripts/test_edge_publication_gate.py",
         "tests/scripts/test_local_ci.py", "tests/scripts/test_local_ci_preflight.py",
         "tests/scripts/test_local_ci_stages.py", "tests/scripts/test_security_gate.py",
         "tests/scripts/test_cloud_split.py"])


def android_play(ctx: Context) -> None:
    for script in ("test-version-name.py", "test-mobile-apk.py", "test-emulator-readiness.py",
                   "test-instrumentation-parser.py", "test-ui-runner.py"):
        run([ctx.python, f"android/ci/{script}"])
    # The workflow test parses each inline step with /bin/bash -n.
    if Path("/bin/bash").exists():
        run([ctx.python, "android/ci/test-play-workflow.py"])
    else:
        ctx.notes.append("Play workflow shell checks not run: they need /bin/bash (a Linux worker).")


def update_reach(ctx: Context, *, head: str = "HEAD", branch: str | None = None) -> None:
    labels, draft = "", True
    reason = "gh is unavailable; treating this branch as having no PR"
    if shutil.which("gh"):
        # Labels and draft state are live policy, not part of a cached tree pass.
        try:
            result = subprocess.run(["gh", "pr", "view", *([branch] if branch else []), "--json", "isDraft,labels"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=20)
            if result.returncode:
                reason = ("no PR exists for this branch" if "no pull requests found" in result.stderr.lower()
                          else "gh could not read PR state (authentication or API failure); "
                               "treating this branch as having no PR")
            else:
                pr = json.loads(result.stdout)
                if not isinstance(pr["isDraft"], bool):
                    raise ValueError("Invalid draft state")
                labels = ",".join(label["name"] for label in pr["labels"])
                draft = pr["isDraft"]
                reason = "the PR is a draft"
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            reason = "gh PR state is unavailable or invalid; treating this branch as having no PR"
    if draft:
        ctx.log(f"NOTE: update-reach is advisory because {reason}. "
                "Owner acceptance labels are still required before readiness/merge.")
    run([ctx.python, "scripts/ci/update_reach.py", "--base", ctx.base, "--head", head, "--labels", labels,
         *(["--draft"] if draft else [])])


def update_reach_checks(ctx: Context) -> None:
    # Non-Python launcher/image changes do not reach backend's name selector.
    run([ctx.python, "-m", "pytest", "tests/test_update_reach.py",
         "tests/scripts/test_local_ci.py", "tests/scripts/test_edge_publication_gate.py",
         *pytest_args(ctx.python)], env=python_env())
    update_reach(ctx)


def android_app(ctx: Context) -> None:
    gradlew = "gradlew.bat" if os.name == "nt" else "./gradlew"
    run([str(ROOT / "android" / gradlew), "testDebugUnitTest", "--max-workers=2"], cwd=ROOT / "android")


def android_unavailable() -> str | None:
    if not (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")):
        return "no Android SDK (ANDROID_HOME) on this machine"
    return None


def linux_only() -> str | None:
    return None if sys.platform.startswith("linux") else "Linux only (bubblewrap, PostgreSQL boot smoke, container contracts)"


SUITES = [
    Suite("backend", BACKEND_PATHS, backend, "Backend pytest: the files a change reaches (full: the whole fast subset)"),
    Suite("backend-linux", ["app/core/chat_shell.py", "app/core/confined_files.py", "app/db/*", "app/main.py",
                            "docker/*", "Dockerfile", "entrypoint.sh", "docker-compose.yml",
                            "tests/smoke/*", "tests/core/test_chat_shell.py",
                            "tests/core/test_container_runtime.py", "tests/core/test_container_image_contract.py"],
          backend_linux, "Sandbox shell, PostgreSQL boot smoke and serial container contracts", linux_only),
    Suite("fluid", ["fluid/*", "conftest.py", "scripts/ci/test_scratch.py", "app/core/temp_scratch.py"],
          fluid, "fluid unit suite"),
    Suite("frontend", ["frontend/*", "app/contract/schema/*"], frontend,
          "svelte-check, Vitest for the files a change reaches, contract check (full: build and every test)"),
    Suite("mobile", ["mobile/*", "frontend/src/*", "frontend/package*.json"], mobile, "Mobile UI type check and tests"),
    Suite("tools", ["tools/*", "tests/core/network/vectors.json"], tools, "AI tools package and network vectors"),
    Suite("browser-mcp", ["browser-mcp/*"], browser_mcp, "Browser MCP build and tests"),
    Suite("docs", ["docs/content/*", "frontend/src/lib/docs/content/*"], docs, "App docs copies match"),
    Suite("desktop", ["desktop/*"], desktop, "Desktop typecheck and unit tests"),
    Suite("android-bridge", ["android/app/src/test/js/*"], android_bridge, "Android download bridge tests"),
    Suite("android-play", ["android/ci/*", ".github/workflows/android.yml", ".github/workflows/android-play*.yml", ".github/workflows/release.yml"],
          android_play, "Android emulator and Play workflow safety checks"),
    Suite("android-app", ["android/app/*", "android/build.gradle*", "android/settings.gradle*", "android/gradle*"],
          android_app, "Android unit tests (Gradle)", android_unavailable),
    Suite("workflows", [".github/workflows/*", "scripts/ci/*", "tests/scripts/test_owner_publication_policy.py",
                        "tests/scripts/test_edge_publication_gate.py", "tests/scripts/test_local_ci*.py", "scripts/local_ci*.py",
                        ".githooks/*", ".gitleaks.toml", "scripts/secret_scan.py", "scripts/dependency_audit.py",
                        "tests/scripts/test_security_gate.py"], workflows, "Workflow and publication policy tests"),
    Suite("update-reach", ["Dockerfile", "entrypoint.sh", "docker/*", "requirements*", "desktop/src/main/*",
                           "desktop/scripts/*", "desktop/build/*", "desktop/electron-builder.yml",
                           "desktop/src/pages/*", "desktop/package-lock.json", "desktop/launcher.json",
                           "scripts/source_archive.py", "scripts/ci/update_reach.py"],
          update_reach_checks, "Update-reach regression tests and installed-app update verdict"),
]

# The Windows full run (about 75 minutes) is longer than one Monitor job, so it
# goes in checkpointed chunks (docs/ci/pr-checks.md, "Full gates longer than one
# Monitor budget"). Release hints name this command, not the one-shot `full --attest`.
WINDOWS_FULL_COMMAND = "python scripts/local_ci.py full --step --attest"
WINDOWS_FULL_HINT = (f"`{WINDOWS_FULL_COMMAND}` (one Monitor job per run; "
                     "repeat the same command until it prints COMPLETE)")

# Covered only at release time or by hand: no hardware here, or a full image build.
NOT_LOCAL = [
    (["ios/*"], "iOS app: needs a Mac. Runs only when the owner dispatches ios.yml before a release."),
    (["app/core/chat_files.py", "app/core/confined_files.py"],
     "macOS folder confinement: needs a Mac. Runs only when the owner dispatches ci-folder-confinement.yml."),
    (["Dockerfile", "docker/*", "entrypoint.sh", "frontend/src/routes/setup/*", "frontend/src/lib/components/setup/*"],
     "Container image: built and verified by the release workflow, or locally with "
     "`python scripts/verify_container_postgres.py` where Docker runs."),
] + ([] if os.name == "nt" else [
    (["desktop/*", "app/core/config.py", "app/core/process*.py", "app/core/*windows*", "run_local.*"],
     f"Windows: not tested on this machine. A stable release waits for {WINDOWS_FULL_HINT} "
     "on a Windows worker; edge does not."),
])


# --- pass records ----------------------------------------------------------

def records_path() -> Path:
    return Path(git("rev-parse", "--git-dir")).resolve() / "local-ci" / "passes.json"


def load_records() -> dict:
    try:
        return json.loads(records_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_record(tree: str, record: dict) -> None:
    path = records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    records = load_records()
    records[tree] = record
    # Keep the newest 200 trees.
    newest = sorted(records.items(), key=lambda item: item[1].get("at", 0))[-200:]
    path.write_text(json.dumps(dict(newest), indent=1), encoding="utf-8")


def clean_checkout() -> bool:
    return git("status", "--porcelain") == ""


def tree_of(revision: str) -> str:
    return git("rev-parse", f"{revision}^{{tree}}")


# --- commands --------------------------------------------------------------

PYTHON_CONFIG = "local-ci.python"


def saved_python() -> str:
    """The interpreter `install` recorded in git config, shared by every worktree."""
    found = subprocess.run(["git", "config", "--get", PYTHON_CONFIG], cwd=ROOT, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else ""


def choose_python() -> str:
    return os.environ.get("LOCAL_CI_PYTHON") or saved_python() or sys.executable


def github_remote(repository: str) -> str | None:
    """The remote (origin first) whose URL is ``repository`` on GitHub, or None."""
    remotes = git("remote").splitlines()
    for remote in sorted(remotes, key=lambda name: (name != "origin", name)):
        url = git("remote", "get-url", remote).rstrip("/")
        if re.fullmatch(GITHUB_URL + re.escape(repository) + r"(?:\.git)?", url, re.IGNORECASE):
            return remote
    return None


def resolve_base(*, public_only: bool = False) -> str:
    """Prefer the public repository, not a remote name inherited from an archive.

    Comparison overrides are useful for forks, but cannot authorize publication.
    """
    if not public_only:
        override = os.environ.get("LOCAL_CI_BASE", "").strip()
        if not override:
            configured = subprocess.run(["git", "config", "--get", "local-ci.base"],
                                        cwd=ROOT, capture_output=True, text=True)
            override = configured.stdout.strip() if configured.returncode == 0 else ""
        if override:
            return override.removeprefix("refs/remotes/")
    remote = github_remote(REPOSITORY)
    if remote:
        return f"{remote}/main"
    raise Failure(f"No remote points to {REPOSITORY}; add a public repository remote"
                  + (" for --attest" if public_only else
                     " or set LOCAL_CI_BASE / git config local-ci.base to <remote>/<branch>"))


def fetch_base(base: str, *, strict: bool = False) -> None:
    """Refresh the exact tracking ref, even with a restricted fetch refspec.

    Worktrees of one clone share their remote refs, so two local CI runs that
    fetch at the same moment can lose git's ref lock. Retry once; then, unless
    ``strict`` (attestation needs the current revision), carry on with the
    tracking ref already on disk and say so.
    """
    for remote in sorted(git("remote").splitlines(), key=len, reverse=True):
        if base.startswith(remote + "/"):
            branch = base[len(remote) + 1:]
            try:
                git("check-ref-format", f"refs/heads/{branch}")
            except subprocess.CalledProcessError:
                raise Failure(f"Invalid local CI base {base!r}; expected <remote>/<branch>") from None
            for attempt in range(2):
                try:
                    git("fetch", "-q", "--", remote,
                        f"+refs/heads/{branch}:refs/remotes/{base}")
                    return
                except subprocess.CalledProcessError:
                    if attempt == 0:
                        time.sleep(2)
            if not strict:
                try:
                    git("rev-parse", "--verify", "-q", f"refs/remotes/{base}")
                except subprocess.CalledProcessError:
                    pass
                else:
                    print(f"Warning: could not refresh {base}; comparing with the copy already fetched.")
                    return
            raise Failure(f"Could not refresh local CI base {base}; check the remote and branch")
    raise Failure(f"Invalid local CI base {base!r}; expected <remote>/<branch>")


def comparison_base(base: str, revision: str) -> str:
    try:
        return git("merge-base", base, revision)
    except subprocess.CalledProcessError:
        raise Failure(f"No common history between {revision} and {base}; use a branch based on "
                      "that remote or set LOCAL_CI_BASE / git config local-ci.base") from None


def changed_files(base: str) -> list[str]:
    names = set(git("diff", "--name-only", f"{base}...HEAD").splitlines())
    names |= set(git("diff", "--name-only", "HEAD").splitlines())
    names |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(n for n in names if n)


def run_suites(mode: str, only: list[str] | None, python: str) -> tuple[list[str], list[str], list[str]]:
    target = resolve_base()
    if mode == "changed":
        fetch_base(target)
    base = comparison_base(target, "HEAD")
    files = None if mode == "full" else changed_files(base)
    ctx = Context(mode=mode, base=base, files=files, python=python)
    if files is not None:
        print(f"{len(files)} files differ from {target}.")
    ran, skipped = [], []
    started = time.monotonic()
    for suite in SUITES:
        if only and suite.name not in only:
            continue
        if not only and not ctx.changed(suite.paths):
            continue
        reason = suite.unavailable()
        if reason:
            skipped.append(f"{suite.name}: {reason}")
            continue
        print(f"\n=== {suite.name}: {suite.about}", flush=True)
        suite.run(ctx)
        ran.append(suite.name)
    for patterns, note in NOT_LOCAL:
        if files is None or any(fnmatch(f, p) for f in files for p in patterns):
            skipped.append(note)
    minutes = (time.monotonic() - started) / 60
    print(f"\nPassed in {minutes:.1f} min: {', '.join(ran) or 'nothing to run'}")
    for line in skipped + ctx.notes:
        print(f"Not covered here: {line}")
    return ran, skipped, ctx.notes


def machine() -> str:
    return f"{platform.node()} ({platform.system()})"


def warn_attestation_main_moved(revision: str, state: str) -> None:
    """A moved branch does not invalidate the tested tree or imply publication."""
    try:
        current = subprocess.check_output(
            ["gh", "api", f"repos/{REPOSITORY}/branches/main", "--jq", ".commit.sha"],
            cwd=ROOT, text=True, timeout=20).strip()
    except (OSError, subprocess.SubprocessError):
        print("WARNING: could not recheck main after attestation; publication will recheck it.")
        return
    if current != revision:
        print(f"WARNING: main moved from {revision[:9]} to {current[:9]}; posted {state} "
              f"{ATTEST_CONTEXT} only for the tested revision. A successful first-parent ancestor "
              "can publish at the next schedule/owner dispatch if it is newer than the published "
              "pointer and no newer attestation wins. Later commits still need their own full "
              "attestation; failed/pending or older revisions will not publish.")


def attest(state: str, description: str, revision: str) -> None:
    contexts = [ATTEST_CONTEXT] + ([WINDOWS_CONTEXT] if os.name == "nt" else [])
    for context in contexts:
        subprocess.run(["gh", "api", f"repos/{REPOSITORY}/statuses/{revision}", "-f", f"state={state}",
                        "-f", f"context={context}", "-f", f"description={description[:139]}"],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    warn_attestation_main_moved(revision, state)


def security_check(python: str, strict: bool = False, *, audit: bool = True) -> None:
    run([python, "scripts/secret_scan.py"], timeout=120)
    if audit:
        run([python, "scripts/dependency_audit.py", *(["--strict-audit"] if strict else [])])
    else:
        print("Dependency audit: not run on automatic push; run `python scripts/local_ci.py check` to refresh.")


def scan_push(local_sha: str, remote_sha: str, push_remote: str | None = None) -> None:
    # Always runs, even for a cached tree: a secret added then deleted in the
    # outgoing history must not slip through a clean final-tree pass.
    # ``push_remote`` (the hook's $1) bounds a new branch's scan by what that
    # remote's main already holds; see secret_scan.new_ref_boundary.
    target = ["--push-remote", push_remote] if push_remote else []
    run([choose_python(), "scripts/secret_scan.py", "--local", local_sha, "--remote", remote_sha, *target],
        timeout=120)


def attest_update_reach(revision: str) -> None:
    from ci.edge_publication_gate import update_reach_readiness

    ready, reason = update_reach_readiness(revision)
    if not ready:
        raise Failure(reason)
    print(reason)


def use_private_checkout() -> None:
    """Point the public-history bridge at the archive this clone already holds.

    Only when AI_SHUFFLE_PRIVATE_CHECKOUT is unset. A shared checkout often has
    the archive as a remote (origin) beside the public repository; its common
    Git directory is then a complete private clone, unless it is shallow.
    Nothing is imported into a public-only clone: without such a remote this
    does nothing and the bridge explains the variable.
    """
    if os.environ.get(PRIVATE_CHECKOUT_ENV, "").strip():
        return
    remote = github_remote(PRIVATE_REPOSITORY)
    if remote is None:
        return
    if git("rev-parse", "--is-shallow-repository") != "false":
        print(f"Not using remote '{remote}' ({PRIVATE_REPOSITORY}) for {PRIVATE_CHECKOUT_ENV}: "
              "this clone is shallow.")
        return
    common = Path(git("rev-parse", "--git-common-dir"))
    common = (common if common.is_absolute() else ROOT / common).resolve()
    try:
        # The bridge checks the archive's current main; refresh it, best effort.
        git("fetch", "-q", "--no-tags", "--", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    except subprocess.CalledProcessError:
        print(f"Warning: could not refresh {remote}/main; using the archive history already fetched.")
    os.environ[PRIVATE_CHECKOUT_ENV] = str(common)
    print(f"{PRIVATE_CHECKOUT_ENV} is unset; using this clone's {PRIVATE_REPOSITORY} remote "
          f"'{remote}' ({common}).")


def on_first_parent_history(revision: str, ref: str) -> bool:
    """Whether ``revision`` is on ``ref``'s first-parent history (a merged main revision)."""
    try:
        return revision in git("rev-list", "--first-parent", ref).split()
    except subprocess.CalledProcessError:
        return False


def attest_preflight(revision: str, resumed: bool = False) -> None:
    """Everything --attest needs, checked before any commit status or suite.

    A new run must start on the current public main. A staged run that already
    started on this revision (``resumed``) may finish after main moved on, as
    long as the revision is still on main's first-parent history: otherwise a
    merge between two ``--step`` chunks would throw the whole run away.
    """
    public_base = resolve_base(public_only=True)
    fetch_base(public_base, strict=True)
    tip = git("rev-parse", public_base)
    moved_on = resumed and revision != tip and on_first_parent_history(revision, public_base)
    if not clean_checkout() or (revision != tip and not moved_on):
        raise Failure(f"--attest runs only on a clean checkout of the current public main ({public_base})")
    if moved_on:
        print(f"Main moved to {tip[:9]} after this staged run started; finishing {revision[:9]}, "
              "which is still on public main's history.")
    use_private_checkout()
    attest_update_reach(revision)


def cmd_check(args) -> int:
    mode = "full" if args.command == "full" else "changed"
    revision = git("rev-parse", "HEAD")
    if args.attest and args.only:
        raise Failure("--attest cannot be combined with --only")
    if args.attest and mode != "full":
        raise Failure("--attest needs `full`")
    python = choose_python()
    python_preflight(python)
    if args.attest:
        # Seconds, not after a long run: public main, a clean tree and update
        # reach (with its private-archive bridge) before any status or suite.
        resumed = False
        if getattr(args, "step", False) and not getattr(args, "restart", False):
            import local_ci_stages
            resumed = local_ci_stages.started(sys.modules[__name__], revision)
        attest_preflight(revision, resumed=resumed)
    if getattr(args, "step", False) or args.attest:
        import local_ci_stages
        if getattr(args, "step", False) or local_ci_stages.completed(sys.modules[__name__], args, python, revision):
            # A completed staged gate for this exact checkout attests without rerunning.
            return local_ci_stages.run_step(sys.modules[__name__], args, python, revision)
    if args.attest:
        attest("pending", f"Full suite running on {machine()}", revision)
    clean = clean_checkout()
    try:
        security_check(python, getattr(args, "strict_audit", False), audit=not getattr(args, "_from_push", False))
        ran, skipped, _ = run_suites(mode, args.only, python)
        if args.attest:
            attest_update_reach(revision)
    except Failure as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        if args.attest:
            attest("failure", f"Failed on {machine()}: {error}", revision)
        return 1
    if clean and not args.only:
        save_record(tree_of("HEAD"), {"security_gate": SECURITY_GATE_VERSION,
                                      "audit_refreshed": not getattr(args, "_from_push", False),
                                      "mode": mode, "suites": ran, "not_covered": skipped,
                                      "machine": machine(), "commit": revision, "at": time.time()})
    elif not clean:
        print("Uncommitted changes: this pass is not recorded.")
    if args.attest:
        attest("success", f"{', '.join(ran)} passed on {machine()}", revision)
        print(f"Posted {ATTEST_CONTEXT} on {revision[:9]}; scheduled publication will recheck eligibility.")
    return 0


def documentation_path(name: str) -> bool:
    return (name.startswith(("docs/", "frontend/src/lib/docs/content/"))
            or name.endswith((".md", ".rst")))


def workflow_path(name: str) -> bool:
    suite = next(suite for suite in SUITES if suite.name == "workflows")
    return any(fnmatch(name, pattern) for pattern in suite.paths)


def affected_suites(files: list[str]) -> list[Suite]:
    """Use SUITES as the area map; unknown areas are noted and deferred to PR CI."""
    code = [name for name in files if not documentation_path(name)]
    ctx = Context(mode="fast", base="", files=code, python="")
    selected = [suite for suite in SUITES if ctx.changed(suite.paths)]
    # The dedicated workflow suite already runs gate and publication regression
    # tests. Do not turn its scripts into an unrelated backend-wide fallback.
    backend_files = [name for name in code
                     if any(fnmatch(name, pattern) for pattern in BACKEND_PATHS)
                     and (not name.startswith("fluid/")
                          or (name.endswith(".py") and not test_file_path(name)
                              and select_by_name([name])))]
    if not backend_files or all(workflow_path(name) for name in backend_files):
        selected = [suite for suite in selected if suite.name != "backend"]
    if any(documentation_path(name) for name in files):
        selected += [suite for suite in SUITES if suite.name == "docs" and suite not in selected]
    return selected


def fetch_push_base(remote: str | None) -> str:
    """Prefer a configured push remote's main, otherwise use the normal base."""
    if remote and remote in git("remote").splitlines():
        target = f"{remote}/main"
        try:
            fetch_base(target)
        except Failure:
            print("Push remote main unavailable; using the configured comparison base.")
        else:
            return target
    target = resolve_base()
    fetch_base(target)
    return target


def cmd_fast(args) -> int:
    """Optional affected-test run. The push gate does not run or require it."""
    if args.attest or args.only:
        raise Failure("fast does not accept --attest or --only")
    python = choose_python()
    # Never consult pass records before this scan, even for an unchanged tree.
    security_check(python, audit=False)
    target = resolve_base()
    fetch_base(target)
    base = comparison_base(target, "HEAD")
    files = changed_files(base)
    ctx = Context(mode="fast", base=base,
                  files=[name for name in files if not documentation_path(name)], python=python)
    ran, skipped = [], []
    for name in ctx.files or []:
        if (not any(fnmatch(name, pattern) for suite in SUITES for pattern in suite.paths)
                and not any(fnmatch(name, pattern) for patterns, _ in NOT_LOCAL for pattern in patterns)):
            ctx.notes.append(f"No local test mapping for {name}; covered by the full suite on PR CI")
    for suite in affected_suites(files):
        reason = suite.unavailable()
        if reason:
            skipped.append(f"{suite.name}: {reason}")
            continue
        print(f"\n=== {suite.name}: affected push checks", flush=True)
        if suite.name in {"frontend", "mobile"}:
            # Area-wide fallback: unlike Vitest related, this also covers files
            # read dynamically rather than imported. No production build on push.
            if suite.name == "mobile":
                npm_ready("frontend")
                run(["npm", "run", "deps"], cwd=ROOT / "mobile")
            node_package(suite.name, "test")(ctx)
        else:
            suite_ctx = ctx
            if suite.name == "backend":
                suite_ctx = Context(mode="fast", base=base, python=python, notes=ctx.notes,
                                    files=[name for name in ctx.files or [] if not workflow_path(name)])
            suite.run(suite_ctx)
        ran.append(suite.name)
    for patterns, note in NOT_LOCAL:
        if any(fnmatch(name, pattern) for name in files for pattern in patterns):
            skipped.append(note)
    for note in skipped + ctx.notes:
        print(f"Not covered here: {note}")
    print(f"Affected checks passed: {', '.join(ran) or 'nothing to run'}")
    return 0


def push_update_reach(revision: str, remote_ref: str) -> None:
    base = comparison_base(resolve_base(), revision)
    ctx = Context(mode="changed", base=base, files=None, python=choose_python())
    update_reach(ctx, head=revision, branch=remote_ref.removeprefix("refs/heads/"))


def cmd_pre_push(args) -> int:
    """Called by the pre-push hook with git's ref lines on stdin.

    The push gate runs no test suites: every pull request runs them on GitHub
    CI. It runs the offline secret scan on every outgoing ref, first and with
    no cache, then a whole-tree scan and the live update-reach policy for
    branch pushes. No pass record can skip any of these.

    The hook passes the remote being pushed to in PUSH_REMOTE_ENV, not as an
    argument: the installed hook is shared by every worktree of the clone,
    and an older checkout's local_ci.py would reject an unknown argument.
    """
    zero = "0" * 40
    # Consume hook-only inputs so nested Git checks cannot inherit this push.
    hook_version = os.environ.pop("LOCAL_CI_HOOK", None)
    remote_argument = os.environ.pop(PUSH_REMOTE_ENV, "")
    push_remote = (remote_argument or None) if hook_version == "4" else None
    # The scanner validates this against configured remotes before using
    # any tracking ref; a URL/path or unknown name scans all history.
    branches = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, remote_ref, remote_sha = parts
        if local_sha == zero:
            continue  # ref deletions introduce no objects
        scan_push(local_sha, remote_sha, push_remote)
        if remote_ref.startswith("refs/heads/"):
            branches.append((local_sha, remote_ref))  # tags get the scan only
    if not branches:
        return 0
    # The whole working tree as well, as `check` and `full` do; no network audits.
    security_check(choose_python(), audit=False)
    # Refresh the push remote's main (the next new-branch scan boundary) or,
    # for a URL or a remote without main, the configured comparison base.
    fetch_push_base(push_remote)
    for local_sha, remote_ref in branches:
        # Labels and draft state are live policy, checked on every push.
        push_update_reach(local_sha, remote_ref)
    print("pre-push: secret scan and update-reach passed; tests run in PR CI.")
    return 0


HOOK_SOURCE = ROOT / ".githooks" / "pre-push"
HOOK_MARKER = "# LOCAL-CI-PRE-PUSH"
# Set by the hook to git's first pre-push argument: the remote being pushed to.
PUSH_REMOTE_ENV = "LOCAL_CI_PUSH_REMOTE"


def hooks_dir() -> Path:
    """The directory git runs hooks from, honouring core.hooksPath.

    AI Shuffle points core.hooksPath at the main clone's .git/hooks and keeps
    its own managed hooks there, so install adds a file to that directory
    instead of redirecting core.hooksPath and switching those hooks off.
    """
    path = Path(git("rev-parse", "--git-path", "hooks"))
    return path if path.is_absolute() else (ROOT / path).resolve()


def install_hook(directory: Path, *, force: bool = False) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target, kept = directory / "pre-push", directory / "pre-push.user"
    source = HOOK_SOURCE.read_bytes().replace(b"\r\n", b"\n")
    if target.exists():
        installed = target.read_text(encoding="utf-8", errors="replace")
        version_pattern = rf"(?m)^{re.escape(HOOK_MARKER)} v(\d+)\s*$"
        current_version = re.search(version_pattern, installed)
        source_version = re.search(version_pattern, source.decode("utf-8"))
        if (current_version and source_version
                and int(current_version[1]) > int(source_version[1]) and not force):
            raise Failure(f"{target} has newer hook v{current_version[1]}; refusing to replace it "
                          f"with v{source_version[1]}. Use install --force to downgrade explicitly.")
        if HOOK_MARKER not in installed:
            if kept.exists():
                raise Failure(f"{target} is someone else's hook and {kept.name} already exists; merge them by hand.")
            target.replace(kept)
    target.write_bytes(source)
    target.chmod(0o755)
    return f"Pre-push checks are on: {target}" + (f" (the earlier hook still runs as {kept.name})" if kept.exists() else "")


def can_run_tests(python: str) -> bool:
    return subprocess.run([python, "-c", "import pytest, yaml"], capture_output=True).returncode == 0


def find_test_python(explicit: str | None = None) -> str:
    """The first interpreter that has pytest and PyYAML.

    The python3 on PATH is often an app or project venv without test tools
    (on the Unraid image it is the AI Shuffle runtime), so the system
    interpreters are tried too.
    """
    names = [explicit, os.environ.get("LOCAL_CI_PYTHON"), saved_python(), sys.executable]
    names += [shutil.which(name) for name in ("python3.12", "python3", "python")]
    names += ["/usr/local/bin/python3", "/usr/bin/python3"]
    tried = []
    for name in names:
        if not name or name in tried:
            continue
        tried.append(name)
        if Path(name).exists() and can_run_tests(name):
            return str(Path(name).absolute())
        if explicit and name == explicit:
            raise Failure(f"{explicit} cannot import pytest and yaml. Install requirements-dev.txt into it.")
    raise Failure("No Python with pytest and PyYAML found (tried " + ", ".join(tried) + "). "
                  "Install requirements-dev.txt, then run install again, or pass --python PATH.")


def cmd_install(args) -> int:
    python = find_test_python(args.python)
    message = install_hook(hooks_dir(), force=getattr(args, "force", False))
    git("config", PYTHON_CONFIG, python)
    print(message)
    print(f"Tests run with {python} (git config {PYTHON_CONFIG}).")
    return 0


def cmd_list(args) -> int:
    for suite in SUITES:
        reason = suite.unavailable()
        print(f"{suite.name:14} {suite.about}" + (f"  [not here: {reason}]" if reason else ""))
        print(f"{'':14} when: {', '.join(suite.paths)}")
    print("\nNot run locally:")
    for _, note in NOT_LOCAL:
        print(f"  {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="check", choices=["check", "fast", "full", "install", "list", "pre-push"])
    parser.add_argument("--only", nargs="+", metavar="SUITE", help="run just these suites (not recorded as a pass)")
    parser.add_argument("--attest", action="store_true", help=f"with full on main: post {ATTEST_CONTEXT}")
    parser.add_argument("--step", action="store_true",
                        help="with full: run resumable stages within --budget; repeat until COMPLETE")
    parser.add_argument("--restart", action="store_true", help="with full --step: discard prior stage receipts")
    parser.add_argument("--budget", type=float, metavar="MINUTES", default=None,
                        help="with full --step: keep running stages while the next one fits in MINUTES, "
                             "stop a stage at MINUTES and split test stages that do not fit "
                             "(default 40, inside a 45-minute Monitor budget; 0 runs one stage, unlimited)")
    parser.add_argument("--python", metavar="PATH", help="with install: the interpreter that runs the tests")
    parser.add_argument("--force", action="store_true", help="with install: allow downgrading a newer shared hook")
    parser.add_argument("--strict-audit", action="store_true",
                        help="fail on Python advisories, npm high/critical findings, or unavailable audits")
    args = parser.parse_args(argv)
    if args.step and (args.command != "full" or args.only):
        parser.error("--step requires full without --only")
    if args.restart and not args.step:
        parser.error("--restart requires --step")
    if args.budget is not None and (not args.step or args.budget < 0):
        parser.error("--budget requires --step and a number of minutes (0 or more)")
    if args.only:
        unknown = set(args.only) - {s.name for s in SUITES}
        if unknown:
            parser.error(f"unknown suite: {', '.join(sorted(unknown))}")
    handlers = {"check": cmd_check, "fast": cmd_fast, "full": cmd_check, "install": cmd_install, "list": cmd_list,
                "pre-push": cmd_pre_push}
    try:
        return handlers[args.command](args)
    except Failure as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
