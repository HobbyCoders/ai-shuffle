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


def _read_env_file(env_path: Path) -> dict:
    """Parse KEY=VALUE lines from .env (best-effort, comments preserved elsewhere)."""
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


def _write_env_values(env_path: Path, updates: dict, retire_keys: tuple = ()):
    """Update KEY=VALUE pairs in .env in place, appending keys that are new.

    Keys in ``retire_keys`` are commented out instead of kept — used to
    disarm the legacy DATABASE_URL (which takes precedence over the
    POSTGRES_* pieces) once setup has collected new values, so the freshly
    entered connection details actually take effect.
    """
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
        elif key in retire_keys:
            lines[i] = f"# retired by setup (superseded by POSTGRES_*): {line}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Database connection (written by run_local.py --setup)")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def configure_database():
    """Collect the PostgreSQL connection details and write them to .env.

    The app assembles its own connection URL from these pieces
    (POSTGRES_HOST/PORT/USER/PASSWORD/DB), so nobody has to hand-build a
    postgresql:// string.
    """
    print(f"\n{Colors.BOLD}Database configuration (PostgreSQL){Colors.END}")
    print_status("AI Shuffle stores its data in a PostgreSQL database.", "info")
    print_status("Enter the connection details below (press Enter to keep the default).", "info")

    project_root = Path(__file__).parent
    env_path = project_root / ".env"
    existing = _read_env_file(env_path)

    # A pre-split install carries a legacy DATABASE_URL instead of POSTGRES_*
    # values. Parse it so the prompts default to the connection the user
    # already had, instead of making them re-derive host/user/password.
    legacy_parts = {}
    legacy_url = existing.get("DATABASE_URL", "")
    if legacy_url:
        try:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(
                legacy_url if "://" in legacy_url else f"postgresql://{legacy_url}"
            )
            legacy_parts = {
                "host": parsed.hostname or "",
                "port": str(parsed.port) if parsed.port else "",
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "db": parsed.path.lstrip("/"),
            }
            print_status("Found a legacy DATABASE_URL — defaults below are taken from it", "info")
        except Exception:
            legacy_parts = {}

    def ask(prompt: str, default: str) -> str:
        try:
            answer = input(f"   {prompt} [{default}]: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    host = ask("Host / IP address",
               existing.get("POSTGRES_HOST") or legacy_parts.get("host") or "localhost")
    port = ask("Port",
               existing.get("POSTGRES_PORT") or legacy_parts.get("port") or "5432")
    user = ask("Username",
               existing.get("POSTGRES_USER") or legacy_parts.get("user") or "ai_shuffle")
    db_name = ask("Database name",
                  existing.get("POSTGRES_DB") or legacy_parts.get("db") or "ai_shuffle")

    stored_password = existing.get("POSTGRES_PASSWORD") or legacy_parts.get("password") or ""
    password = ""
    while not password:
        try:
            import getpass
            prompt = "   Password (input hidden{}): ".format(
                ", Enter keeps current" if stored_password else ""
            )
            password = getpass.getpass(prompt).strip()
        except EOFError:
            break
        if not password and stored_password:
            password = stored_password
            print_status("Keeping the existing password", "info")
            break
        if not password:
            print_status("A password is required", "warning")

    if not password:
        print_status("No password entered — set POSTGRES_PASSWORD in .env before starting", "warning")
        return False

    _write_env_values(env_path, {
        "POSTGRES_HOST": host,
        "POSTGRES_PORT": port,
        "POSTGRES_USER": user,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": db_name,
    }, retire_keys=("DATABASE_URL",))
    if existing.get("DATABASE_URL"):
        print_status("Legacy DATABASE_URL commented out — the POSTGRES_* values above now apply", "info")
    print_status(f"Database settings saved to {env_path}", "success")

    # Best-effort connectivity check via the venv's psycopg2 (installed with
    # the app requirements). A failure is a warning, not a setup blocker —
    # the database might simply not be running yet.
    venv_python = get_venv_python()
    if venv_python.exists():
        check = subprocess.run(
            [str(venv_python), "-c", (
                "import psycopg2,sys;"
                "psycopg2.connect(host=sys.argv[1],port=int(sys.argv[2]),"
                "user=sys.argv[3],password=sys.argv[4],dbname=sys.argv[5],"
                "connect_timeout=5).close()"
            ), host, port, user, password, db_name],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            print_status("Connected to PostgreSQL successfully", "success")
        else:
            err = (check.stderr or "").strip().splitlines()
            print_status(
                f"Could not connect to PostgreSQL yet: {err[-1] if err else 'unknown error'}",
                "warning",
            )
            print_status("  Make sure the server is running and reachable, then start the app", "info")
    return True


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


def run_setup(*, configure_db: bool = True):
    """Run the full setup process"""
    print(f"\n{Colors.BOLD}Running first-time setup...{Colors.END}\n")

    if not check_node_version():
        return False

    if not setup_venv():
        return False

    if not install_dependencies():
        return False

    if configure_db:
        configure_database()

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

    # Database credentials come from .env (written by --setup) or the process
    # env. A legacy DATABASE_URL from a pre-split install also counts — the
    # app still honors it, so an upgraded install starts without re-running
    # setup.
    env_values = _read_env_file(project_root / ".env")
    db_configured = any(
        env.get(key) or env_values.get(key)
        for key in ("POSTGRES_PASSWORD", "DATABASE_URL")
    )
    if not db_configured:
        print_status("Database not configured (no POSTGRES_PASSWORD). Run with --setup first.", "error")
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
    parser.add_argument("--setup", action="store_true", help="Install dependencies, configure DB and rebuild assets")
    parser.add_argument("--update", action="store_true", help="Update dependencies and rebuild assets without DB prompts")
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
        check_claude_auth()
        sys.exit(0 if ok else 1)

    if args.setup or args.update:
        if not run_setup(configure_db=not args.update):
            sys.exit(1)
        print(f"\n{Colors.BOLD}Ready to start!{Colors.END}")
        print("Run: python run_local.py")
        sys.exit(0)

    if args.pair or args.join:
        sys.exit(run_pairing_command(args.pair_url, args.join, args.port))

    # Double-clicking run_local.bat (or ./run_local.sh) on a fresh extract is the
    # documented first step, so a missing venv or frontend build runs setup here
    # rather than telling the user to come back with --setup.
    if needs_first_run_setup():
        print_status("First run detected: installing dependencies and building the frontend.", "info")
        if not run_setup(configure_db=True):
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
