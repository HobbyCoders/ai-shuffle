"""
Import/Export API routes for profiles and subagents

This module uses DYNAMIC export/import - all fields from the database are
automatically included without needing to update models when new fields are added.
Only system fields (is_builtin, created_at, updated_at) are excluded from export.
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
import json

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File
from pydantic import BaseModel, Field

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
    config: Dict[str, Any] = Field(default_factory=dict)  # Dynamic config - all fields preserved


class ExportData(BaseModel):
    """Complete export data structure"""
    version: str = "1.0"
    export_type: str = Field(..., description="Type of export: 'profiles', 'subagents', or 'all'")
    exported_at: str
    profiles: Optional[List[ExportedProfile]] = None
    subagents: Optional[List[ExportedSubagent]] = None


class ImportResult(BaseModel):
    """Result of an import operation"""
    success: bool
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
        config=config  # Dynamic - all config fields are preserved
    )


def _export_subagent(s: dict) -> dict:
    """Helper to export a single subagent, excluding system fields"""
    # Create a copy excluding system fields
    exported = {k: v for k, v in s.items() if k not in SYSTEM_FIELDS}
    return exported


@router.get("/profiles", response_model=ExportData)
async def export_profiles(token: str = Depends(require_admin)):
    """Export all profiles as JSON (all config fields are preserved dynamically)"""
    profiles = database.get_all_profiles()

    exported_profiles = [_export_profile(p) for p in profiles]

    return ExportData(
        version="1.0",
        export_type="profiles",
        exported_at=datetime.utcnow().isoformat() + "Z",
        profiles=exported_profiles
    )


@router.get("/profiles/{profile_id}", response_model=ExportData)
async def export_single_profile(profile_id: str, token: str = Depends(require_admin)):
    """Export a single profile as JSON (all config fields are preserved dynamically)"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    return ExportData(
        version="1.0",
        export_type="profiles",
        exported_at=datetime.utcnow().isoformat() + "Z",
        profiles=[_export_profile(profile)]
    )


@router.get("/subagents")
async def export_subagents(token: str = Depends(require_admin)):
    """Export all subagents as JSON (all fields are preserved dynamically)"""
    subagents = database.get_all_subagents()

    exported_subagents = [_export_subagent(s) for s in subagents]

    return {
        "version": "1.0",
        "export_type": "subagents",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "subagents": exported_subagents
    }


@router.get("/subagents/{subagent_id}")
async def export_single_subagent(subagent_id: str, token: str = Depends(require_admin)):
    """Export a single subagent as JSON (all fields are preserved dynamically)"""
    subagent = database.get_subagent(subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    return {
        "version": "1.0",
        "export_type": "subagents",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "subagents": [_export_subagent(subagent)]
    }


@router.get("/all")
async def export_all(token: str = Depends(require_admin)):
    """Export all profiles and subagents as JSON (all fields are preserved dynamically)"""
    profiles = database.get_all_profiles()
    subagents = database.get_all_subagents()

    exported_profiles = [_export_profile(p) for p in profiles]
    exported_subagents = [_export_subagent(s) for s in subagents]

    return {
        "version": "1.0",
        "export_type": "all",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "profiles": [p.model_dump() for p in exported_profiles],
        "subagents": exported_subagents
    }


# ============================================================================
# Import Endpoints
# ============================================================================

@router.post("/import", response_model=ImportResult)
async def import_data(
    file: UploadFile = File(...),
    overwrite_existing: bool = False,
    token: str = Depends(require_admin)
):
    """
    Import profiles and/or subagents from a JSON file.

    - **file**: JSON file containing export data
    - **overwrite_existing**: If True, overwrite existing items with same ID
    """
    # Read and parse file
    try:
        content = await file.read()
        data = json.loads(content.decode('utf-8'))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON file: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {str(e)}"
        )

    # Validate structure
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export file format: expected JSON object"
        )

    version = data.get("version", "1.0")
    if version != "1.0":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported export version: {version}"
        )

    result = ImportResult(success=True)

    # Import subagents first (profiles may reference them)
    subagents = data.get("subagents", [])
    if subagents:
        for subagent_data in subagents:
            try:
                subagent_id = subagent_data.get("id")
                if not subagent_id:
                    result.errors.append("Subagent missing required 'id' field")
                    continue

                # Check if exists
                existing = database.get_subagent(subagent_id)
                if existing:
                    if overwrite_existing:
                        database.update_subagent(
                            subagent_id=subagent_id,
                            name=subagent_data.get("name"),
                            description=subagent_data.get("description"),
                            prompt=subagent_data.get("prompt"),
                            tools=subagent_data.get("tools"),
                            model=subagent_data.get("model")
                        )
                        result.subagents_updated += 1
                    else:
                        result.subagents_skipped += 1
                        result.warnings.append(f"Subagent '{subagent_id}' already exists, skipped")
                else:
                    database.create_subagent(
                        subagent_id=subagent_id,
                        name=subagent_data.get("name", subagent_id),
                        description=subagent_data.get("description", ""),
                        prompt=subagent_data.get("prompt", ""),
                        tools=subagent_data.get("tools"),
                        model=subagent_data.get("model"),
                        is_builtin=False
                    )
                    result.subagents_imported += 1
            except Exception as e:
                result.errors.append(f"Failed to import subagent '{subagent_data.get('id', 'unknown')}': {str(e)}")

    # Import profiles
    profiles = data.get("profiles", [])
    if profiles:
        for profile_data in profiles:
            try:
                profile_id = profile_data.get("id")
                if not profile_id:
                    result.errors.append("Profile missing required 'id' field")
                    continue

                # Check if exists
                existing = database.get_profile(profile_id)
                config = profile_data.get("config", {})

                if existing:
                    if overwrite_existing:
                        database.update_profile(
                            profile_id=profile_id,
                            name=profile_data.get("name"),
                            description=profile_data.get("description"),
                            config=config,
                            allow_builtin=True
                        )
                        result.profiles_updated += 1
                    else:
                        result.profiles_skipped += 1
                        result.warnings.append(f"Profile '{profile_id}' already exists, skipped")
                else:
                    database.create_profile(
                        profile_id=profile_id,
                        name=profile_data.get("name", profile_id),
                        description=profile_data.get("description"),
                        config=config,
                        is_builtin=False
                    )
                    result.profiles_imported += 1
            except Exception as e:
                result.errors.append(f"Failed to import profile '{profile_data.get('id', 'unknown')}': {str(e)}")

    # Set success to False if there were any errors
    if result.errors:
        result.success = False

    return result


