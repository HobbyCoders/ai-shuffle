"""Preview and apply explicit provider-settings imports into Fluid.

Provider homes are inert until an administrator applies a fresh preview. Fluid
owns filesystem validation and copy semantics; this host layer adds source-mount
policy, stale-plan hashing, presentation, and AI Shuffle project registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.platform import is_local_mode
from app.db import database

DEFAULT_PROVIDERS = ("codex", "claude")
SUPPORTED_PROVIDERS = frozenset(DEFAULT_PROVIDERS)


class InvalidImportSource(ValueError):
    pass


class InvalidImportSelection(ValueError):
    pass


class StaleImportPlan(ValueError):
    pass


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _source_home(requested: str | None) -> Path:
    configured_default = os.environ.get(
        "SETTINGS_IMPORT_SOURCE_HOME",
        os.environ.get("CLAUDE_IMPORT_SOURCE_HOME", str(Path.home())),
    )
    default = _resolved(configured_default)
    source = _resolved(requested) if requested else default
    if not source.is_dir():
        raise InvalidImportSource(f"Settings import home does not exist: {source}")
    if is_local_mode():
        return source

    allowed = {_resolved(Path.home()), default}
    configured = os.environ.get("SETTINGS_IMPORT_ALLOWED_HOMES", "")
    allowed.update(_resolved(value) for value in configured.split(os.pathsep) if value.strip())
    if source not in allowed:
        raise InvalidImportSource(
            "Docker imports are limited to the configured read-only settings import mount"
        )
    return source


def _destination_home() -> Path:
    # This is the home that CONTAINS .fluid, not the .fluid directory itself.
    return _resolved(os.environ.get("SETTINGS_IMPORT_DESTINATION_HOME", str(Path.home())))


def _provider_order(providers: Sequence[str] | None) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(providers or DEFAULT_PROVIDERS))
    unknown = set(selected) - SUPPORTED_PROVIDERS
    if unknown:
        raise InvalidImportSource(f"Unsupported settings provider: {min(unknown)}")
    return selected


def _project_path_for_row(row: dict[str, Any]) -> Path:
    path = Path(row["path"])
    return path.resolve() if path.is_absolute() else (settings.workspace_dir / path).resolve()


def _project_status(path: Path, existing: set[Path], claimed: set[Path]) -> tuple[str, str]:
    resolved = path.resolve()
    if not resolved.is_dir():
        return "diagnostic", "Configured project directory does not exist"
    if not is_local_mode():
        try:
            resolved.relative_to(settings.workspace_dir.resolve())
        except ValueError:
            return "diagnostic", "Project is outside the mounted Docker workspace"
    if resolved in existing:
        return "conflict", "Project is already registered; it will not be replaced"
    if resolved in claimed:
        return "duplicate", "Another selected provider already lists this project"
    claimed.add(resolved)
    return "ready", "Register this existing directory as an AI Shuffle project"


def _serialize_items(plan: Any) -> list[dict[str, Any]]:
    existing = {_project_path_for_row(row) for row in database.get_all_projects()}
    claimed: set[Path] = set()
    items: list[dict[str, Any]] = []
    for item in plan.items:
        source = item.source or (item.source_paths[0] if item.source_paths else "")
        destination = item.destination
        status = item.status
        message = item.message
        project_path = item.provenance.get("project_path") if item.kind == "project_registration" else None
        if project_path and status == "ready":
            source = str(Path(project_path).expanduser().resolve())
            destination = source
            status, message = _project_status(Path(source), existing, claimed)
        items.append({
            "id": item.id,
            "source_system": item.source_system,
            "kind": item.kind,
            "source": source,
            "source_paths": list(item.source_paths),
            "destination": destination,
            "status": status,
            "message": message,
            "is_directory": item.is_directory,
            "source_digest": item.source_digest,
            "provenance": item.provenance,
        })
    return items


def _category(kind: str) -> str:
    if "instruction" in kind:
        return "global_instructions" if "global" in kind else "project_instructions"
    if "skill" in kind:
        return "skills"
    if "agent" in kind:
        return "agents"
    if kind in {"project", "project_registration"}:
        return "configured_projects"
    if "mcp" in kind:
        return "mcp_servers"
    return "settings_plugins_inventory"


def _label(item: dict[str, Any]) -> str:
    source = Path(item["source"]) if item["source"] else Path(item["kind"])
    provider = item["source_system"].title()
    if item["kind"] == "project_registration":
        return f"{provider} project: {source.name or source}"
    return f"{provider}: {source.name}"


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    status = item["status"]
    provider = item["source_system"] if item["source_system"] in SUPPORTED_PROVIDERS else None
    return {
        "id": item["id"],
        "category_id": _category(item["kind"]),
        "label": _label(item),
        "provider": provider,
        "provenance": item["source_system"].title(),
        "description": item["message"] or None,
        "source_display_path": item["source"],
        "destination_display_path": item["destination"],
        "status": status,
        "selectable": status == "ready",
        "selected_by_default": status == "ready",
        "legacy_detected": True,
        "conflict": item["message"] if status == "conflict" else None,
        "diagnostics": ([{
            "severity": "warning" if status == "skipped" else "info",
            "message": item["message"],
            "item_id": item["id"],
        }] if status in {"diagnostic", "skipped"} and item["message"] else []),
    }


def _categories(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    metadata = {
        "global_instructions": ("Global instructions", "Provider instructions imported into Fluid or review sidecars"),
        "project_instructions": ("Project instructions", "Claude/Codex instructions translated with native Fluid precedence"),
        "skills": ("Skills", "Reusable skills imported into Fluid-native roots"),
        "agents": ("Agents", "Provider agent definitions translated into Fluid-native agents"),
        "configured_projects": ("Configured projects", "Existing projects available to register"),
        "mcp_servers": ("MCP servers", "Sanitized server definitions with secret fields omitted"),
        "settings_plugins_inventory": ("Settings and plugins", "Inventory only; unsupported behavior is not activated"),
    }
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in metadata}
    for item in items:
        grouped.setdefault(_category(item["kind"]), []).append(_public_item(item))
    return [
        {"id": key, "label": label, "description": description, "items": grouped[key]}
        for key, (label, description) in metadata.items()
        if grouped[key]
    ]


def _flat_preview(
    *, source_home: str | None, providers: Sequence[str] | None
) -> tuple[dict[str, Any], Any]:
    from fluid.context.settings_import import preview_settings_import

    source = _source_home(source_home)
    destination = _destination_home()
    selected = _provider_order(providers)
    plan = preview_settings_import(
        sources=selected,
        source_home=source,
        destination_home=destination,
    )
    flat = {
        "source_home": str(source),
        "destination_home": str(destination),
        "providers": list(selected),
        "items": _serialize_items(plan),
    }
    return flat, plan


def _plan_token(flat: dict[str, Any]) -> str:
    raw = json.dumps(flat, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def preview_import(
    *, source_home: str | None = None, providers: Sequence[str] | None = None
) -> dict[str, Any]:
    flat, _ = _flat_preview(source_home=source_home, providers=providers)
    token = _plan_token(flat)
    conflicts = [item["message"] for item in flat["items"] if item["status"] == "conflict"]
    diagnostics = [
        {"severity": "info", "message": item["message"], "item_id": item["id"]}
        for item in flat["items"]
        if item["status"] in {"diagnostic", "skipped"} and item["message"]
    ]
    return {
        "token": token,
        "hash": token,
        "source_display_path": flat["source_home"],
        "destination_display_path": str(Path(flat["destination_home"]) / ".fluid"),
        "providers": flat["providers"],
        "legacy_detected": bool(flat["items"]),
        "source_untouched": True,
        "overwrite_existing": False,
        "categories": _categories(flat["items"]),
        "conflicts": conflicts,
        "diagnostics": diagnostics,
        "items": flat["items"],
    }


def _project_slug(path: Path) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "project"
    base = base[:41].rstrip("-")
    suffix = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    return f"{base}-{suffix}"


def _register_project(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    for existing in database.get_all_projects():
        if _project_path_for_row(existing) == resolved:
            return {"path": str(resolved), "status": "conflict", "project_id": existing["id"]}
    if is_local_mode():
        db_path = str(resolved)
    else:
        try:
            db_path = resolved.relative_to(settings.workspace_dir.resolve()).as_posix()
        except ValueError:
            return {"path": str(resolved), "status": "skipped", "message": "outside workspace"}
    project_id = _project_slug(resolved)
    if database.get_project(project_id):
        return {"path": str(resolved), "status": "conflict", "project_id": project_id}
    project = database.create_project(
        project_id=project_id,
        name=resolved.name,
        description="Imported from provider settings",
        path=db_path,
        settings_dict=None,
    )
    return {"path": str(resolved), "status": "imported", "project_id": project["id"]}


def _outcome(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": item["id"],
        "label": _label(item),
        "category_id": _category(item["kind"]),
        "status": item["status"],
        "message": item["message"] or None,
    }


def apply_import(
    *,
    token: str,
    selected_item_ids: list[str],
    source_home: str | None = None,
    providers: Sequence[str] | None = None,
) -> dict[str, Any]:
    from fluid.context.settings_import import apply_settings_import

    flat, plan = _flat_preview(source_home=source_home, providers=providers)
    if not token or token != _plan_token(flat):
        raise StaleImportPlan("Provider settings changed after preview; preview again before importing")

    by_id = {item["id"]: item for item in flat["items"]}
    selected = set(selected_item_ids)
    if selected - set(by_id):
        raise InvalidImportSelection("The selection contains items that were not in the preview")
    if any(by_id[item_id]["status"] != "ready" for item_id in selected):
        raise InvalidImportSelection("Only ready items from the current preview can be imported")

    file_ids = {
        item_id for item_id in selected if by_id[item_id]["kind"] != "project_registration"
    }
    result = apply_settings_import(plan, selected_ids=file_ids)
    result_items = _serialize_items(
        replace(plan, items=tuple(item for item in result.items if item.id in file_ids))
    ) if file_ids else []

    project_items = {
        item_id: by_id[item_id]
        for item_id in selected
        if by_id[item_id]["kind"] == "project_registration"
    }
    imported_projects = [
        _register_project(Path(item["source"])) for item in project_items.values()
    ]

    imported = [_outcome(item) for item in result_items if item["status"] == "imported"]
    skipped = [
        _outcome(item) for item in result_items
        if item["status"] in {"skipped", "diagnostic", "duplicate"}
    ]
    conflicts = [_outcome(item) for item in result_items if item["status"] == "conflict"]
    return {
        "imported": imported,
        "skipped": skipped,
        "conflicts": conflicts,
        "errors": [],
        "imported_projects": imported_projects,
        "source_untouched": True,
        "overwrite_existing": False,
    }
