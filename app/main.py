"""
AI Hub - Claude Code Web Interface and API
Main FastAPI application entry point
"""

import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings, ensure_directories
from app.db.database import init_database
from app.core.profiles import seed_builtin_profiles
from app.core.auth import auth_service

# Import API routers
from app.api import auth, profiles, projects, sessions, query, system, api_users, websocket


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
            "connect-src 'self' ws: wss:; "
            "font-src 'self'; "
            "frame-ancestors 'self';"
        )

        # Prevent caching of sensitive pages
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"

        return response

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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

    # Seed built-in profiles
    seed_builtin_profiles()

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

    logger.info(f"API docs: http://localhost:{settings.port}/docs")
    logger.info("=" * 60)

    yield

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

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
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
app.include_router(api_users.router)
app.include_router(websocket.router)

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
        reload=False
    )
