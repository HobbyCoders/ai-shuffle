"""
System API routes - health, version, stats, workspace configuration, tools
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.models import HealthResponse, VersionResponse, StatsResponse
from app.core.auth import auth_service
from app.core.config import settings
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, require_admin

logger = logging.getLogger(__name__)


router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint (no auth required)"""
    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": settings.version,
        "authenticated": False,  # Not checking session for health
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated()
    }


@router.get("/api/v1/health", response_model=HealthResponse)
async def api_health_check():
    """API health check endpoint"""
    return await health_check()


async def _readiness_report() -> tuple[bool, dict]:
    """Build the readiness report: (ready, body).

    Checks the two things a probe must know before routing traffic to this
    instance:
      * the database answers ``SELECT 1`` (the pool is up and the DB reachable)
      * the session pool has admission headroom (not wedged at its hard cap)

    Returns a plain dict so both the route and its tests can assert on the body
    without a live server. Never raises — every failure is folded into a reason
    string so a probe always gets a structured answer.
    """
    reasons: list[str] = []
    checks: dict = {}

    # 1) Database: SELECT 1 in a short-lived connection.
    try:
        await asyncio.to_thread(_probe_db)
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001 — a probe must never 500
        checks["database"] = "error"
        reasons.append(f"database unreachable: {e}")

    # 2) Session pool headroom: reject only when wedged at the hard cap
    #    (no new top-level session could be admitted right now).
    try:
        from app.core.session_pool import get_session_pool
        pool = get_session_pool()
        pool_size = len(pool)
        hard_cap = pool.hard_cap
        checks["session_pool"] = {
            "size": pool_size,
            "max_size": pool.max_size,
            "hard_cap": hard_cap,
        }
        if pool.is_at_hard_cap():
            reasons.append(
                f"session pool at hard cap ({pool_size}/{hard_cap})"
            )
    except Exception as e:  # noqa: BLE001
        checks["session_pool"] = "error"
        reasons.append(f"session pool unavailable: {e}")

    ready = not reasons
    body = {
        "status": "ready" if ready else "not_ready",
        "service": settings.service_name,
        "version": settings.version,
        "checks": checks,
    }
    if reasons:
        body["reasons"] = reasons
    return ready, body


def _probe_db() -> None:
    """Run ``SELECT 1`` against a pooled connection (blocking; call via to_thread)."""
    from app.db.base import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()


@router.get("/health/ready")
async def readiness_check():
    """Kubernetes/Docker-style readiness probe (no auth — probes are unauthenticated).

    200 with a small JSON body when the DB answers and the session pool has
    admission headroom; 503 with ``reasons`` when not ready. Distinct from
    ``/health`` (liveness): a live process can still be *not ready* (DB down,
    pool wedged), and orchestrators must not route traffic to it.
    """
    ready, body = await _readiness_report()
    if not ready:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
    return body


@router.get("/api/v1/health/ready")
async def api_readiness_check():
    """API-prefixed alias for the readiness probe."""
    return await readiness_check()


@router.get("/api/v1/version", response_model=VersionResponse)
async def get_version():
    """Get API and Claude Code versions"""
    claude_version = None

    try:
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            claude_version = result.stdout.strip()
    except Exception:
        pass

    return {
        "api_version": settings.version,
        "claude_version": claude_version
    }


@router.get("/api/v1/stats", response_model=StatsResponse)
async def get_stats(token: str = Depends(require_auth)):
    """Get usage statistics"""
    # Polled for the status widget: one aggregation, one off-loop hop.
    stats = await run_sync(database.get_usage_stats)
    return stats


@router.get("/api/v1/deployment")
async def get_deployment_info(token: str = Depends(require_auth)):
    """
    Get deployment configuration and platform information.

    Useful for debugging and understanding the current deployment mode.
    """
    return settings.get_deployment_info()


