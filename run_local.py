#!/usr/bin/env python3
"""
AI Shuffle Local Mode Launcher

Cross-platform launcher script for running AI Shuffle on your local PC.
This script handles:
- Virtual environment setup
- Dependency installation
- Prerequisite checks (Node.js, GitHub CLI)
- Frontend building (if needed)
- Starting the server

Usage:
    python run_local.py [--setup] [--no-browser] [--port PORT]
    python run_local.py --pair [--pair-url URL]

Options:
    --setup      Run first-time setup (install dependencies, build frontend)
    --no-browser Don't open browser after starting
    --port PORT  Use a specific port (default: 8000)
    --host HOST  Use a specific host (default: 127.0.0.1 for local mode)
    --pair       Create a five-minute device pairing code and exit
    --pair-url   Reachable URL to encode in the pairing QR
    --join CODE  Approve this configured runner with a one-time enrollment code
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
    """Print the AI Shuffle banner"""
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
    """Match the required vendored Fluid runtime."""
    if sys.version_info < (3, 12):
        print_status(f"Python 3.12+ required (you have {sys.version})", "error")
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


def check_prerequisites():
    """Report prerequisites without installing software or changing the host."""
    ok = check_node_version()
    check_prerequisite("GitHub CLI (optional, for GitHub features)", "gh", "https://cli.github.com/")
    from app.core.cloudflared import find_executable
    if not find_executable():
        print_status("Remote access client missing; setup or normal startup will install cloudflared.", "warning")
    return ok


def setup_remote_access():
    """Optional remote support must not prevent offline/local use of Shuffle."""
    from app.core.cloudflared import find_executable, install
    if find_executable():
        return True
    print_status("Installing cloudflared for optional remote access...", "info")
    try:
        install()
    except Exception as error:
        print_status(f"Remote access client could not be installed: {error}", "warning")
        print_status("Retry with: python -m app.core.cloudflared", "info")
        return False
    print_status("Remote access client installed", "success")
    return True


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
        result = subprocess.run([
            str(get_venv_python()), "-c",
            "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)",
        ], check=False)
        if result.returncode:
            print_status("Existing venv requires Python 3.12+. Recreate it with a supported Python.", "error")
            return False
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

    fluid_dir = project_root / "fluid"
    if not requirements.exists() or not (fluid_dir / "pyproject.toml").exists():
        print_status("Incomplete installation: requirements.txt and fluid/pyproject.toml are required", "error")
        return False

    try:
        subprocess.run([
            str(get_venv_python()), "-m", "pip", "install", "--upgrade", "pip==26.2.1"
        ], check=True)
        subprocess.run([
            str(get_venv_python()), "-m", "pip", "install", "-r", str(requirements), str(fluid_dir)
        ], check=True)
        subprocess.run([str(get_venv_python()), "-c", "import fluid"], check=True)
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

    if not frontend_dir.exists():
        if (static_dir / "index.html").is_file():
            print_status("Using the pre-built release frontend", "success")
            return True
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
        subprocess.run([npm, "ci"], cwd=str(frontend_dir), check=True)

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


def check_node_version() -> bool:
    """Vite 7 supports Node 20.19+ or 22.12+ (and newer even releases)."""
    node = shutil.which("node")
    if not node:
        print_status("Node.js 22.12+ (or 20.19+) is required for setup/update", "error")
        return False
    try:
        version = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
        major, minor, *_ = (int(part) for part in version.split("."))
        supported = (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major > 22
        if supported:
            return True
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    print_status("Node.js 22.12+ (or 20.19+) is required; update Node before setup", "error")
    return False


def build_node_package(name: str) -> bool:
    """Reinstall the lockfile and rebuild so source AND dependency updates take effect."""
    package_dir = Path(__file__).parent / name
    npm = shutil.which("npm")
    if not npm or not (package_dir / "package-lock.json").is_file():
        print_status(f"Cannot build {name}: npm and the package lockfile are required", "error")
        return False
    try:
        subprocess.run([npm, "ci", "--ignore-scripts"], cwd=package_dir, check=True)
        subprocess.run([npm, "run", "build"], cwd=package_dir, check=True)
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print_status(f"Failed to build {name}: {exc}", "error")
        return False


def build_browser_mcp():
    return build_node_package("browser-mcp")


def require_managed_database(env: dict) -> bool:
    """Source workers need a descriptor from a running supported supervisor."""
    python = get_venv_python()
    ready = False
    if python.exists():
        # Preserve non-DB .env settings (especially DATA_DIR) and config-file
        # indirection by using the worker's real resolver, not a second parser.
        try:
            result = subprocess.run(
                [str(python), "-c", "from app.core.config import settings; settings.get_database_connection()"],
                cwd=str(Path(__file__).parent), env=dict(env, LOCAL_MODE="true"),
                capture_output=True, timeout=15,
            )
            ready = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        descriptor = _get_data_dir_for_supervisor(env) / "managed-postgres.json"
        ready = descriptor.is_file() and not descriptor.is_symlink()
    if not ready:
        print_status(
            "Managed PostgreSQL is not ready. Start the packaged desktop launcher "
            "or the container supervisor; standalone source startup does not "
            "configure or connect to an external database.", "error"
        )
        return False
    # The worker performs strict descriptor validation before any DB connection.
    return True


def check_claude_cli():
    """Check that the stock Claude Code CLI is on PATH.

    Claude sessions run the ``claude`` binary installed by Anthropic's own
    installer. The app never installs, bundles or falls back to another copy.
    """
    found = shutil.which("claude")
    if found:
        print_status(f"Claude Code CLI found: {found}", "success")
        return True
    print_status("Claude Code CLI not found", "warning")
    print_status("  Install it with Anthropic's installer, then run `claude auth login`:", "info")
    if sys.platform == "win32":
        print_status("    irm https://claude.ai/install.ps1 | iex   (PowerShell)", "info")
    else:
        print_status("    curl -fsSL https://claude.ai/install.sh | bash", "info")
    return False


def check_claude_auth():
    """Ask the Claude Code CLI whether it is signed in (`claude auth status`)."""
    found = shutil.which("claude")
    if not found:
        return False
    try:
        result = subprocess.run([found, "auth", "status"], capture_output=True, text=True,
                                timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        print_status("Claude Code CLI is signed in", "success")
        return True
    print_status("Claude Code CLI is not signed in", "warning")
    print_status("  Run `claude auth login` in the AI Shuffle terminal (Settings > Providers > Claude Code CLI)", "info")
    print_status("  or any terminal on this machine, or add an Anthropic API key in Settings.", "info")
    print_status("  AI Shuffle never reads your Claude credentials.", "info")
    return False


def run_setup():
    """Run the full setup process"""
    print(f"\n{Colors.BOLD}Running first-time setup...{Colors.END}\n")

    if not check_node_version():
        return False

    if not setup_venv():
        return False

    if not install_dependencies():
        return False

    setup_remote_access()

    # Browser automation uses the bundled browser-mcp server + a local Chrome or
    # Edge in headless mode, always auto-launched by the backend over CDP. No
    # Claude account, browser extension, or Playwright Chromium download is
    # involved. Knobs: CHROME_EXECUTABLE (pick a specific binary),
    # CHROME_CDP_PORT (default 9221). See LOCAL_SETUP.md →
    # "Browser automation (native installs)".
    print_status(
        "Browser automation: bundled browser-mcp + auto-launched headless Chrome/Edge",
        "info",
    )

    if not build_frontend():
        return False
    if not build_browser_mcp() or not build_node_package("tools"):
        return False

    check_claude_cli()
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


def needs_first_run_setup() -> bool:
    """True when the venv or the built frontend that start_server needs is absent."""
    static_dir = Path(__file__).parent / "app" / "static"
    return not get_venv_python().exists() or not (static_dir / "index.html").exists()


def start_server(host: str, port: int, open_browser: bool):
    """Start the AI Shuffle server"""
    print(f"\n{Colors.BOLD}Starting AI Shuffle...{Colors.END}")

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

    if not require_managed_database(env):
        sys.exit(1)

    url = f"http://{host}:{port}"
    # Existing native installs also need dependencies added after their setup.
    setup_remote_access()
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


def run_pairing_command(url: str | None, join_code: str | None, port: int) -> int:
    """Run the pairing/enrollment CLI inside the project virtual environment."""
    project_root = Path(__file__).parent
    venv_python = get_venv_python()
    if not venv_python.exists():
        print_status("Virtual environment not found. Run with --setup first.", "error")
        return 1
    command = [str(venv_python), "-m", "app.cli_pair"]
    if join_code:
        command.extend(["--join", join_code])
    elif url:
        command.extend(["--url", url])
    env = os.environ.copy()
    env["LOCAL_MODE"] = "true"
    env["PORT"] = str(port)
    return subprocess.run(command, cwd=str(project_root), env=env).returncode


def main():
    parser = argparse.ArgumentParser(
        description="AI Shuffle Local Mode Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_local.py --setup         First-time setup
  python run_local.py                 Start the server
  python run_local.py --port 3000     Start on a different port
  python run_local.py --no-browser    Start without opening browser
  python run_local.py --pair          Create a one-time device pairing code
  python run_local.py --join CODE     Enroll this configured runner
        """
    )
    parser.add_argument("--setup", action="store_true", help="Install dependencies and rebuild assets (database managed by launcher)")
    parser.add_argument("--update", action="store_true", help="Update dependencies and rebuild assets")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--check", action="store_true", help="Check prerequisites only")
    parser.add_argument("--pair", action="store_true", help="Create a one-time device pairing code and exit")
    parser.add_argument("--pair-url", help="Reachable base URL to encode in the pairing QR")
    parser.add_argument("--join", metavar="CODE", help="Enroll this configured runner with a one-time code")

    args = parser.parse_args()

    print_banner()
    check_python_version()

    if args.check:
        ok = check_prerequisites()
        check_claude_cli()
        check_claude_auth()
        sys.exit(0 if ok else 1)

    if args.setup or args.update:
        if not run_setup():
            sys.exit(1)
        print(f"\n{Colors.BOLD}Ready to start!{Colors.END}")
        print("Start the packaged desktop launcher or container supervisor for managed PostgreSQL.")
        sys.exit(0)

    if args.pair or args.join:
        sys.exit(run_pairing_command(args.pair_url, args.join, args.port))

    if not require_managed_database(os.environ):
        sys.exit(1)

    # Double-clicking run_local.bat (or ./run_local.sh) on a fresh extract is the
    # documented first step, so a missing venv or frontend build runs setup here
    # rather than telling the user to come back with --setup.
    if needs_first_run_setup():
        print_status("First run detected: installing dependencies and building the frontend.", "info")
        if not run_setup():
            sys.exit(1)

    # Normal startup
    start_server(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    # Redirected Windows consoles can use a legacy encoding that cannot render
    # status icons. Logging must never prevent the backend from starting.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    main()
