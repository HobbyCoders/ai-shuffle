"""
Canonical agent export/import with compatibility for legacy profiles/subagents.

Config blobs are preserved verbatim. Version 1 exports remain accepted; when
both legacy collections contain the same ID, the full profile config wins.
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
import json

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File
from pydantic import BaseModel, Field

from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

from app.db import database
from app.api.auth import require_admin

router = APIRouter(prefix="/api/v1/export-import", tags=["Import/Export"])


# ============================================================================
# Export/Import Data Models
# ============================================================================

# Fields to exclude from export (system-managed fields)
SYSTEM_FIELDS = {"is_builtin", "created_at", "updated_at"}


class ExportedSubagent(BaseModel):
    """
    Subagent data for export - uses dynamic dict to capture all fields.
    Only system fields (is_builtin, created_at, updated_at) are excluded.
    """
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[List[Any]] = None
    # Allow extra fields dynamically
    class Config:
        extra = "allow"


class ExportedProfile(BaseModel):
    """
    Profile data for export - uses dynamic dict for config to capture all fields.
    Only system fields (is_builtin, created_at, updated_at) are excluded.
    """
    id: str
    name: str
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    mcp_tools: Optional[List[str]] = None


class ExportData(BaseModel):
    """Complete export data structure"""
    version: str = "1.0"
    export_type: str = Field(..., description="Type of export: 'profiles', 'subagents', or 'all'")
    exported_at: str
    profiles: Optional[List[ExportedProfile]] = None
    subagents: Optional[List[ExportedSubagent]] = None
    agents: Optional[List[ExportedProfile]] = None


class ImportResult(BaseModel):
    """Result of an import operation"""
    success: bool
    agents_imported: int = 0
    agents_skipped: int = 0
    agents_updated: int = 0
    profiles_imported: int = 0
    profiles_skipped: int = 0
    profiles_updated: int = 0
    subagents_imported: int = 0
    subagents_skipped: int = 0
    subagents_updated: int = 0
    errors: List[str] = []
    warnings: List[str] = []


class ImportOptions(BaseModel):
    """Options for import behavior"""
    overwrite_existing: bool = Field(default=False, description="Overwrite existing items with same ID")
    skip_existing: bool = Field(default=True, description="Skip items that already exist (ignored if overwrite_existing is True)")


# ============================================================================
# Export Endpoints
# ============================================================================

def _export_profile(p: dict) -> ExportedProfile:
    """Helper to export a single profile, excluding system fields"""
    config = p.get("config", {})
    # Config is already a dict, just pass it through (all fields preserved)
    return ExportedProfile(
        id=p["id"],
        name=p["name"],
        description=p.get("description"),
        config=config,
        mcp_tools=p.get("mcp_tools")
    )


def _export_subagent(s: dict) -> dict:
    """Helper to export a single subagent, excluding system fields"""
    # Create a copy excluding system fields
    exported = {k: v for k, v in s.items() if k not in SYSTEM_FIELDS}
    return exported


@router.get("/profiles", response_model=ExportData)
async def export_profiles(token: str = Depends(require_admin)):
    """Export all profiles as JSON (all config fields are preserved dynamically)"""
    profiles = await run_sync(database.get_all_profiles)

    exported_profiles = [_export_profile(p) for p in profiles]

    return ExportData(
        version="1.0",
        export_type="profiles",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        profiles=exported_profiles
    )


@router.get("/profiles/{profile_id}", response_model=ExportData)
async def export_single_profile(profile_id: str, token: str = Depends(require_admin)):
    """Export a single profile as JSON (all config fields are preserved dynamically)"""
    profile = await run_sync(database.get_profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    return ExportData(
        version="1.0",
        export_type="profiles",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        profiles=[_export_profile(profile)]
    )


@router.get("/subagents")
async def export_subagents(token: str = Depends(require_admin)):
    """Export all subagents as JSON (all fields are preserved dynamically)"""
    subagents = await run_sync(database.get_all_subagents)

    exported_subagents = [_export_subagent(s) for s in subagents]

    return {
        "version": "1.0",
        "export_type": "subagents",
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subagents": exported_subagents
    }


@router.get("/subagents/{subagent_id}")
async def export_single_subagent(subagent_id: str, token: str = Depends(require_admin)):
    """Export a single subagent as JSON (all fields are preserved dynamically)"""
    subagent = await run_sync(database.get_subagent, subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    return {
        "version": "1.0",
        "export_type": "subagents",
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subagents": [_export_subagent(subagent)]
    }


@router.get("/all")
async def export_all(token: str = Depends(require_admin)):
    """Export all profiles and subagents as JSON (all fields are preserved dynamically)"""
    # Two contiguous listings — one hop, not two.
    def _load():
        return database.get_all_profiles(), database.get_all_subagents()

    profiles, subagents = await run_sync(_load)

    exported_profiles = [_export_profile(p) for p in profiles]
    exported_subagents = [_export_subagent(s) for s in subagents]

    return {
        "version": "1.0",
        "export_type": "all",
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "profiles": [p.model_dump() for p in exported_profiles],
        "subagents": exported_subagents
    }


@router.get("/agents", response_model=ExportData)
async def export_agents(token: str = Depends(require_admin)):
    """Export unified agents once, retaining config and MCP tool selection."""
    agents = await run_sync(database.get_all_profiles)
    return ExportData(
        version="2.0", export_type="agents",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        agents=[_export_profile(agent) for agent in agents],
    )


def _import_payload(data: dict, overwrite_existing: bool) -> ImportResult:
    """Apply either export format. Called off-loop by both transport endpoints."""
    result = ImportResult(success=True)
    profiles = data.get("profiles") or []
    agents = data.get("agents") or []
    subagents = data.get("subagents") or []
    for collection in (profiles, agents, subagents):
        if not isinstance(collection, list):
            raise HTTPException(status_code=400, detail="Export collections must be lists")

    # Since v71 all three shapes identify the same table. Full configs take
    # precedence over lossy subagent projections before any writes occur.
    full_ids = {item.get("id") for item in profiles + agents if isinstance(item, dict)}
    canonical_ids = {item.get("id") for item in agents if isinstance(item, dict)}
    existing_agents = database.get_all_subagents()
    valid_ids = {row["id"] for row in existing_agents} | {
        item.get("id") for item in subagents + profiles + agents
        if isinstance(item, dict) and item.get("id")
    }
    seen = set()
    written_profiles: list[tuple[str, dict]] = []
    for kind, collection in (("subagents", subagents), ("profiles", profiles), ("agents", agents)):
        for item in collection:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not item_id or not isinstance(item_id, str):
                result.errors.append(f"{kind.rstrip('s').capitalize()} missing required 'id' field")
                continue
            if (kind == "subagents" and item_id in full_ids) or (kind == "profiles" and item_id in canonical_ids):
                result.warnings.append(f"Ignored legacy projection for '{item_id}'; using the full agent config")
                continue
            if item_id in seen:
                result.warnings.append(f"Duplicate agent '{item_id}' ignored")
                continue
            seen.add(item_id)
            try:
                legacy = kind == "subagents"
                existing = database.get_subagent(item_id) if legacy else database.get_profile(item_id)
                if existing and not overwrite_existing:
                    setattr(result, f"{kind}_skipped", getattr(result, f"{kind}_skipped") + 1)
                    result.warnings.append(f"Agent '{item_id}' already exists, skipped")
                    continue
                fields = {"name": item.get("name", item_id), "description": item.get("description", "" if legacy else None)}
                if legacy:
                    # Only keys the file carries are written: update_subagent
                    # treats an omitted field as "keep" and None as "clear", so
                    # an older export must not blank fields it never knew about.
                    fields.update({key: item[key] for key in ("prompt", "tools", "model", "skills", "memory", "mcp_servers") if key in item})
                    if not existing or "prompt" in fields:
                        fields["prompt"] = fields.get("prompt") or ""
                    if existing:
                        database.update_subagent(subagent_id=item_id, **fields)
                    else:
                        database.create_subagent(subagent_id=item_id, is_builtin=False, **fields)
                else:
                    # Copy before filtering old dangling references; never mutate
                    # the uploaded object or drop a forward reference in this file.
                    config = dict(item.get("config") or {})
                    enabled = config.get("enabled_agents")
                    if isinstance(enabled, list):
                        missing = [aid for aid in enabled if aid not in valid_ids]
                        if missing:
                            result.warnings.append(f"Profile '{item_id}': removed {len(missing)} subagent(s) not found in environment: {', '.join(missing)}")
                            config["enabled_agents"] = [aid for aid in enabled if aid in valid_ids]
                    fields["config"] = config
                    if item.get("mcp_tools") is not None:
                        fields["mcp_tools"] = item["mcp_tools"]
                    if existing:
                        database.update_profile(profile_id=item_id, allow_builtin=True, **fields)
                    else:
                        database.create_profile(profile_id=item_id, is_builtin=False, **fields)
                    written_profiles.append((item_id, config))
                action = "updated" if existing else "imported"
                setattr(result, f"{kind}_{action}", getattr(result, f"{kind}_{action}") + 1)
            except Exception as exc:
                # A forward reference to this id was trusted above; withdraw it.
                valid_ids.discard(item_id)
                result.errors.append(f"Failed to import {kind.rstrip('s')} '{item_id}': {exc}")
    # A profile written before a later agent in the file failed still points at
    # that agent. Strip those references the same way missing ones are stripped.
    for profile_id, config in written_profiles:
        enabled = config.get("enabled_agents")
        if not isinstance(enabled, list):
            continue
        failed = [aid for aid in enabled if aid not in valid_ids]
        if failed:
            result.warnings.append(f"Profile '{profile_id}': removed {len(failed)} subagent(s) that failed to import: {', '.join(failed)}")
            database.update_profile(profile_id=profile_id, allow_builtin=True, config={**config, "enabled_agents": [aid for aid in enabled if aid in valid_ids]})
    result.success = not result.errors
    return result


def _validate_export(data: Any) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid export file format: expected JSON object")
    version = data.get("version", "1.0")
    if version not in ("1.0", "2.0"):
        raise HTTPException(status_code=400, detail=f"Unsupported export version: {version}")
    return data


@router.post("/import", response_model=ImportResult)
async def import_data(
    file: UploadFile = File(...),
    overwrite_existing: bool = False,
    token: str = Depends(require_admin),
):
    """Import a canonical agent export or a legacy profile/subagent JSON file."""
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {exc}")
    except Exception:
        logger.exception("Failed to read import file")
        raise HTTPException(status_code=400, detail="Failed to read file")
    return await run_sync(_import_payload, _validate_export(data), overwrite_existing)


@router.post("/import/json", response_model=ImportResult)
async def import_data_json(
    data: ExportData,
    overwrite_existing: bool = False,
    token: str = Depends(require_admin),
):
    """JSON-body equivalent of the file import, with identical merge rules."""
    payload = _validate_export(data.model_dump(exclude_none=True))
    return await run_sync(_import_payload, payload, overwrite_existing)
