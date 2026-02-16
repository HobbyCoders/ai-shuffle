"""
AI Hub - Claude Code Web Interface and API
Main FastAPI application entry point
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings, ensure_directories, load_workspace_from_database
from app.db import database
from app.db.database import init_database
from app.core.profiles import run_migrations
from app.core.auth import auth_service
from app.core import encryption

# Import API routers
from app.api import auth, profiles, projects, sessions, query, system, websocket, commands, preferences, subagents, permission_rules, import_export, settings as settings_api, generated_images, generated_videos, shared_files, tags, analytics, search, templates, webhooks, security, rate_limits, github, git, studio, plugins, meshy, notes, hooks, generation, project_agents, project_plugins, mcp, mobile, internal, workspace_files, connectors, automations, browser, api_users

# Import middleware
from app.middleware.rate_limit import RateLimitMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers for reverse proxy deployments"""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # Security headers - essential for reverse proxy setups
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Content Security Policy - adjust based on your needs
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self'; "
            "frame-ancestors 'self';"
        )

        # Prevent caching of sensitive pages
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"

        return response


# Configure logging with both console and file handlers
# Guard against duplicate handlers when module is imported multiple times
# (e.g., uvicorn.run("app.main:app") re-imports the module)
log_level = getattr(logging, settings.log_level.upper())
log_format = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Root logger setup
root_logger = logging.getLogger()
root_logger.setLevel(log_level)

# Only add handlers if not already present (prevents duplicates on re-import)
if not root_logger.handlers:
    # Console handler (for docker logs)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

# Filter out noisy WebSocket connection logs from uvicorn
# (e.g., "('127.0.0.1', 55839) - "WebSocket /api/v1/ws/chat..." [accepted]")
class _UvicornWebSocketFilter(logging.Filter):
    """Suppress routine WebSocket accepted/open/closed messages."""
    NOISE = ("connection open", "connection closed", "[accepted]")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(phrase in msg for phrase in self.NOISE)

logging.getLogger("uvicorn.error").addFilter(_UvicornWebSocketFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager"""
    logger.info("=" * 60)
    logger.info(f"Starting AI Hub v{settings.version}")
    logger.info("=" * 60)

    # Ensure directories exist
    ensure_directories()

    # Initialize database
    init_database()

    # Run database migrations
    run_migrations()

    # Materialize notes as physical markdown files
    from app.core.note_files import materialize_all_notes, get_notes_dir
    notes_dir = get_notes_dir()
    unmaterialized = database.get_notes_without_file_path()
    if unmaterialized:
        logger.info(f"Materializing {len(unmaterialized)} notes as markdown files...")
        path_updates = materialize_all_notes(unmaterialized)
        database.bulk_update_note_file_paths(path_updates)
        logger.info(f"Materialized {len(path_updates)} note files to {notes_dir}")

    # Seed built-in subagents
    from app.core.builtin_subagents import seed_builtin_subagents
    seed_results = seed_builtin_subagents()
    created = [k for k, v in seed_results.items() if v == "created"]
    promoted = [k for k, v in seed_results.items() if v == "promoted"]
    if created:
        logger.info(f"Created built-in subagents: {', '.join(created)}")
    if promoted:
        logger.info(f"Promoted to built-in subagents: {', '.join(promoted)}")

    # Try to initialize encryption from ADMIN_PASSWORD env var
    # This enables headless operation without requiring admin login
    if encryption.init_encryption_from_env(database):
        logger.info("Encryption initialized from ADMIN_PASSWORD environment variable")
    else:
        # Clear all sessions on startup - forces re-login to restore encryption key
        # This is required because the encryption key (derived from admin password)
        # is only kept in memory and lost on restart
        admin_cleared, api_cleared = database.clear_all_sessions()
        if admin_cleared or api_cleared:
            logger.info(f"Cleared {admin_cleared} admin and {api_cleared} API user sessions (re-login required)")
        logger.info("Set ADMIN_PASSWORD env var for automatic encryption key loading")

    # Load user-configured workspace path from database (for local mode)
    load_workspace_from_database()

    # Re-ensure workspace directory exists (in case it was just configured)
    settings.effective_workspace_dir.mkdir(parents=True, exist_ok=True)

    # Check Claude CLI authentication
    if auth_service.is_claude_authenticated():
        logger.info("Claude CLI: Authenticated")
    else:
        logger.warning("Claude CLI: Not authenticated - run 'claude login' in container")

    # Check if setup is required
    if auth_service.is_setup_required():
        logger.info("Admin setup required - visit /setup to create admin account")
    else:
        logger.info(f"Admin user: {auth_service.get_admin_username()}")

    # Start connectors (after encryption is potentially available)
    from app.core.connectors.manager import connector_manager
    await connector_manager.start_all()

    # Start automations scheduler (after connectors are available)
    from app.core.automation_manager import automation_manager
    await automation_manager.start()

    # Start git file watcher service and hook manager
    from app.core.git_watcher import git_watcher
    from app.core.git_hook_manager import git_hook_manager
    await git_watcher.start()
    await git_hook_manager.start(port=settings.port)

    # Browser tools are now served in-process via SDK MCP server
    # (browser_sdk_tools.py), no startup registration needed.
    logger.info("Browser MCP server: In-process SDK mode (no subprocess)")

    logger.info(f"API docs: http://localhost:{settings.port}/docs")
    logger.info("=" * 60)

    yield

    # Shutdown automations (before connectors, since automations may use them)
    await automation_manager.shutdown()

    # Shutdown connectors
    await connector_manager.shutdown_all()

    # Shutdown git hook manager (uninstalls hooks before watcher stops)
    await git_hook_manager.shutdown()

    # Shutdown git file watcher
    await git_watcher.shutdown()

    # Cleanup browser sessions on shutdown
    try:
        from app.mcp.browser_session import browser_session_manager
        await browser_session_manager.close_all()
        logger.info("Browser sessions cleaned up")
    except Exception:
        pass

    logger.info("Shutting down AI Hub...")


# Create FastAPI application
app = FastAPI(
    title="AI Hub",
    description="Claude Code Web Interface and OpenAI-compatible API",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Security headers middleware (runs first on response)
app.add_middleware(SecurityHeadersMiddleware)

# Request body size limit middleware
class LimitRequestBodyMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent DoS attacks"""

    async def dispatch(self, request: Request, call_next):
        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        max_size = settings.max_request_body_mb * 1024 * 1024

        if content_length and int(content_length) > max_size:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large. Maximum size is {settings.max_request_body_mb}MB"}
            )

        return await call_next(request)

