#!/usr/bin/env python3
"""
AI Hub Local Mode Launcher

Cross-platform launcher script for running AI Hub on your local PC.
This script handles:
- Virtual environment setup
- Dependency installation
- Prerequisite checks (Node.js, GitHub CLI)
- Frontend building (if needed)
- Starting the server

Usage:
    python run_local.py [--setup] [--no-browser] [--port PORT]

Options:
    --setup      Run first-time setup (install dependencies, build frontend)
    --no-browser Don't open browser after starting
    --port PORT  Use a specific port (default: 8000)
    --host HOST  Use a specific host (default: 127.0.0.1 for local mode)
"""

import os
import sys
import subprocess
import argparse
import webbrowser
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path


# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    END = '\033[0m'

    @classmethod
    def disable(cls):
        """Disable colors for non-TTY outputs"""
        cls.GREEN = cls.YELLOW = cls.RED = cls.BLUE = cls.BOLD = cls.END = ''


# Disable colors on Windows cmd.exe or non-TTY
if sys.platform == 'win32' or not sys.stdout.isatty():
    try:
        # Try to enable ANSI on Windows 10+
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        Colors.disable()


def print_banner():
    """Print the AI Hub banner"""
    print(f"""
{Colors.BLUE}{Colors.BOLD}
    _    ___   _   _ _   _ ____
   / \\  |_ _| | | | | | | | __ )
  / _ \\  | |  | |_| | | | |  _ \\
 / ___ \\ | |  |  _  | |_| | |_) |
/_/   \\_\\___| |_| |_|\\___/|____/
{Colors.END}
{Colors.GREEN}Local Mode Launcher{Colors.END}
""")


def print_status(message: str, status: str = "info"):
    """Print a status message with color"""
    icons = {
        "info": f"{Colors.BLUE}ℹ{Colors.END}",
        "success": f"{Colors.GREEN}✓{Colors.END}",
        "warning": f"{Colors.YELLOW}⚠{Colors.END}",
        "error": f"{Colors.RED}✗{Colors.END}",
    }
    print(f" {icons.get(status, icons['info'])} {message}")


def check_python_version():
    """Ensure Python 3.10+ is being used"""
    if sys.version_info < (3, 10):
        print_status(f"Python 3.10+ required (you have {sys.version})", "error")
        sys.exit(1)
    print_status(f"Python {sys.version_info.major}.{sys.version_info.minor} detected", "success")


def check_prerequisite(name: str, cmd: str, install_hint: str) -> bool:
    """Check if a prerequisite is installed"""
    path = shutil.which(cmd)
    if path:
        print_status(f"{name} found at {path}", "success")
        return True
    else:
        print_status(f"{name} not found", "warning")
        print_status(f"  Install: {install_hint}", "info")
        return False


