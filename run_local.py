#!/usr/bin/env python3
"""
AI Hub Local Mode Launcher

Cross-platform launcher script for running AI Hub on your local PC.
This script handles:
- Virtual environment setup
- Dependency installation
- Prerequisite checks (Claude CLI, GitHub CLI)
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


def check_prerequisites():
    """Check all prerequisites are installed"""
    print(f"\n{Colors.BOLD}Checking prerequisites...{Colors.END}")

    all_ok = True

    # Check Claude CLI
    if not check_prerequisite(
        "Claude CLI",
        "claude",
        "npm install -g @anthropic-ai/claude-code"
    ):
        all_ok = False

    # Check Node.js (for building frontend)
    if not check_prerequisite(
        "Node.js",
        "node",
        "https://nodejs.org/ or use your package manager"
    ):
        print_status("  Node.js is needed to build the frontend", "info")

    # Check GitHub CLI (optional but recommended)
    if not check_prerequisite(
        "GitHub CLI",
        "gh",
        "https://cli.github.com/"
    ):
        print_status("  GitHub CLI is optional but recommended", "info")

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
        return True
    except subprocess.CalledProcessError as e:
        print_status(f"Failed to install dependencies: {e}", "error")
        return False


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

    if not shutil.which("npm"):
        print_status("npm not found - cannot build frontend", "error")
        print_status("Install Node.js from https://nodejs.org/", "info")
        return False

    print_status("Building frontend...", "info")

    try:
        # Install npm dependencies
        subprocess.run(["npm", "install"], cwd=str(frontend_dir), check=True)

        # Build the frontend
        subprocess.run(["npm", "run", "build"], cwd=str(frontend_dir), check=True)

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

    except subprocess.CalledProcessError as e:
        print_status(f"Failed to build frontend: {e}", "error")
        return False


def check_claude_auth():
    """Check if Claude CLI is authenticated"""
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        print_status("Claude CLI credentials found", "success")
        return True
    else:
        print_status("Claude CLI not authenticated", "warning")
        print_status("Run 'claude login' to authenticate", "info")
        return False


def run_setup():
    """Run the full setup process"""
    print(f"\n{Colors.BOLD}Running first-time setup...{Colors.END}\n")

    if not setup_venv():
        return False

    if not install_dependencies():
        return False

    if not build_frontend():
        print_status("Frontend build skipped - you can build it later", "warning")

    check_claude_auth()

    print(f"\n{Colors.GREEN}Setup complete!{Colors.END}")
    return True


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

    try:
        subprocess.run([
            str(venv_python), "-m", "app.main"
        ], cwd=str(project_root), env=env)
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
