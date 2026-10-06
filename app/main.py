"""
AI Shuffle web interface and API.
Main FastAPI application entry point
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.cors import configured_origins
# Takes the desktop listener token out of the environment before anything can
# spawn a child process that would inherit it.
from app.core import desktop_listener  # noqa: F401

# Import API routers
from app.api import auth, model_providers, agents, projects, sessions, query, system, websocket, commands, preferences, settings as settings_api, settings_import, generated_images, generated_videos, generated_audio, shared_files, tags, analytics, templates, security, github, github_inbox, git, git_operations, plugins, notes, hooks, generation, project_agents, project_plugins, project_memory, mcp, browser_panel, mobile, pairing, internal, workspace_files, connectors, automations, api_users, documentation, catalog, logs, model_registry, computer_use, cluster, notifications, usage, voice
from app.api import voice_context, assistant
from app.api import tools as tools_api
from app.api import events as events_api
from app.api import artifacts as artifacts_api
from app.api import network as network_api
from app.api import plugin_oauth as plugin_oauth_api

# Import middleware
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
            "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com https://cdn.plaid.com; "
            "style-src 'self' 'unsafe-inline'; "
            # http:: cross-install artifact requests answer with a 307 to the
            # peer install that actually holds the blob, and cluster peers
            # advertise plain-http LAN base_urls. https: is already a blanket
            # image wildcard here and img-src grants no script execution, so
            # this only lets those images render — mixed-content rules still
            # govern them on an https page.
            "img-src 'self' data: blob: https: http:; "
            "media-src 'self' blob:; "
            # api.openai.com: the voice call surface POSTs its WebRTC SDP offer
            # directly from the browser to OpenAI Realtime (voiceCall.ts) —
            # without this the fetch dies as a CSP block ("Failed to fetch").
            "connect-src 'self' ws: wss: https://*.cloudflareaccess.com https://static.cloudflareinsights.com https://img.shields.io https://em-content.zobj.net https://api.openai.com https://*.plaid.com; "
            "frame-src 'self' https://cdn.plaid.com; "
            "font-src 'self'; "
            "frame-ancestors 'self';"
        )

        # HTML selects the revision's hashed JS/CSS. A cached entry page can keep
        # showing the previous UI after the worker has been updated. Immutable
        # assets remain cacheable; API responses still must never be stored.
        is_html = response.headers.get("content-type", "").split(";", 1)[0] == "text/html"
        if request.url.path.startswith("/api/") or is_html:
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

# Console handler (for docker logs), added once. Found by name, not by "root
# has no handlers": importing app.api.logs above already attached its ring
# buffer to the root logger, so that check never added this handler and the
# console carried no application logs at all. Test runs (TESTING=1) keep the
# console quiet, as they always have since then.
CONSOLE_HANDLER_NAME = "ai-shuffle-console"
if os.environ.get("TESTING") != "1" and not any(
    handler.get_name() == CONSOLE_HANDLER_NAME for handler in root_logger.handlers
):
    console_handler = logging.StreamHandler()
    console_handler.set_name(CONSOLE_HANDLER_NAME)
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
        shutdown_services, harden_process_introspection,
        prewarm_code_mode_sandbox,
    )
    from app.core.resource_monitor import start_resource_monitor, stop_resource_monitor
    from app.core.loop_watchdog import start_loop_watchdog, stop_loop_watchdog
    from app.core.plugin_auto_update import start_plugin_auto_update, stop_plugin_auto_update

    # Persistent, crash-surviving on-disk logging: attach a RotatingFileHandler
    # to the root logger + enable faulthandler + a logging excepthook so a crash
    # leaves a post-mortem trail under <data_dir>/logs/<instance_id> (durable
    # volume). Runs HERE, not at import time — a bare `import app.main` (e.g.
    # pytest collection with the real settings singleton) must never create the
    # log dir or truncate a previous crash's fatal.log. Additive only — does not
    # touch the ring buffer (app/api/logs.py) or the error_log DB handler.
    # Then the startup banner (pid/version/revision/instance_id) so restart boundaries —
    # and the LAST line before a crash — are obvious on disk, and the asyncio
    # loop exception handler now that a running loop exists.
    from app.core.file_logging import (
        setup_file_logging, log_startup_banner, install_asyncio_exception_handler,
        source_revision,
    )
    setup_file_logging()
    log_startup_banner()
    install_asyncio_exception_handler()

    logger.info("=" * 60)
    logger.info(f"Starting AI Shuffle v{settings.version} (revision {source_revision()})")
    logger.info("=" * 60)

    # Before any child is spawned: same-uid children must not be able to read
    # this process's /proc environ (its start-up secrets).
    harden_process_introspection()
    # Also before any child is spawned: every child inherits this run's id, so
    # the next boot can stop what this run leaves behind if it dies (#1711).
    if os.environ.get("TESTING") != "1":
        from app.core import process_ledger
        process_ledger.mark_run()
    prewarm_code_mode_sandbox()

    # Startup phases
    init_core()
    # OWNER_PASSWORD seeds the owner password on first boot only; it is taken
    # out of the environment either way so agents never inherit it.
    from app.core import owner_auth
    try:
        if owner_auth.seed_password_from_env() == 'seeded':
            logger.info("Owner password set from OWNER_PASSWORD (first boot)")
    except Exception:
        logger.exception("Could not apply OWNER_PASSWORD (non-fatal)")
    # LICENSE_KEY (Docker, Unraid) is likewise taken out of the environment
    # now; the license refresh task activates it once in the background.
    from app.core import license as licensing
    try:
        if licensing.capture_env_key():
            logger.info("License key found in LICENSE_KEY; it is activated in the background")
    except Exception:
        logger.exception("Could not read LICENSE_KEY (non-fatal)")
    # A network database and its device keys belong together. Do not start
    # schedulers/connector workers against another device's private database.
    from app.network import startup as network_startup
    try:
        network_startup.initialize()
    except network_startup.PrivateDatabaseRequiredError:
        raise
    except Exception:
        logger.exception("Worker network initialization failed (non-fatal)")
    # Now that the DB pool is up, funnel backend ERROR+ logs into error_log so
    # the GetAppErrors MCP tool and admin UI can surface them.
    from app.core.error_log_handler import install_error_log_handler
    install_error_log_handler()
    # Now the DB handler is attached, persist any pending "previous run crashed"
    # fatal.log warning to error_log too (the boot-check above ran before the
    # DB pool existed, so its WARNING couldn't reach error_log then).
    from app.core.file_logging import emit_pending_fatal_warning
    emit_pending_fatal_warning()

    # Register before starting any shared-DB workers. A brand-new instance id
    # on a populated database is visible to the existing admin but remains
    # quarantined until explicitly approved and restarted.
    from app.core import cluster
    from app.db.async_database import run_sync
    try:
        runner_approved = bool(await run_sync(cluster.register_self))
    except Exception:
        logger.exception("Cluster self-registration failed; keeping runner quarantined")
        runner_approved = False
    cluster_heartbeat_task = asyncio.create_task(_cluster_heartbeat_loop())

    # Install the v2 turn-scheduler seams (turn_scheduler.install +
    # turn_manager interrupt executor) BEFORE reconcile runs: reconcile's boot
    # sweep fires turn_scheduler.kick() for recovered sessions, which is a no-op
    # unless these seams are already installed.
    from app.core.startup import install_v2_turns_wiring
    install_v2_turns_wiring()

    # Reconcile from durable truth BEFORE any session machinery starts: a
    # previous run's crash can leave sessions durably marked parked (stuck
    # spinners/badges in the UI) and leak its CLI subprocesses — nothing in
    # memory survived, so clear both now. Guarded: reconcile failure must
    # never block boot. An install awaiting approval still runs turns, so it
    # still stops the processes its own previous run left behind; everything
    # touching shared database state waits for approval (#1711).
    try:
        from app.core import startup_reconcile
        await startup_reconcile.reconcile_at_boot(runner_approved)
    except Exception:
        logger.exception("Startup reconcile failed (non-fatal)")

    init_encryption_and_sessions()
    init_workspace()
    check_prerequisites()
    # Sync, scan and presence loops need unlocked credential storage (secrets
    # travel decrypted under the network key), so they start after
    # ``init_encryption_and_sessions``.
    network_tasks = []
    try:
        from app.network import startup as network_startup
        network_tasks = await network_startup.start()
    except Exception:
        logger.exception("Worker network loops failed to start (non-fatal)")
    bg_tasks = []
    monitor_task = None
    plugin_task = None
    orphan_backfill_task = None
    if runner_approved:
        await start_services()
        bg_tasks = start_background_tasks()
        from app.core.fluid_memory import maintain_memories
        bg_tasks.append(asyncio.create_task(maintain_memories(), name="memory-maintenance"))
        monitor_task = await start_resource_monitor(interval_seconds=300)
        # Thread-based: keeps working while the loop is frozen and logs the
        # loop thread's stack so a stall is diagnosable after the fact.
        start_loop_watchdog()
        plugin_task = start_plugin_auto_update()
    else:
        logger.warning(
            "Runner services are disabled while this install awaits approval. "
            "The admin UI and heartbeat remain available; processes this "
            "install's own previous run left behind are still stopped at boot."
        )

    # One-time, unbounded backfill: heal any sessions orphaned by a past
    # mid-turn crash (NULL sdk_session_id but an intact transcript in the store →
    # blank history in postgres_only). Runs off the event loop and logs an ERROR
    # per heal so the operator is alerted. Periodic re-sweeps (bounded to recent
    # sessions) continue inside the resource monitor loop.
    if runner_approved:
        from app.core.resource_monitor import reconcile_orphans_once
        orphan_backfill_task = asyncio.create_task(reconcile_orphans_once())

    if settings.api_docs_enabled:
        logger.info(f"API docs: http://localhost:{settings.port}/docs")
    logger.info("=" * 60)

    from app.core.provider_usage import provider_usage_monitor
    if runner_approved:
        provider_usage_monitor.start()

    yield

    await provider_usage_monitor.stop()

    # Shutdown
    if orphan_backfill_task is not None and not orphan_backfill_task.done():
        orphan_backfill_task.cancel()
    if orphan_backfill_task is not None:
        try:
            await orphan_backfill_task
        except asyncio.CancelledError:
            pass
    cluster_heartbeat_task.cancel()
    try:
        await cluster_heartbeat_task
    except asyncio.CancelledError:
        pass
    if network_tasks:
        try:
            from app.network import startup as network_startup
            await network_startup.stop()
        except Exception:
            logger.exception("Worker network shutdown failed")
    for task in bg_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if plugin_task is not None:
        await stop_plugin_auto_update(plugin_task)
    if monitor_task is not None:
        await stop_resource_monitor(monitor_task)
    stop_loop_watchdog()
    from app.core import browser_runtime
    await browser_runtime.shutdown()
    # Close any live voice sideband: the socket belongs to this process,
    # so a restart must hang up rather than leak a half-open realtime call.
    try:
        from app.core import voice_bridge
        await voice_bridge.shutdown_all()
    except Exception:
        logger.exception("voice_bridge.shutdown_all failed")
    # Per-connection WS writer tasks are normally retired by unregister_*; this
    # cancels any that outlive their connection so none leak past process exit.
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.shutdown()
    except Exception:
        logger.exception("sync_engine.shutdown failed")
    if runner_approved:
        await shutdown_services()
    # Journal losses buffered since the last rate-limited warning would die
    # with the process; write them to each store's journal_loss.json.
    try:
        from fluid.codemode.skills import flush_journal_loss
        await run_sync(flush_journal_loss)
    except Exception:
        logger.exception("Inference journal loss flush failed")
    try:
        from app.core.error_log_handler import uninstall_error_log_handler
        uninstall_error_log_handler()
    except Exception:
        pass
    logger.info("Shutting down AI Shuffle...")
    from app.core.file_logging import log_shutdown_banner
    log_shutdown_banner()


# Create FastAPI application
app = FastAPI(
    title="AI Shuffle",
    description="AI Shuffle web interface and OpenAI-compatible API",
    version=settings.version,
    lifespan=lifespan,
    # Off unless API_DOCS_ENABLED=true: these routes are unauthenticated and
    # map the whole API for anyone who can reach the port.
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
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

    # Lowered from 5000ms so 2-4s "sluggish" actions become visible in the
    # access log (they previously slipped under the threshold). Override with
    # ACCESS_LOG_SLOW_MS if this proves too chatty on a slow host.
    SLOW_REQUEST_THRESHOLD_MS = int(os.environ.get("ACCESS_LOG_SLOW_MS", "1500"))

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
        elif (
            duration_ms >= self.SLOW_REQUEST_THRESHOLD_MS
            and not (
                request.method == "POST"
                and path == "/api/v1/browser/panel/control/poll"
                and 200 <= status_code < 300
            )
        ):
            # A successful panel poll deliberately waits for commands (20s).
            # Failures above and slow requests on other routes stay visible.
            logger.warning(
                'SLOW %s - "%s %s" %d (%.0fms)',
                request.client.host if request.client else "-",
                request.method, path, status_code, duration_ms,
            )

        return response

app.add_middleware(AccessLogMiddleware)

# CSRF protection middleware - validates Origin/Referer on state-changing requests.
app.add_middleware(CSRFProtectionMiddleware)

# CORS middleware - configure origins via CORS_ORIGINS environment variable
# Use "*" only for development; in production, specify exact origins
# CORS_ORIGINS plus the Android and iOS apps' bundled-UI origins (see app/core/cors.py).
cors_origins = configured_origins()

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

# Desktop listener token (outermost): when the desktop app launched this
# worker with one, local requests without it are refused.
from app.middleware.desktop_listener import DesktopListenerGate
app.add_middleware(DesktopListenerGate)

# Include API routers
app.include_router(system.router)
app.include_router(auth.router)
from app.api import owner_login
app.include_router(owner_login.router)
app.include_router(model_providers.router)
app.include_router(agents.router)
app.include_router(assistant.router)
app.include_router(projects.router)
app.include_router(sessions.router)
from app.api import chat_access
app.include_router(chat_access.router)
from app.api.swarms import router as swarm_router
app.include_router(swarm_router, prefix="/api/sessions", tags=["Sessions"])
app.include_router(query.router)
app.include_router(websocket.router)
app.include_router(commands.router)
app.include_router(preferences.router)
app.include_router(settings_api.router)
app.include_router(settings_import.router)
app.include_router(generated_images.router)
app.include_router(generated_videos.router)
app.include_router(generated_audio.router)
app.include_router(shared_files.router)
app.include_router(tags.router)
app.include_router(analytics.router)
app.include_router(templates.router)
app.include_router(connectors.router)
app.include_router(api_users.router)
app.include_router(automations.router)
app.include_router(security.router)
app.include_router(github.router)
app.include_router(github_inbox.router)
app.include_router(git.router)
app.include_router(git_operations.router)
# Before plugins.router: its catch-all /{plugin_id:path} would take /oauth/*.
app.include_router(plugin_oauth_api.router)
app.include_router(plugins.router)
app.include_router(notes.router)
app.include_router(hooks.router)
app.include_router(generation.router)
app.include_router(project_agents.router)
app.include_router(project_plugins.router)
app.include_router(project_memory.router)
app.include_router(mcp.router)
app.include_router(tools_api.router)
app.include_router(browser_panel.router)
app.include_router(mobile.router)
from app.api import mobile_tools
app.include_router(mobile_tools.router)
app.include_router(pairing.router)
app.include_router(internal.router)
app.include_router(workspace_files.router)
app.include_router(workspace_files.peer_router)
from app.api import remote_workspace_files
app.include_router(remote_workspace_files.router)
app.include_router(documentation.router)
app.include_router(catalog.router)
app.include_router(logs.router)
from app.api import support_reports
app.include_router(support_reports.router)
app.include_router(model_registry.router)
app.include_router(computer_use.router)
app.include_router(cluster.router)
app.include_router(network_api.router)
app.include_router(notifications.router)
app.include_router(usage.router)
app.include_router(voice.router)
app.include_router(voice_context.router)
app.include_router(events_api.router)
app.include_router(artifacts_api.router)
from app.api import license as license_api
app.include_router(license_api.router)

# Preview is opt-in and authenticates branch-specific assets on this listener.
from app.preview import install_preview

# Serve packaged assets only when the branch preview has not claimed the UI.
static_dir = Path(__file__).parent / "static"
if not install_preview(app) and static_dir.exists():
    # Mount static assets (js, css, etc.) at /_app
    app_assets = static_dir / "_app"
    if app_assets.exists():
        app.mount("/_app", StaticFiles(directory=str(app_assets)), name="app_assets")

    # Serve icons
    icons_dir = static_dir / "icons"
    if icons_dir.exists():
        app.mount("/icons", StaticFiles(directory=str(icons_dir)), name="icons")

    # The phone UI the Android and iOS apps load from this worker (app/mobile_ui.py).
    from app.mobile_ui import install_mobile_ui
    if not install_mobile_ui(app, static_dir / "mobile"):
        logger.warning("app/static/mobile is missing: the phone apps cannot open chat on this worker. "
                       "Rebuild the frontend with mobile/ present.")

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

    @app.get("/plaid-link")
    async def serve_plaid_link():
        return FileResponse(static_dir / "index.html")

    @app.get("/setup")
    async def serve_spa_setup():
        return FileResponse(static_dir / "index.html")

    @app.get("/pair")
    async def serve_spa_pair():
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
    from app.serve import run

    run(app, settings)