@router.get("/api/v1/diagnostics")
async def get_diagnostics(token: str = Depends(require_auth)):
    """
    On-demand resource diagnostics snapshot.

    Returns the same metrics collected by the periodic ResourceMonitor,
    plus warnings. Use this to check system health without digging through logs.
    """
    from app.core.resource_monitor import _collect_metrics, _check_warnings

    metrics = await _collect_metrics()
    warnings = _check_warnings(metrics)

    return {
        "metrics": metrics,
        "warnings": warnings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Workspace Configuration (for local mode)
# ============================================================================

class WorkspaceConfigResponse(BaseModel):
    """Workspace configuration response"""
    workspace_path: str
    is_configured: bool
    is_local_mode: bool
    default_path: str
    exists: bool


class WorkspaceConfigRequest(BaseModel):
    """Request to set workspace path"""
    workspace_path: str


@router.get("/api/v1/workspace/config")
async def get_workspace_config() -> WorkspaceConfigResponse:
    """
    Get current workspace configuration.

    This endpoint is used during setup to determine if workspace needs to be configured.
    No authentication required so it can be called before admin setup.
    """
    from app.core.platform import get_default_workspace_dir, is_local_mode

    # Get configured path from database. `is_configured` only reflects whether
    # THIS instance has its own workspace saved — a legacy global path belongs
    # to whichever machine set it up first, not the current instance. Falling
    # back to the global path for `workspace_path` display is fine so the
    # setup UI can pre-fill a sensible default.
    instance_key = f"workspace_path:{settings.effective_instance_id}"

    def _read_paths():
        """Both settings reads in ONE hop — this endpoint is polled during
        setup, and two separate hops would double the scheduler churn for what
        is a single logical lookup."""
        return (
            database.get_system_setting(instance_key),
            database.get_system_setting("workspace_path"),
        )

    instance_path, legacy_path = await run_sync(_read_paths)
    configured_path = instance_path or legacy_path

    # Determine current effective path
    if configured_path:
        current_path = configured_path
    else:
        current_path = str(settings.effective_workspace_dir)
    is_configured = bool(instance_path)

    # Get default path for the platform
    default_path = str(get_default_workspace_dir())

    return WorkspaceConfigResponse(
        workspace_path=current_path,
        is_configured=is_configured,
        is_local_mode=is_local_mode(),
        default_path=default_path,
        exists=Path(current_path).exists()
    )


@router.post("/api/v1/workspace/config")
async def set_workspace_config(
    request: WorkspaceConfigRequest,
    token: str = Depends(require_admin)
) -> WorkspaceConfigResponse:
    """
    Set the workspace directory path (admin only).

    This is primarily used in local mode to let users choose where to store projects.
    The directory will be created if it doesn't exist.
    """
    from app.core.platform import is_local_mode

    workspace_path = request.workspace_path.strip()

    if not workspace_path:
        raise HTTPException(status_code=400, detail="Workspace path cannot be empty")

    # Convert to absolute path
    path = Path(workspace_path).expanduser().resolve()

    # Validate the path
    try:
        # Create directory if it doesn't exist
        path.mkdir(parents=True, exist_ok=True)

        # Test write permissions by creating a temp file
        test_file = path / ".ai-shuffle-test"
        test_file.touch()
        test_file.unlink()

    except PermissionError:
        raise HTTPException(
            status_code=400,
            detail=f"Permission denied: Cannot write to '{path}'"
        )
    except Exception as e:
        logger.exception("Invalid workspace path")
        raise HTTPException(
            status_code=400,
            detail="Invalid path"
        )

    # Save to database (instance-scoped so multi-instance deployments don't conflict)
    instance_key = f"workspace_path:{settings.effective_instance_id}"
    await run_sync(database.set_system_setting, instance_key, str(path))

    # Update the runtime settings
    object.__setattr__(settings, 'workspace_dir', path)

    return WorkspaceConfigResponse(
        workspace_path=str(path),
        is_configured=True,
        is_local_mode=is_local_mode(),
        default_path=str(path),  # After setting, this becomes the effective path
        exists=True
    )


@router.post("/api/v1/workspace/validate")
async def validate_workspace_path(request: WorkspaceConfigRequest, http_request: Request = None) -> dict:
    """
    Validate a workspace path without saving it.

    Requires admin authentication, EXCEPT during initial setup
    (when no admin exists yet) where it is allowed for setup convenience.

    This handler is stat-only: it never creates or mutates the filesystem. It
    reports existence/writability via os.stat/os.access without touching disk.
    """
    # Gate: admin-only unless first-run setup is still pending. FastAPI always
    # injects http_request; the None default only applies to direct unit calls.
    if http_request is not None and not auth_service.is_setup_required():
        require_admin(http_request)

    workspace_path = request.workspace_path.strip()

    if not workspace_path:
        return {
            "valid": False,
            "error": "Workspace path cannot be empty",
            "path": "",
            "exists": False,
            "writable": False
        }

    try:
        path = Path(workspace_path).expanduser().resolve()
        exists = path.exists()
        writable = False
        error = None

        if exists:
            # Stat-only writability probe: no file is created or removed.
            if os.access(str(path), os.W_OK):
                writable = True
            else:
                error = "Directory exists but is not writable"
        else:
            # Directory does not exist yet — check whether the parent is writable
            # so it could be created later, without actually creating anything.
            parent = path.parent
            if parent.exists():
                if os.access(str(parent), os.W_OK):
                    writable = True
                else:
                    error = "Cannot create directory: permission denied"
            else:
                error = f"Parent directory does not exist: {parent}"

        return {
            "valid": writable and error is None,
            "error": error,
            "path": str(path),
            "exists": exists,
            "writable": writable
        }

    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
            "path": workspace_path,
            "exists": False,
            "writable": False
        }


