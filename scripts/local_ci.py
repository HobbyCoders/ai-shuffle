"""Run the project's checks on this machine, before code leaves it.

CI does not test pull requests. This script does, on whichever worker the
change was made, at low CPU priority so the machine stays usable.

    python scripts/local_ci.py                 # the suites this branch's changes reach
    python scripts/local_ci.py full            # every suite, and record the backend test map
    python scripts/local_ci.py full --attest   # on main: also post local/full-suite for edge publication
    python scripts/local_ci.py install         # turn on the pre-push hook for this clone
    python scripts/local_ci.py list            # what each suite runs and when

A pass on a clean checkout is recorded against the commit's tree, so the
pre-push hook (.githooks/pre-push, copied into git's hooks directory by
install) lets that commit through without running
anything again. Suites that need hardware this machine lacks (macOS, iOS, a
Windows install when run elsewhere) are printed as not covered; the pass says
so instead of claiming them.
"""
from __future__ import annotations

import argparse
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
BASE = "origin/main"
# Edge publication waits for this commit status on the exact main revision
# (scripts/ci/edge_publication_gate.py).
ATTEST_CONTEXT = "local/full-suite"
# A full pass on Windows also posts this; a stable release waits for it
# (.fluid/skills/ship-release). Edge does not, so DesktopWorker being off never
# blocks Linux and Docker work.
WINDOWS_CONTEXT = "local/windows-suite"
MAP_PATH = Path(os.environ.get("LOCAL_CI_TEST_MAP")
                or Path.home() / ".cache" / "ai-shuffle" / "backend-test-map.json.gz")
# The shared worker hosts run other sessions: never more than two test workers.
WORKERS = os.environ.get("LOCAL_CI_WORKERS", "2")

# The CI Backend fast subset; scripts/ci/backend_test_map.py keeps the same list.
FAST_SUBSET = [
    "tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract",
    "tests/performance", "tests/mcp", "tests/services", "tests/unit", "tests/scripts",
]
FAST_SUBSET_EXCLUDED = [
    "tests/core/test_chat_shell.py",
    "tests/core/test_container_runtime.py",
    "tests/core/test_container_image_contract.py",
]
BACKEND_PATHS = ["app/*", "tests/*", "fluid/*", "scripts/*", "docker/*", "Dockerfile",
                 "requirements*", "pytest.ini", "run_local.*"]


class Failure(Exception):
    pass


@dataclass
class Context:
    mode: str                      # "changed" or "full"
    base: str                      # merge base with origin/main
    files: list[str] | None        # changed paths; None in full mode
    python: str
    log: Callable[[str], None] = print
    notes: list[str] = field(default_factory=list)

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


def run(argv: list[str], cwd: Path = ROOT, env: dict | None = None) -> None:
    exe = shutil.which(argv[0]) or argv[0]
    shown = " ".join(argv)
    rel = cwd.relative_to(ROOT).as_posix() if cwd != ROOT else "."
    print(f"\n$ ({rel}) {shown}", flush=True)
    merged = {**os.environ, **(env or {})}
    result = subprocess.run([exe, *argv[1:]], cwd=cwd, env=merged, **low_priority_kwargs())
    if result.returncode:
        raise Failure(f"{shown} (in {rel}) exited {result.returncode}")


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
    return {"PYTHONPATH": os.pathsep.join(p for p in paths if p)}


def has_module(python: str, module: str) -> bool:
    return subprocess.run([python, "-c", f"import {module}"], capture_output=True).returncode == 0


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
    return args


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
    return select_by_name(ctx.files or [])


