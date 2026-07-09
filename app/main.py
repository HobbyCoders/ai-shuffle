"""
AI Hub - Claude Code Web Interface and API
Main FastAPI application entry point
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

# Import API routers
from app.api import auth, claude_accounts, profiles, projects, sessions, query, system, websocket, commands, preferences, subagents, permission_rules, import_export, settings as settings_api, generated_images, generated_videos, generated_audio, shared_files, tags, analytics, search, templates, webhooks, security, rate_limits, github, git, git_operations, plugins, notes, hooks, generation, project_agents, project_plugins, project_memory, mcp, chrome_mcp, mobile, internal, workspace_files, connectors, automations, api_users, documentation, catalog, claude_status, logs, teams, model_registry, computer_use, cluster, notifications, usage

# Import middleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.csrf import CSRFProtectionMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers for reverse proxy deployments"""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # Security headers - essential for reverse proxy setups
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Content Security Policy
        # Note: 'unsafe-inline' is required for both script-src (SvelteKit's
        # bootstrap inline script in index.html) and style-src (SvelteKit's
        # runtime style injection and component libraries).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss: https://*.cloudflareaccess.com https://static.cloudflareinsights.com https://img.shields.io https://em-content.zobj.net; "
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


# How often this install bumps its registry last_seen. Online window in the
# registry (app/db/installs.py) is ~3x this so a single missed beat won't flap.
CLUSTER_HEARTBEAT_INTERVAL_SECONDS = 30