def install_github_cli() -> bool:
    """Attempt to automatically install GitHub CLI"""
    print_status("Attempting to install GitHub CLI...", "info")

    try:
        if sys.platform == 'darwin':
            # macOS - try Homebrew
            if shutil.which("brew"):
                print_status("Installing via Homebrew...", "info")
                result = subprocess.run(
                    ["brew", "install", "gh"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and shutil.which("gh"):
                    print_status("GitHub CLI installed via Homebrew", "success")
                    return True

        elif sys.platform == 'win32':
            # Windows - try winget, then scoop
            if shutil.which("winget"):
                print_status("Installing via winget...", "info")
                result = subprocess.run(
                    ["winget", "install", "--id", "GitHub.cli", "--accept-source-agreements", "--accept-package-agreements"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print_status("GitHub CLI installed via winget", "success")
                    print_status("  You may need to restart your terminal for 'gh' to be on PATH", "info")
                    return True
            if shutil.which("scoop"):
                print_status("Installing via Scoop...", "info")
                result = subprocess.run(
                    ["scoop", "install", "gh"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print_status("GitHub CLI installed via Scoop", "success")
                    return True

        else:
            # Linux - try package managers in order of preference
            if shutil.which("apt-get") and os.geteuid() == 0:
                # Debian/Ubuntu (with root)
                print_status("Installing via apt...", "info")
                subprocess.run(
                    ["apt-get", "update", "-qq"],
                    capture_output=True, text=True
                )
                result = subprocess.run(
                    ["apt-get", "install", "-y", "-qq", "gh"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and shutil.which("gh"):
                    print_status("GitHub CLI installed via apt", "success")
                    return True

            if shutil.which("brew"):
                print_status("Installing via Homebrew...", "info")
                result = subprocess.run(
                    ["brew", "install", "gh"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and shutil.which("gh"):
                    print_status("GitHub CLI installed via Homebrew", "success")
                    return True

            if shutil.which("conda"):
                print_status("Installing via conda-forge...", "info")
                result = subprocess.run(
                    ["conda", "install", "-y", "-c", "conda-forge", "gh"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and shutil.which("gh"):
                    print_status("GitHub CLI installed via conda", "success")
                    return True

    except Exception as e:
        print_status(f"Auto-install failed: {e}", "warning")

    print_status("Could not auto-install GitHub CLI", "warning")
    print_status("  Please install manually from https://cli.github.com/", "info")
    return False


def check_prerequisites():
    """Check all prerequisites are installed"""
    print(f"\n{Colors.BOLD}Checking prerequisites...{Colors.END}")

    all_ok = True

    # Check Node.js (for building frontend)
    if not check_prerequisite(
        "Node.js",
        "node",
        "https://nodejs.org/ or use your package manager"
    ):
        print_status("  Node.js is needed to build the frontend", "info")

    # Check GitHub CLI (used for PR reviews, issue management, etc.)
    if not shutil.which("gh"):
        print_status("GitHub CLI not found", "warning")
        if not install_github_cli():
            all_ok = False
    else:
        print_status(f"GitHub CLI found at {shutil.which('gh')}", "success")

    return all_ok


def get_venv_python() -> Path:
    """Get the path to the virtual environment Python"""
    project_root = Path(__file__).parent
    if sys.platform == 'win32':
        return project_root / "venv" / "Scripts" / "python.exe"
    return project_root / "venv" / "bin" / "python"


def get_venv_pip() -> Path:
    """Get the path to the virtual environment pip"""
    project_root = Path(__file__).parent
    if sys.platform == 'win32':
        return project_root / "venv" / "Scripts" / "pip.exe"
    return project_root / "venv" / "bin" / "pip"


def setup_venv():
    """Create virtual environment if it doesn't exist"""
    project_root = Path(__file__).parent
    venv_path = project_root / "venv"

    if venv_path.exists() and get_venv_python().exists():
        print_status("Virtual environment already exists", "success")
        return True

    print_status("Creating virtual environment...", "info")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        print_status("Virtual environment created", "success")
        return True
    except subprocess.CalledProcessError as e:
        print_status(f"Failed to create virtual environment: {e}", "error")
        return False


def install_dependencies():
    """Install Python dependencies"""
    print_status("Installing Python dependencies...", "info")

    project_root = Path(__file__).parent
    requirements = project_root / "requirements.txt"

    if not requirements.exists():
        print_status("requirements.txt not found", "error")
        return False

    try:
        subprocess.run([
            str(get_venv_pip()), "install", "-r", str(requirements)
        ], check=True)
        print_status("Dependencies installed", "success")
    except subprocess.CalledProcessError as e:
        print_status(f"Failed to install dependencies: {e}", "error")
        return False

    # Native-only deps (computer-use). Best-effort: a failure here (e.g. evdev build
    # on a Linux host without kernel headers) just disables computer-use, it should
    # not block startup.
    native_requirements = project_root / "requirements-native.txt"
    if native_requirements.exists():
        try:
            subprocess.run([
                str(get_venv_pip()), "install", "-r", str(native_requirements)
            ], check=True)
            print_status("Native (computer-use) dependencies installed", "success")
        except subprocess.CalledProcessError as e:
            print_status(
                f"Native (computer-use) dependencies skipped: {e}. "
                "Computer-use will be unavailable.",
                "warning",
            )

    return True


def build_frontend():
    """Build the Svelte frontend"""
    project_root = Path(__file__).parent
    frontend_dir = project_root / "frontend"
    static_dir = project_root / "app" / "static"

    # Check if frontend needs building
    if static_dir.exists() and (static_dir / "index.html").exists():
        print_status("Frontend already built", "success")
        return True

    if not frontend_dir.exists():
        print_status("Frontend directory not found", "error")
        return False

    npm = shutil.which("npm")
    if not npm:
        print_status("npm not found - cannot build frontend", "error")
        print_status("Install Node.js from https://nodejs.org/", "info")
        return False

    print_status("Building frontend...", "info")

    try:
        # Install npm dependencies
        subprocess.run([npm, "install"], cwd=str(frontend_dir), check=True)

        # Build the frontend
        subprocess.run([npm, "run", "build"], cwd=str(frontend_dir), check=True)

        # Copy build output to static directory
        build_dir = frontend_dir / "build"
        if build_dir.exists():
            if static_dir.exists():
                shutil.rmtree(static_dir)
            shutil.copytree(build_dir, static_dir)
            print_status("Frontend built and copied to app/static/", "success")
            return True
        else:
            print_status("Build output not found", "error")
            return False

    except (subprocess.CalledProcessError, OSError) as e:
        print_status(f"Failed to build frontend: {e}", "error")
        return False


def build_browser_mcp():
    """Build the bundled browser-mcp STDIO server (browser-automation tools).

    Mirrors build_frontend(): same npm discovery and status printing. The backend
    spawns ``node browser-mcp/dist/index.js`` per session to drive a local
    headless Chrome/Edge over CDP, so the package must be compiled once (tsc ->
    dist/). Skipped when dist/index.js already exists AND is newer than the
    newest file under browser-mcp/src/ (i.e. nothing changed since last build).
    """
    project_root = Path(__file__).parent
    mcp_dir = project_root / "browser-mcp"
    dist_entry = mcp_dir / "dist" / "index.js"
    src_dir = mcp_dir / "src"

    if not mcp_dir.exists():
        print_status("browser-mcp directory not found - skipping", "warning")
        return False

    # Up-to-date check: dist newer than the newest source file → nothing to do.
    if dist_entry.exists() and src_dir.exists():
        try:
            newest_src = max(
                (p.stat().st_mtime for p in src_dir.rglob("*") if p.is_file()),
                default=0,
            )
            if dist_entry.stat().st_mtime >= newest_src:
                print_status("browser-mcp already built (up to date)", "info")
                return True
        except OSError:
            pass  # fall through to a rebuild on any stat error

    npm = shutil.which("npm")
    if not npm:
        print_status("npm not found - cannot build browser-mcp", "error")
        print_status("Install Node.js from https://nodejs.org/", "info")
        return False

    print_status("Building browser-mcp...", "info")

    try:
        # --ignore-scripts: no postinstall browser download (attach-only server).
        subprocess.run(
            [npm, "ci", "--ignore-scripts"], cwd=str(mcp_dir), check=True
        )
        subprocess.run([npm, "run", "build"], cwd=str(mcp_dir), check=True)

        if dist_entry.exists():
            print_status("browser-mcp built (dist/index.js)", "success")
            return True
        print_status("browser-mcp build output not found", "error")
        return False

    except (subprocess.CalledProcessError, OSError) as e:
        print_status(f"Failed to build browser-mcp: {e}", "error")
        return False


def check_claude_auth():
    """Check if Claude is authenticated (credentials exist from in-app setup)"""
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        print_status("Claude credentials found", "success")
        return True
    else:
        print_status("Claude not authenticated", "warning")
        print_status("  You'll be guided through authentication on first launch", "info")
        return False


def run_setup():
    """Run the full setup process"""
    print(f"\n{Colors.BOLD}Running first-time setup...{Colors.END}\n")

    if not setup_venv():
        return False

    if not install_dependencies():
        return False

    # Browser automation uses the bundled browser-mcp server + a local Chrome or
    # Edge in headless mode, auto-launched by the backend over CDP. No Claude
    # account, browser extension, or Playwright Chromium download is involved.
    # Knobs: CHROME_EXECUTABLE (pick a specific binary), CHROME_CDP_PORT (default
    # 9221), START_CHROME=0 (opt out of auto-launch). See LOCAL_SETUP.md →
    # "Browser automation (native installs)".
    print_status(
        "Browser automation: bundled browser-mcp + auto-launched headless Chrome/Edge",
        "info",
    )

    if not build_frontend():
        print_status("Frontend build skipped - you can build it later", "warning")

    if not build_browser_mcp():
        print_status(
            "browser-mcp build skipped - browser tools unavailable until built", "warning"
        )

    check_claude_auth()

    print(f"\n{Colors.GREEN}Setup complete!{Colors.END}")
    return True


def _get_data_dir_for_supervisor(env: dict) -> Path:
    """Best-effort mirror of ``app.core.platform.get_app_data_dir()``.

    run_local.py launches the child under the project ``venv``, but this
    launcher process itself runs on the system Python (no pydantic/app
    imports available), so the app's real data-dir resolver can't be
    called directly. Docker deployments don't go through this launcher at
    all (entrypoint.sh runs uvicorn directly there), so only the native
    branches need mirroring. ``DATA_DIR`` overrides both, matching
    ``Settings.data_dir``.
    """
    override = env.get("DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        userprofile = env.get("USERPROFILE")
        base = Path(userprofile) if userprofile else Path.home()
        return base / ".ai-shuffle"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ai-shuffle"
    return Path.home() / ".local" / "share" / "ai-shuffle"


def _get_instance_id_for_supervisor(env: dict, data_dir: Path, port: int) -> str:
    """Best-effort mirror of ``Settings.effective_instance_id`` for log placement.

    Priority matches app/core/config.py: an explicit ``INSTANCE_ID`` env var,
    then the ``.instance_id`` file the app itself persists into its data dir,
    then a port-scoped fallback so concurrent debug instances at least don't
    collide with each other.
    """
    explicit = env.get("INSTANCE_ID")
    if explicit and explicit.strip():
        return explicit.strip()
    id_file = data_dir / ".instance_id"
    try:
        if id_file.exists():
            stored = id_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        pass
    return f"local-{port}"


def get_supervisor_log_path(env: dict, port: int) -> Path:
    """Where this launcher records child exit events, alongside the app's own logs.

    Reuses the same ``<data_dir>/logs/<instance_id>/`` directory
    ``app.core.file_logging`` writes ``app.log``/``fatal.log`` into, so a
    post-mortem only has one directory to check regardless of whether the
    app itself got to log anything before dying.
    """
    data_dir = _get_data_dir_for_supervisor(env)
    instance_id = _get_instance_id_for_supervisor(env, data_dir, port)
    return data_dir / "logs" / instance_id / "supervisor.log"


def format_child_exit_line(exit_code, started_at: str, ended_at: str, uptime_s: float) -> str:
    """Render the exit-notice line shared by the console and supervisor.log.

    A zero exit is a clean stop. Anything else — including a negative code
    (POSIX: killed by signal ``-N``) or a huge unsigned value (Windows:
    access violation / TerminateProcess) — is flagged as an unexpected death
    so a scan of supervisor.log finds it without cross-referencing app.log
    (issue #587: the app process died overnight with nothing in app.log at
    all, so the exit record has to live independently of the app's own logs).
    """
    if exit_code == 0:
        return (
            f"{ended_at} child exited cleanly (exit=0), started {started_at}, "
            f"uptime={uptime_s:.1f}s"
        )
    return (
        f"{ended_at} child died unexpectedly (exit={exit_code}), "
        f"started {started_at}, uptime={uptime_s:.1f}s — no auto-respawn "
        f"configured; this launcher is exiting"
    )


def log_child_exit(env: dict, port: int, exit_code, started_at: str, ended_at: str,
                    uptime_s: float) -> str:
    """Print + durably persist the child's exit so it survives a closed terminal.

    This runs *after* ``subprocess.run()`` returns — the child has already
    exited by then, so it cannot itself catch a silent external kill
    (SIGKILL / OOM-kill / TerminateProcess never leave anything for the
    victim process to observe). What it fixes is the OTHER half of #587:
    even when nothing could be caught, the fact and timestamp of the death
    must not be lost the moment the launching terminal is closed or the
    machine goes back to sleep. Writing to disk is best-effort — a failure
    here must never mask the real exit status, so it only warns.
    """
    line = format_child_exit_line(exit_code, started_at, ended_at, uptime_s)
    print_status(line, "success" if exit_code == 0 else "error")

    try:
        log_path = get_supervisor_log_path(env, port)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print_status(f"Could not write supervisor.log ({e}) — see console only", "warning")

    return line


def start_server(host: str, port: int, open_browser: bool):
    """Start the AI Hub server"""
    print(f"\n{Colors.BOLD}Starting AI Hub...{Colors.END}")

    # Set environment variables for local mode
    env = os.environ.copy()
    env["LOCAL_MODE"] = "true"
    env["HOST"] = host
    env["PORT"] = str(port)
    env["COOKIE_SECURE"] = "false"  # Allow HTTP for local development

    project_root = Path(__file__).parent
    venv_python = get_venv_python()

    if not venv_python.exists():
        print_status("Virtual environment not found. Run with --setup first.", "error")
        sys.exit(1)

    # Check if static files exist
    static_dir = project_root / "app" / "static"
    if not static_dir.exists() or not (static_dir / "index.html").exists():
        print_status("Frontend not built. Run with --setup first.", "error")
        sys.exit(1)

    url = f"http://{host}:{port}"
    print_status(f"Server starting at {url}", "info")
    print_status("Press Ctrl+C to stop", "info")

    # Open browser after a short delay
    if open_browser:
        def open_browser_delayed():
            time.sleep(2)
            webbrowser.open(url)

        import threading
        threading.Thread(target=open_browser_delayed, daemon=True).start()

    started_at = datetime.now(timezone.utc).isoformat()
    start_monotonic = time.monotonic()
    try:
        result = subprocess.run([
            str(venv_python), "-m", "app.main"
        ], cwd=str(project_root), env=env)
        # Child has exited (whether cleanly or not) — log the exit code and
        # timestamp BEFORE doing anything else, so if a respawn loop is ever
        # added here later it can never race ahead of this record. There is
        # deliberately no auto-respawn today (see DO-NOT-add-heavyweight-
        # monitoring note in issue #587) — this is diagnostic instrumentation,
        # not resilience.
        log_child_exit(
            env, port, result.returncode, started_at,
            datetime.now(timezone.utc).isoformat(),
            time.monotonic() - start_monotonic,
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.GREEN}Server stopped.{Colors.END}")


def main():
    parser = argparse.ArgumentParser(
        description="AI Hub Local Mode Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_local.py --setup         First-time setup
  python run_local.py                 Start the server
  python run_local.py --port 3000     Start on a different port
  python run_local.py --no-browser    Start without opening browser
        """
    )
    parser.add_argument("--setup", action="store_true", help="Run first-time setup")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--check", action="store_true", help="Check prerequisites only")

    args = parser.parse_args()

    print_banner()
    check_python_version()

    if args.check:
        check_prerequisites()
        check_claude_auth()
        sys.exit(0)

    if args.setup:
        check_prerequisites()
        if not run_setup():
            sys.exit(1)
        print(f"\n{Colors.BOLD}Ready to start!{Colors.END}")
        print("Run: python run_local.py")
        sys.exit(0)

    # Normal startup
    start_server(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