# ============================================================================
# Filesystem Browse API (local mode only)
# ============================================================================

class DirectoryEntry(BaseModel):
    """A directory entry for the filesystem browser"""
    name: str
    path: str
    is_dir: bool
    has_children: bool = False  # For directories: whether they contain subdirs


class BrowseResponse(BaseModel):
    """Response from filesystem browse"""
    path: str
    parent: Optional[str]
    entries: List[DirectoryEntry]
    is_local_mode: bool


@router.get("/api/v1/filesystem/browse")
async def browse_filesystem(
    path: Optional[str] = None,
    token: str = Depends(require_admin)
) -> BrowseResponse:
    """
    Browse the filesystem for directory selection (admin only).

    In local mode: browse the full filesystem.
    In Docker mode: browse within the workspace directory only.

    Returns directories at the given path for the tree browser UI.
    If no path is given, returns starting points.
    """
    from app.core.platform import is_local_mode
    from app.core.config import settings as app_settings
    import sys

    local_mode = is_local_mode()
    workspace_root = app_settings.workspace_dir.resolve()

    if not path:
        entries = []
        if local_mode:
            # Local mode: platform-specific starting points
            if sys.platform == "win32":
                import string
                for letter in string.ascii_uppercase:
                    drive = f"{letter}:\\"
                    if Path(drive).exists():
                        entries.append(DirectoryEntry(
                            name=f"{letter}:",
                            path=drive,
                            is_dir=True,
                            has_children=True
                        ))
            else:
                home = str(Path.home())
                entries.append(DirectoryEntry(
                    name="Home",
                    path=home,
                    is_dir=True,
                    has_children=True
                ))
                entries.append(DirectoryEntry(
                    name="/",
                    path="/",
                    is_dir=True,
                    has_children=True
                ))
        else:
            # Docker mode: start at workspace root
            entries.append(DirectoryEntry(
                name="workspace",
                path=str(workspace_root),
                is_dir=True,
                has_children=True
            ))

        return BrowseResponse(
            path="",
            parent=None,
            entries=entries,
            is_local_mode=local_mode
        )

    # Browse the specified directory
    target = Path(path).resolve()

    # Docker mode: enforce workspace boundary
    if not local_mode:
        try:
            target.relative_to(workspace_root)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Browsing outside the workspace is not allowed"
            )

    if not target.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}"
        )
    if not target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path is not a directory: {path}"
        )

    entries = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            # Skip hidden files/dirs (starting with .)
            if item.name.startswith('.'):
                continue
            # Skip system directories
            if sys.platform == "win32" and item.name in ('$Recycle.Bin', 'System Volume Information', 'Recovery'):
                continue

            is_dir = item.is_dir()
            has_children = False
            if is_dir:
                try:
                    has_children = any(c.is_dir() for c in item.iterdir() if not c.name.startswith('.'))
                except PermissionError:
                    pass

            entries.append(DirectoryEntry(
                name=item.name,
                path=str(item),
                is_dir=is_dir,
                has_children=has_children
            ))
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {path}"
        )

    # Parent path (don't go above workspace root in Docker mode)
    parent = str(target.parent) if target.parent != target else None
    if not local_mode and parent:
        try:
            Path(parent).resolve().relative_to(workspace_root)
        except ValueError:
            parent = None

    return BrowseResponse(
        path=str(target),
        parent=parent,
        entries=entries,
        is_local_mode=local_mode
    )



@router.post("/api/v1/system/db-maintenance")
async def run_db_maintenance(token: str = Depends(require_admin)):
    """Run database maintenance on-demand: WAL checkpoint, OPTIMIZE, log cleanup, VACUUM."""
    import time
    start = time.monotonic()
    result = await asyncio.to_thread(database.run_database_maintenance)
    elapsed = time.monotonic() - start
    return {"elapsed_seconds": round(elapsed, 2), **result}
