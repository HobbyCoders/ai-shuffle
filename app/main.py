"""
AI Hub - Claude Code Web Interface and API
Main FastAPI application entry point
"""

import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings, ensure_directories
from app.db.database import init_database
from app.core.profiles import seed_builtin_profiles
from app.core.auth import auth_service

# Import API routers
from app.api import auth, profiles, projects, sessions, query, system

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

# Serve static files (Svelte build) if they exist
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


# Legacy endpoints for backward compatibility
# These map old endpoints to new API structure

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class LegacyChatRequest(BaseModel):
    prompt: str = Field(..., description="The prompt/question to send to Claude")
    model: Optional[str] = Field(None, description="Claude model to use")
    system_prompt: Optional[str] = Field(None, description="Optional system instructions")


class LegacyConversationMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., description="Message content")


class LegacyConversationRequest(BaseModel):
    messages: List[LegacyConversationMessage] = Field(..., description="Conversation history")
    system_prompt: Optional[str] = Field(None, description="System instructions")
    model: Optional[str] = Field(None, description="Claude model to use")


@app.post("/chat")
async def legacy_chat(request: LegacyChatRequest):
    """Legacy chat endpoint - maps to /api/v1/query"""
    from app.core.query_engine import execute_query

    if not auth_service.is_claude_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Claude CLI not authenticated. Please run 'claude login' in the container."
        )

    try:
        overrides = {}
        if request.model:
            overrides["model"] = request.model
        if request.system_prompt:
            overrides["system_prompt_append"] = request.system_prompt

        result = await execute_query(
            prompt=request.prompt,
            profile_id="code-reader",
            overrides=overrides if overrides else None
        )

        return {
            "response": result["response"],
            "status": "success",
            "metadata": result["metadata"]
        }

    except Exception as e:
        logger.error(f"Legacy chat error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.post("/conversation")
async def legacy_conversation(request: LegacyConversationRequest):
    """Legacy conversation endpoint"""
    from app.core.query_engine import execute_query

    if not auth_service.is_claude_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Claude CLI not authenticated"
        )

    try:
        # Get the last user message
        last_user_msg = None
        for msg in reversed(request.messages):
            if msg.role == "user":
                last_user_msg = msg.content
                break

        if not last_user_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No user message found in conversation"
            )

        overrides = {}
        if request.model:
            overrides["model"] = request.model
        if request.system_prompt:
            overrides["system_prompt_append"] = request.system_prompt

        result = await execute_query(
            prompt=last_user_msg,
            profile_id="code-reader",
            overrides=overrides if overrides else None
        )

        return {
            "response": result["response"],
            "status": "success",
            "metadata": result["metadata"]
        }

    except Exception as e:
        logger.error(f"Legacy conversation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.get("/auth/status")
async def legacy_auth_status():
    """Legacy auth status endpoint"""
    return auth_service.get_claude_auth_info()


@app.get("/auth/login-instructions")
async def legacy_login_instructions():
    """Legacy login instructions endpoint"""
    return auth_service.get_login_instructions()


@app.get("/version")
async def legacy_version():
    """Legacy version endpoint"""
    import subprocess

    try:
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            return {
                "version": result.stdout.strip(),
                "status": "success"
            }
        else:
            return {
                "version": "unknown",
                "status": "error",
                "error": result.stderr
            }
    except Exception as e:
        return {
            "version": "unknown",
            "status": "error",
            "error": str(e)
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False
    )