@router.post("/import/json", response_model=ImportResult)
async def import_data_json(
    data: ExportData,
    overwrite_existing: bool = False,
    token: str = Depends(require_admin)
):
    """
    Import profiles and/or subagents from JSON body (alternative to file upload).

    - **data**: ExportData object containing profiles and/or subagents
    - **overwrite_existing**: If True, overwrite existing items with same ID
    """
    result = ImportResult(success=True)

    # Import subagents first (profiles may reference them)
    if data.subagents:
        for subagent in data.subagents:
            try:
                existing = database.get_subagent(subagent.id)
                if existing:
                    if overwrite_existing:
                        database.update_subagent(
                            subagent_id=subagent.id,
                            name=subagent.name,
                            description=subagent.description,
                            prompt=subagent.prompt,
                            tools=subagent.tools,
                            model=subagent.model
                        )
                        result.subagents_updated += 1
                    else:
                        result.subagents_skipped += 1
                        result.warnings.append(f"Subagent '{subagent.id}' already exists, skipped")
                else:
                    database.create_subagent(
                        subagent_id=subagent.id,
                        name=subagent.name,
                        description=subagent.description,
                        prompt=subagent.prompt,
                        tools=subagent.tools,
                        model=subagent.model,
                        is_builtin=False
                    )
                    result.subagents_imported += 1
            except Exception as e:
                result.errors.append(f"Failed to import subagent '{subagent.id}': {str(e)}")

    # Import profiles
    if data.profiles:
        for profile in data.profiles:
            try:
                existing = database.get_profile(profile.id)
                # config is already a Dict[str, Any], no conversion needed
                config = profile.config

                if existing:
                    if overwrite_existing:
                        database.update_profile(
                            profile_id=profile.id,
                            name=profile.name,
                            description=profile.description,
                            config=config,
                            allow_builtin=True
                        )
                        result.profiles_updated += 1
                    else:
                        result.profiles_skipped += 1
                        result.warnings.append(f"Profile '{profile.id}' already exists, skipped")
                else:
                    database.create_profile(
                        profile_id=profile.id,
                        name=profile.name,
                        description=profile.description,
                        config=config,
                        is_builtin=False
                    )
                    result.profiles_imported += 1
            except Exception as e:
                result.errors.append(f"Failed to import profile '{profile.id}': {str(e)}")

    # Set success to False if there were any errors
    if result.errors:
        result.success = False

    return result