async def _cluster_heartbeat_loop():
    """Periodically refresh this install's last_seen in the shared registry."""
    from app.core import cluster
    from app.db.async_database import run_sync
    while True:
        try:
            await asyncio.sleep(CLUSTER_HEARTBEAT_INTERVAL_SECONDS)
            await run_sync(cluster.heartbeat_self)
            # Reconcile cross-install streaming: clear any remote-owned streaming
            # spinner whose owning install went offline mid-stream (its stream_end
            # fan-out never arrived).
            from app.core.sync_engine import sync_engine
            await sync_engine.sweep_offline_remote_streaming()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed beat must not kill the loop
            logger.debug("Cluster heartbeat tick failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager"""
    from app.core.startup import (
        init_core, init_encryption_and_sessions, init_workspace,
        check_prerequisites, start_services, start_background_tasks,
        shutdown_services,
    )
    from app.core.resource_monitor import start_resource_monitor, stop_resource_monitor
    from app.core.plugin_auto_update import start_plugin_auto_update, stop_plugin_auto_update

    # Persistent, crash-surviving on-disk logging: attach a RotatingFileHandler
    # to the root logger + enable faulthandler + a logging excepthook so a crash
    # leaves a post-mortem trail under <data_dir>/logs/<instance_id> (durable
    # volume). Runs HERE, not at import time — a bare `import app.main` (e.g.
    # pytest collection with the real settings singleton) must never create the
    # log dir or truncate a previous crash's fatal.log. Additive only — does not
    # touch the ring buffer (app/api/logs.py) or the error_log DB handler.
    # Then the startup banner (pid/version/instance_id) so restart boundaries —
    # and the LAST line before a crash — are obvious on disk, and the asyncio
    # loop exception handler now that a running loop exists.
    from app.core.file_logging import (
        setup_file_logging, log_startup_banner, install_asyncio_exception_handler,
    )
    setup_file_logging()
    log_startup_banner()
    install_asyncio_exception_handler()

    logger.info("=" * 60)
    logger.info(f"Starting AI Hub v{settings.version}")
    logger.info("=" * 60)

    # Patch the SDK's resume materialization BEFORE any session can connect:
    # per-spawn temp CLAUDE_CONFIG_DIRs churn the Chrome native-host manifest
    # and pop the extension reconnect window on every resumed spawn.
    from app.core.sdk_resume_patch import install_stable_resume_patch
    install_stable_resume_patch()

    # Startup phases
    init_core()
    # Now that the DB pool is up, funnel backend ERROR+ logs into error_log so
    # the GetAppErrors MCP tool and admin UI can surface them.
    from app.core.error_log_handler import install_error_log_handler
    install_error_log_handler()
    # Now the DB handler is attached, persist any pending "previous run crashed"
    # fatal.log warning to error_log too (the boot-check above ran before the
    # DB pool existed, so its WARNING couldn't reach error_log then).
    from app.core.file_logging import emit_pending_fatal_warning
    emit_pending_fatal_warning()

    # Reconcile from durable truth BEFORE any session machinery starts: a
    # previous run's crash can leave sessions durably marked parked (stuck
    # spinners/badges in the UI) and leak its CLI subprocesses — nothing in
    # memory survived, so clear both now. Guarded: reconcile failure must
    # never block boot.
    try:
        from app.core.startup_reconcile import reconcile_from_durable_truth
        await reconcile_from_durable_truth()
    except Exception:
        logger.exception("Startup reconcile failed (non-fatal)")

    init_encryption_and_sessions()
    init_workspace()
    check_prerequisites()
    await start_services()
    bg_tasks = start_background_tasks()
    monitor_task = await start_resource_monitor(interval_seconds=300)
    plugin_task = start_plugin_auto_update()

    # One-time, unbounded backfill: heal any sessions orphaned by a past
    # mid-turn crash (NULL sdk_session_id but an intact transcript in the store →
    # blank history in postgres_only). Runs off the event loop and logs an ERROR
    # per heal so the operator is alerted. Periodic re-sweeps (bounded to recent
    # sessions) continue inside the resource monitor loop.
    from app.core.resource_monitor import reconcile_orphans_once
    orphan_backfill_task = asyncio.create_task(reconcile_orphans_once())

    # Cross-install cluster: register this install in the shared registry and
    # heartbeat its last_seen so peers can see it's online and relay control.
    from app.core import cluster
    from app.db.async_database import run_sync
    try:
        await run_sync(cluster.register_self)
    except Exception:
        logger.exception("Cluster self-registration failed (non-fatal)")
    cluster_heartbeat_task = asyncio.create_task(_cluster_heartbeat_loop())

    logger.info(f"API docs: http://localhost:{settings.port}/docs")
    logger.info("=" * 60)

    yield

    # Shutdown
    if not orphan_backfill_task.done():
        orphan_backfill_task.cancel()
    try:
        await orphan_backfill_task
    except asyncio.CancelledError:
        pass
    cluster_heartbeat_task.cancel()
    try:
        await cluster_heartbeat_task
    except asyncio.CancelledError:
        pass
    for task in bg_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await stop_plugin_auto_update(plugin_task)
    await stop_resource_monitor(monitor_task)
    try:
        from app.core.team_transcript_watcher import team_transcript_watcher
        await team_transcript_watcher.stop_all()
    except Exception:
        logger.exception("team_transcript_watcher.stop_all failed")
    await shutdown_services()
    try:
        from app.core.error_log_handler import uninstall_error_log_handler
        uninstall_error_log_handler()
    except Exception:
        pass
    logger.info("Shutting down AI Hub...")
    from app.core.file_logging import log_shutdown_banner
    log_shutdown_banner()


# Create FastAPI application
app = FastAPI(
    title="AI Hub",
    description="Claude Code Web Interface and OpenAI-compatible API",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Global exception handler — surface actual error details instead of generic
# "Internal Server Error" for unhandled exceptions.  The detail is already
# logged by the AccessLogMiddleware; this just ensures the *client* sees it too.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {exc}"},
    )

# Security headers middleware (runs first on response)
app.add_middleware(SecurityHeadersMiddleware)

# Request body size limit middleware
class LimitRequestBodyMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent DoS attacks, including chunked transfers."""

    async def dispatch(self, request: Request, call_next):
        max_size = settings.max_request_body_mb * 1024 * 1024

        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_size:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large. Maximum size is {settings.max_request_body_mb}MB"}
            )

        # For chunked transfers (no Content-Length), wrap the body stream
        # to enforce the limit by tracking bytes read.
        if not content_length and request.method in ("POST", "PUT", "PATCH"):
            original_body = request._body if hasattr(request, "_body") else None  # noqa: SLF001
            if original_body is None:
                # Wrap the receive callable to track bytes
                original_receive = request._receive  # noqa: SLF001
                bytes_read = 0

                async def limited_receive():
                    nonlocal bytes_read
                    message = await original_receive()
                    if message.get("type") == "http.request":
                        body_chunk = message.get("body", b"")
                        bytes_read += len(body_chunk)
                        if bytes_read > max_size:
                            from fastapi.responses import JSONResponse
                            raise ValueError(
                                f"Request body too large. Maximum size is {settings.max_request_body_mb}MB"
                            )
                    return message

                request._receive = limited_receive  # noqa: SLF001

        try:
            return await call_next(request)
        except ValueError as e:
            if "Request body too large" in str(e):
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=413,
                    content={"detail": str(e)}
                )
            raise

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

# CSRF protection middleware - validates Origin/Referer on state-changing requests.
# Must be registered AFTER RateLimitMiddleware so it runs BEFORE rate limiting
# (Starlette processes middleware in reverse registration order for requests).
app.add_middleware(CSRFProtectionMiddleware)

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
app.include_router(claude_accounts.router)
app.include_router(profiles.router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(query.router)
app.include_router(websocket.router)
app.include_router(commands.router)
app.include_router(preferences.router)
app.include_router(subagents.router)
app.include_router(teams.router)
app.include_router(permission_rules.router)
app.include_router(import_export.router)
app.include_router(settings_api.router)
app.include_router(generated_images.router)
app.include_router(generated_videos.router)
app.include_router(generated_audio.router)
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
app.include_router(git_operations.router)
app.include_router(plugins.router)
app.include_router(notes.router)
app.include_router(hooks.router)
app.include_router(generation.router)
app.include_router(project_agents.router)
app.include_router(project_plugins.router)
app.include_router(project_memory.router)
app.include_router(mcp.router)
app.include_router(chrome_mcp.router)
app.include_router(mobile.router)
app.include_router(internal.router)
app.include_router(workspace_files.router)
app.include_router(documentation.router)
app.include_router(catalog.router)
app.include_router(claude_status.router)
app.include_router(logs.router)
app.include_router(model_registry.router)
app.include_router(computer_use.router)
app.include_router(cluster.router)
app.include_router(notifications.router)
app.include_router(usage.router)

# Serve static files (Svelte build) if they exist
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    # Mount static assets (js, css, etc.) at /_app
    app_assets = static_dir / "_app"
    if app_assets.exists():
        app.mount("/_app", StaticFiles(directory=str(app_assets)), name="app_assets")

    # Serve icons
    icons_dir = static_dir / "icons"
    if icons_dir.exists():
        app.mount("/icons", StaticFiles(directory=str(icons_dir)), name="icons")

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

    @app.get("/sw.js")
    async def serve_service_worker():
        return FileResponse(static_dir / "sw.js", media_type="application/javascript")

    @app.get("/manifest.json")
    async def serve_manifest():
        return FileResponse(static_dir / "manifest.json", media_type="application/json")


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