app.add_middleware(LimitRequestBodyMiddleware)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log only error responses (4xx/5xx) and slow requests.

    Replaces uvicorn's default access log (disabled via access_log=False)
    to eliminate noise from frequent successful polling requests.
    """

    SLOW_REQUEST_THRESHOLD_MS = 5000

    SKIP_PATHS = frozenset({"/health", "/api/v1/health", "/favicon.ico", "/favicon.svg"})
    SKIP_PREFIXES = ("/_app/", "/static/")

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        path = request.url.path
        status_code = response.status_code

        if path in self.SKIP_PATHS or path.startswith(self.SKIP_PREFIXES):
            return response

        if status_code >= 400:
            logger.warning(
                '%s - "%s %s" %d (%.0fms)',
                request.client.host if request.client else "-",
                request.method, path, status_code, duration_ms,
            )
        elif duration_ms >= self.SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                'SLOW %s - "%s %s" %d (%.0fms)',
                request.client.host if request.client else "-",
                request.method, path, status_code, duration_ms,
            )

        return response

app.add_middleware(AccessLogMiddleware)

# Rate limit middleware - enforce per-user rate limits
app.add_middleware(RateLimitMiddleware)

# CORS middleware - configure origins via CORS_ORIGINS environment variable
# Use "*" only for development; in production, specify exact origins
cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

# If "*" is in the list, we need to handle it specially
# Note: allow_credentials=True is incompatible with allow_origins=["*"] in production
if "*" in cors_origins:
    # Development mode - allow all origins but warn
    logger.warning("CORS configured with wildcard '*' - this is insecure for production!")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # Must be False when using wildcard
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Production mode - use specific origins with credentials
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include API routers
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(profiles.router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(query.router)
app.include_router(websocket.router)
app.include_router(commands.router)
app.include_router(preferences.router)
app.include_router(subagents.router)
app.include_router(permission_rules.router)
app.include_router(import_export.router)
app.include_router(settings_api.router)
app.include_router(generated_images.router)
app.include_router(generated_videos.router)
app.include_router(shared_files.router)
app.include_router(tags.router)
app.include_router(analytics.router)
app.include_router(search.router)
app.include_router(templates.router)
app.include_router(webhooks.router)
app.include_router(connectors.router)
app.include_router(api_users.router)
app.include_router(automations.router)
app.include_router(security.router)
app.include_router(rate_limits.router)
app.include_router(github.router)
app.include_router(git.router)
app.include_router(studio.router)
app.include_router(plugins.router)
app.include_router(meshy.router)
app.include_router(notes.router)
app.include_router(hooks.router)
app.include_router(generation.router)
app.include_router(project_agents.router)
app.include_router(project_plugins.router)
app.include_router(mcp.router)
app.include_router(mobile.router)
app.include_router(internal.router)
app.include_router(workspace_files.router)
app.include_router(browser.router)

# Serve static files (Svelte build) if they exist
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    # Mount static assets (js, css, etc.) at /_app
    app_assets = static_dir / "_app"
    if app_assets.exists():
        app.mount("/_app", StaticFiles(directory=str(app_assets)), name="app_assets")

    # Serve other static files
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static_files")

    # Serve index.html for SPA routes
    from fastapi.responses import FileResponse

    @app.get("/")
    async def serve_spa_root():
        return FileResponse(static_dir / "index.html")

    @app.get("/login")
    async def serve_spa_login():
        return FileResponse(static_dir / "index.html")

    @app.get("/setup")
    async def serve_spa_setup():
        return FileResponse(static_dir / "index.html")

    @app.get("/chat")
    async def serve_spa_chat():
        return FileResponse(static_dir / "index.html")

    @app.get("/chat/{path:path}")
    async def serve_spa_chat_path(path: str):
        return FileResponse(static_dir / "index.html")

    @app.get("/settings")
    async def serve_spa_settings():
        return FileResponse(static_dir / "index.html")

    @app.get("/workspace")
    async def serve_spa_workspace():
        return FileResponse(static_dir / "index.html")

    @app.get("/favicon.svg")
    async def serve_favicon():
        return FileResponse(static_dir / "favicon.svg")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
        access_log=False,  # Replaced by AccessLogMiddleware (only logs errors/slow requests)
        # Increase WebSocket max message size from default 1MB to 10MB
        # This is a safety net - payloads should be truncated before hitting this limit
        ws_max_size=10 * 1024 * 1024,  # 10MB
        # Disable Uvicorn's built-in WebSocket ping/pong — the app has its own
        # keepalive (30s server ping + 10s client ping). Uvicorn's default 20s
        # ping + 20s timeout can race with app-level pings and cause spurious
        # disconnects, especially during long load_session or tool execution.
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
