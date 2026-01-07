#!/bin/bash
# AI Hub Local Mode Launcher for macOS/Linux
# Usage: ./run_local.sh [--setup]

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

print_banner() {
    echo -e "${BLUE}${BOLD}"
    echo "    _    ___   _   _ _   _ ____"
    echo "   / \\  |_ _| | | | | | | | __ )"
    echo "  / _ \\  | |  | |_| | | | |  _ \\"
    echo " / ___ \\ | |  |  _  | |_| | |_) |"
    echo "/_/   \\_\\___| |_| |_|\\___/|____/"
    echo -e "${NC}"
    echo -e "${GREEN}Local Mode Launcher${NC}"
    echo ""
}

print_status() {
    local message=$1
    local status=${2:-info}

    case $status in
        success) echo -e " ${GREEN}✓${NC} $message" ;;
        warning) echo -e " ${YELLOW}⚠${NC} $message" ;;
        error)   echo -e " ${RED}✗${NC} $message" ;;
        *)       echo -e " ${BLUE}ℹ${NC} $message" ;;
    esac
}

check_python() {
    if command -v python3 &> /dev/null; then
        PYTHON_CMD=python3
    elif command -v python &> /dev/null; then
        PYTHON_CMD=python
    else
        print_status "Python not found. Please install Python 3.10+" "error"
        exit 1
    fi

    # Check version
    PY_VERSION=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')

    if [[ $PY_MAJOR -lt 3 ]] || [[ $PY_MAJOR -eq 3 && $PY_MINOR -lt 10 ]]; then
        print_status "Python 3.10+ required (you have $PY_VERSION)" "error"
        exit 1
    fi

    print_status "Python $PY_VERSION detected" "success"
}

check_prerequisites() {
    echo -e "\n${BOLD}Checking prerequisites...${NC}"

    # Check Claude CLI
    if command -v claude &> /dev/null; then
        print_status "Claude CLI found at $(which claude)" "success"
    else
        print_status "Claude CLI not found" "warning"
        print_status "  Install: npm install -g @anthropic-ai/claude-code" "info"
    fi

    # Check Node.js
    if command -v node &> /dev/null; then
        NODE_VER=$(node --version)
        print_status "Node.js $NODE_VER found" "success"
    else
        print_status "Node.js not found" "warning"
        print_status "  Install from https://nodejs.org or use your package manager" "info"
    fi

    # Check GitHub CLI
    if command -v gh &> /dev/null; then
        print_status "GitHub CLI found at $(which gh)" "success"
    else
        print_status "GitHub CLI not found (optional)" "warning"
        print_status "  Install from https://cli.github.com" "info"
    fi
}

setup_venv() {
    if [[ -f "venv/bin/python" ]]; then
        print_status "Virtual environment already exists" "success"
        return 0
    fi

    print_status "Creating virtual environment..." "info"
    $PYTHON_CMD -m venv venv
    print_status "Virtual environment created" "success"
}

install_dependencies() {
    print_status "Installing Python dependencies..." "info"
    source venv/bin/activate
    pip install -r requirements.txt
    print_status "Dependencies installed" "success"
}

build_frontend() {
    if [[ -f "app/static/index.html" ]]; then
        print_status "Frontend already built" "success"
        return 0
    fi

    if ! command -v npm &> /dev/null; then
        print_status "npm not found - skipping frontend build" "warning"
        print_status "  Install Node.js to build the frontend" "info"
        return 1
    fi

    print_status "Building frontend..." "info"
    cd frontend
    npm install
    npm run build
    cd ..

    if [[ -d "frontend/build" ]]; then
        rm -rf app/static
        cp -r frontend/build app/static
        print_status "Frontend built and copied to app/static/" "success"
    else
        print_status "Build output not found" "error"
        return 1
    fi
}

check_claude_auth() {
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then
        print_status "Claude CLI credentials found" "success"
        return 0
    else
        print_status "Claude CLI not authenticated" "warning"
        print_status "  Run 'claude login' to authenticate" "info"
        return 1
    fi
}

run_setup() {
    echo -e "\n${BOLD}Running first-time setup...${NC}\n"

    setup_venv
    install_dependencies
    build_frontend || print_status "Frontend build skipped - you can build it later" "warning"
    check_claude_auth

    echo -e "\n${GREEN}Setup complete!${NC}"
}

start_server() {
    local host=${1:-127.0.0.1}
    local port=${2:-8000}

    echo -e "\n${BOLD}Starting AI Hub...${NC}"

    if [[ ! -f "venv/bin/python" ]]; then
        print_status "Virtual environment not found. Run with --setup first." "error"
        exit 1
    fi

    if [[ ! -f "app/static/index.html" ]]; then
        print_status "Frontend not built. Run with --setup first." "error"
        exit 1
    fi

    print_status "Server starting at http://$host:$port" "info"
    print_status "Press Ctrl+C to stop" "info"

    # Set environment variables for local mode
    export LOCAL_MODE=true
    export HOST=$host
    export PORT=$port
    export COOKIE_SECURE=false

    # Activate venv and start server
    source venv/bin/activate
    python -m app.main
}

# Main script
print_banner
check_python

case "$1" in
    --setup)
        check_prerequisites
        run_setup
        echo -e "\n${BOLD}Ready to start!${NC}"
        echo "Run: ./run_local.sh"
        ;;
    --check)
        check_prerequisites
        check_claude_auth
        ;;
    --help|-h)
        echo "Usage: $0 [OPTIONS]"
        echo ""
        echo "Options:"
        echo "  --setup     Run first-time setup (create venv, install deps, build frontend)"
        echo "  --check     Check prerequisites only"
        echo "  --help      Show this help message"
        echo ""
        echo "Environment variables:"
        echo "  HOST        Host to bind to (default: 127.0.0.1)"
        echo "  PORT        Port to run on (default: 8000)"
        ;;
    *)
        # Check if setup is needed
        if [[ ! -f "venv/bin/python" ]] || [[ ! -f "app/static/index.html" ]]; then
            print_status "First-time setup required..." "info"
            check_prerequisites
            run_setup
        fi

        start_server "${HOST:-127.0.0.1}" "${PORT:-8000}"
        ;;
esac