def select_by_name(files: list[str]) -> list[str] | str:
    if any(fnmatch(name, pattern) for name in files
           for pattern in ("requirements*", "pytest.ini", "tests/conftest.py", "tests/*/conftest.py")):
        return "all"
    roots = [ROOT / d for d in FAST_SUBSET]
    tests = [p.relative_to(ROOT).as_posix() for d in roots for p in d.rglob("test_*.py")]
    tests += [p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py")]
    selected = {name for name in files if name in tests}
    for name in files:
        if not name.endswith(".py") or name.startswith("tests/"):
            continue
        module = name[:-3].replace("/", ".")
        if module.startswith("fluid.fluid."):
            module = module[len("fluid."):]
        stem = module.rsplit(".", 1)[-1]
        parent = module.rsplit(".", 1)[0] if "." in module else ""
        pattern = re.compile(rf"\b{re.escape(module)}\b|from {re.escape(parent)} import [^\n]*\b{re.escape(stem)}\b")
        for test in tests:
            try:
                if pattern.search((ROOT / test).read_text(encoding="utf-8", errors="ignore")):
                    selected.add(test)
            except OSError:
                continue
    return sorted(t for t in selected if t not in FAST_SUBSET_EXCLUDED and (ROOT / t).is_file())


# --- suites ----------------------------------------------------------------

def backend(ctx: Context) -> None:
    env = python_env()
    args = pytest_args(ctx.python)
    if ctx.changed(["app/*", "scripts/scan_blocking_db.py", "tests/test_scan_blocking_db.py"]):
        run([ctx.python, "-m", "pytest", "tests/test_scan_blocking_db.py", *args], env=env)
        run([ctx.python, "scripts/scan_blocking_db.py"], env=env)
    selection = "all" if ctx.mode == "full" else select_backend_tests(ctx)
    if selection == "all":
        targets = [*FAST_SUBSET, *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py"))]
        extra = ["--deselect=tests/test_scan_blocking_db.py", *[f"--ignore={p}" for p in FAST_SUBSET_EXCLUDED]]
    else:
        targets, extra = selection, []
    if not targets:
        print("No backend tests reach this change.")
        return
    print(f"Backend: {'the whole fast subset' if selection == 'all' else f'{len(targets)} test files'}")
    hits = None
    if ctx.mode == "full" and sys.version_info >= (3, 12):
        hits = Path(tempfile.mkdtemp(prefix="local-ci-map-"))
        env = {**env, "BACKEND_TEST_MAP_DIR": str(hits)}
        extra = [*extra, "-p", "tests._test_map"]
    run([ctx.python, "-m", "pytest", *targets, *extra, *args], env=env)
    if hits is not None:
        MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        run([ctx.python, "scripts/ci/backend_test_map.py", "merge", "--hits", str(hits),
             "--out", str(MAP_PATH), "--commit", git("rev-parse", "HEAD")], env=env)
        shutil.rmtree(hits, ignore_errors=True)


def backend_linux(ctx: Context) -> None:
    """Steps CI Backend ran on Linux with real PostgreSQL and bubblewrap."""
    env = python_env()
    args = pytest_args(ctx.python, timeout=240)
    ran = False
    if shutil.which("bwrap"):
        run([ctx.python, "-m", "pytest", "tests/core/test_chat_shell.py", "-rs", *args],
            env={**env, "CHAT_SHELL_REQUIRE_SANDBOX": "1"})
        ran = True
    else:
        ctx.notes.append("Isolated folder shell tests not run: bubblewrap (bwrap) is not installed here.")
    pg_ctl = shutil.which("pg_ctl")
    pg_bin = os.environ.get("SMOKE_PG_BIN") or (str(Path(pg_ctl).parent) if pg_ctl else "")
    if pg_bin:
        run([ctx.python, "-m", "pytest", "tests/smoke", "tests/db/test_schema_lifecycle.py", "-m", "boot_smoke",
             "-rs", *pytest_args(ctx.python, timeout=240, parallel=False)],
            env={**env, "BOOT_SMOKE_REQUIRED": "1", "SMOKE_PG_BIN": pg_bin})
        ran = True
    else:
        ctx.notes.append("Boot smoke test not run: no PostgreSQL binaries (set SMOKE_PG_BIN).")
    if not ran:
        ctx.notes.append("Linux-only backend steps were not run on this machine.")


def fluid(ctx: Context) -> None:
    run([ctx.python, "-m", "pytest", "fluid/tests/unit", *pytest_args(ctx.python)], env=python_env())


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
    run(["node", "scripts/render-docs.mjs", "--check"], cwd=npm_ready("android/site"))


def desktop(ctx: Context) -> None:
    node_package("desktop", "typecheck", "test")(ctx)


def hub(ctx: Context) -> None:
    node_package("android/hub", "typecheck", "test")(ctx)
    run([ctx.python, "android/ci/test-cloudflare-deploy.py"])


def site(ctx: Context) -> None:
    node_package("android/site", "typecheck", "test")(ctx)
    run(["node", "--test", "app/src/test/js/chat-downloads.test.cjs"], cwd=ROOT / "android")


def workflows(ctx: Context) -> None:
    for module in ("yaml", "pytest"):
        if not has_module(ctx.python, module):
            raise Failure(f"{ctx.python} needs {module} (PyYAML, pytest) for the workflow policy checks")
    run([ctx.python, "scripts/ci/test-artifact-policy.py"])
    run([ctx.python, "scripts/ci/test-cost-policy.py"])
    run([ctx.python, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "--confcutdir=tests/scripts",
         "tests/scripts/test_owner_publication_policy.py", "tests/scripts/test_edge_publication_gate.py",
         "tests/scripts/test_local_ci.py"])


def android_play(ctx: Context) -> None:
    for script in ("test-version-name.py", "test-mobile-apk.py"):
        run([ctx.python, f"android/ci/{script}"])
    # The workflow test parses each inline step with /bin/bash -n.
    if Path("/bin/bash").exists():
        run([ctx.python, "android/ci/test-play-workflow.py"])
    else:
        ctx.notes.append("Play workflow shell checks not run: they need /bin/bash (a Linux worker).")


def update_reach(ctx: Context) -> None:
    labels = ""
    if shutil.which("gh"):
        # The owner approves an unreachable change with a PR label; reading it costs nothing.
        result = subprocess.run(["gh", "pr", "view", "--json", "labels", "--jq", '[.labels[].name] | join(",")'],
                                cwd=ROOT, capture_output=True, text=True)
        labels = result.stdout.strip() if result.returncode == 0 else ""
    run([ctx.python, "scripts/ci/update_reach.py", "--base", ctx.base, "--head", "HEAD", "--labels", labels])


def android_app(ctx: Context) -> None:
    gradlew = "gradlew.bat" if os.name == "nt" else "./gradlew"
    run([str(ROOT / "android" / gradlew), "testDebugUnitTest", "--max-workers=2"], cwd=ROOT / "android")


def android_unavailable() -> str | None:
    if not (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")):
        return "no Android SDK (ANDROID_HOME) on this machine"
    return None


def linux_only() -> str | None:
    return None if sys.platform.startswith("linux") else "Linux only (bubblewrap, PostgreSQL boot smoke)"


SUITES = [
    Suite("backend", BACKEND_PATHS, backend, "Backend pytest: the files a change reaches (full: the whole fast subset)"),
    Suite("backend-linux", ["app/core/chat_shell.py", "app/core/confined_files.py", "app/db/*", "app/main.py",
                            "docker/*", "tests/smoke/*", "tests/core/test_chat_shell.py"],
          backend_linux, "Sandbox shell and real-PostgreSQL boot smoke", linux_only),
    Suite("fluid", ["fluid/*"], fluid, "fluid unit suite"),
    Suite("frontend", ["frontend/*", "app/contract/schema/*"], frontend,
          "svelte-check, Vitest for the files a change reaches, contract check (full: build and every test)"),
    Suite("mobile", ["mobile/*", "frontend/src/*", "frontend/package*.json"], mobile, "Mobile UI type check and tests"),
    Suite("tools", ["tools/*", "tests/core/network/vectors.json"], tools, "AI tools package and network vectors"),
    Suite("browser-mcp", ["browser-mcp/*"], browser_mcp, "Browser MCP build and tests"),
    Suite("docs", ["docs/content/*", "frontend/src/lib/docs/content/*", "android/site/scripts/render-docs.mjs",
                   "android/site/src/content/docs.json"], docs, "Docs copies match and the website docs render"),
    Suite("desktop", ["desktop/*"], desktop, "Desktop typecheck and unit tests"),
    Suite("hub", ["android/hub/*", "android/ci/test-cloudflare-deploy.py", ".github/workflows/deploy-*.yml"], hub,
          "Account hub Worker tests and deploy guard"),
    Suite("site", ["android/site/*", "android/app/src/test/js/*"], site, "Website Worker tests"),
    Suite("license", ["android/license/*"], node_package("android/license", "typecheck", "test"), "License server tests"),
    Suite("registry", ["android/registry/*"], node_package("android/registry", "typecheck", "test"), "Registry tests"),
    Suite("uptime", ["android/uptime/*"], node_package("android/uptime", "typecheck", "test"), "Uptime monitor tests"),
    Suite("android-play", ["android/ci/*", ".github/workflows/android-play*.yml", ".github/workflows/release.yml"],
          android_play, "Play workflow safety checks"),
    Suite("android-app", ["android/app/*", "android/build.gradle*", "android/settings.gradle*", "android/gradle*"],
          android_app, "Android unit tests (Gradle)", android_unavailable),
    Suite("workflows", [".github/workflows/*", "scripts/ci/*", "tests/scripts/test_owner_publication_policy.py",
                        "tests/scripts/test_edge_publication_gate.py", "tests/scripts/test_local_ci.py", "scripts/local_ci.py",
                        ".githooks/*"], workflows, "Workflow and publication policy tests"),
    Suite("update-reach", ["Dockerfile", "entrypoint.sh", "docker/*", "requirements*", "desktop/src/main/*",
                           "desktop/scripts/*", "desktop/build/*", "desktop/electron-builder.yml",
                           "scripts/source_archive.py", "scripts/ci/update_reach.py"],
          update_reach, "Can installed apps take this change through the in-app update?"),
]

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
     "Windows: not tested on this machine. A stable release waits for `python scripts/local_ci.py full --attest` "
     "on a Windows worker (DesktopWorker); edge does not."),
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

def choose_python() -> str:
    return os.environ.get("LOCAL_CI_PYTHON") or sys.executable


def changed_files(base: str) -> list[str]:
    names = set(git("diff", "--name-only", f"{base}...HEAD").splitlines())
    names |= set(git("diff", "--name-only", "HEAD").splitlines())
    names |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(n for n in names if n)


def run_suites(mode: str, only: list[str] | None, python: str) -> tuple[list[str], list[str], list[str]]:
    if mode == "changed":
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, check=False)
    base = git("merge-base", BASE, "HEAD")
    files = None if mode == "full" else changed_files(base)
    ctx = Context(mode=mode, base=base, files=files, python=python)
    if files is not None:
        print(f"{len(files)} files differ from {BASE}.")
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


def attest(state: str, description: str, revision: str) -> None:
    contexts = [ATTEST_CONTEXT] + ([WINDOWS_CONTEXT] if os.name == "nt" else [])
    for context in contexts:
        subprocess.run(["gh", "api", f"repos/{REPOSITORY}/statuses/{revision}", "-f", f"state={state}",
                        "-f", f"context={context}", "-f", f"description={description[:139]}"],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def cmd_check(args) -> int:
    mode = "full" if args.command == "full" else "changed"
    python = choose_python()
    revision = git("rev-parse", "HEAD")
    if args.attest:
        if mode != "full":
            raise Failure("--attest needs `full`")
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, check=True)
        if not clean_checkout() or revision != git("rev-parse", BASE):
            raise Failure("--attest runs only on a clean checkout of the current origin/main")
        attest("pending", f"Full suite running on {machine()}", revision)
    clean = clean_checkout()
    try:
        ran, skipped, _ = run_suites(mode, args.only, python)
    except Failure as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        if args.attest:
            attest("failure", f"Failed on {machine()}: {error}", revision)
        return 1
    if clean and not args.only:
        save_record(tree_of("HEAD"), {"mode": mode, "suites": ran, "not_covered": skipped,
                                      "machine": machine(), "commit": revision, "at": time.time()})
    elif not clean:
        print("Uncommitted changes: this pass is not recorded. Commit, then the pre-push hook re-runs it.")
    if args.attest:
        attest("success", f"{', '.join(ran)} passed on {machine()}", revision)
        print(f"Posted {ATTEST_CONTEXT} on {revision[:9]}; edge publication can proceed.")
    return 0


def cmd_pre_push(args) -> int:
    """Called by the pre-push hook with git's ref lines on stdin."""
    zero = "0" * 40
    records = load_records()
    head = git("rev-parse", "HEAD")
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, remote_ref, _ = parts
        if local_sha == zero or not remote_ref.startswith("refs/heads/"):
            continue  # deletions and tags
        if tree_of(local_sha) in records:
            continue
        if local_sha != head or not clean_checkout():
            print(f"pre-push: {local_sha[:9]} has no local check pass. Check out that commit with a clean "
                  "tree and run `python scripts/local_ci.py`.", file=sys.stderr)
            return 1
        print(f"pre-push: running local checks for {local_sha[:9]} before it leaves this machine.")
        args.command, args.attest, args.only = "check", False, None
        if cmd_check(args):
            print("pre-push: checks failed; nothing was pushed.", file=sys.stderr)
            return 1
    return 0


HOOK_SOURCE = ROOT / ".githooks" / "pre-push"
HOOK_MARKER = "# LOCAL-CI-PRE-PUSH"


def hooks_dir() -> Path:
    """The directory git runs hooks from, honouring core.hooksPath.

    AI Shuffle points core.hooksPath at the main clone's .git/hooks and keeps
    its own managed hooks there, so install adds a file to that directory
    instead of redirecting core.hooksPath and switching those hooks off.
    """
    path = Path(git("rev-parse", "--git-path", "hooks"))
    return path if path.is_absolute() else (ROOT / path).resolve()


def install_hook(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target, kept = directory / "pre-push", directory / "pre-push.user"
    if target.exists() and HOOK_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        if kept.exists():
            raise Failure(f"{target} is someone else's hook and {kept.name} already exists; merge them by hand.")
        target.replace(kept)
    target.write_bytes(HOOK_SOURCE.read_bytes().replace(b"\r\n", b"\n"))
    target.chmod(0o755)
    return f"Pre-push checks are on: {target}" + (f" (the earlier hook still runs as {kept.name})" if kept.exists() else "")


def cmd_install(args) -> int:
    print(install_hook(hooks_dir()))
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
    parser.add_argument("command", nargs="?", default="check", choices=["check", "full", "install", "list", "pre-push"])
    parser.add_argument("--only", nargs="+", metavar="SUITE", help="run just these suites (not recorded as a pass)")
    parser.add_argument("--attest", action="store_true", help=f"with full on main: post {ATTEST_CONTEXT}")
    args = parser.parse_args(argv)
    if args.only:
        unknown = set(args.only) - {s.name for s in SUITES}
        if unknown:
            parser.error(f"unknown suite: {', '.join(sorted(unknown))}")
    handlers = {"check": cmd_check, "full": cmd_check, "install": cmd_install, "list": cmd_list,
                "pre-push": cmd_pre_push}
    try:
        return handlers[args.command](args)
    except Failure as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
