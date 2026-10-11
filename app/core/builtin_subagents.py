"""Legacy built-in compatibility and retirement; no subagents ship by default.

The starter team lives in agent_templates and is installed only at the owner's
request. Generic defaults helpers remain for compatibility with stored agents.
"""

import hashlib
import logging
from typing import Dict, Any, List, Optional, Sequence

from app.db import database
from app.db.profiles import RETIRED_BUILTIN_IDS

logger = logging.getLogger(__name__)


# No agents ship by default. Definitions live in the opt-in agent_templates module.
BUILTIN_SUBAGENTS: List[Dict[str, Any]] = []


# ============================================================================
# Shipped-default tracking
# ============================================================================
#
# Start-up seeding refreshes a stored built-in's TEXT (name, description,
# prompt) to the current definition only where the owner never changed it.
# A field counts as "never changed" when its stored value is a default this
# app shipped earlier. Each field is decided on its own: an owner who rewrote
# the prompt keeps that prompt, while the untouched description still moves.
# Tools, model, memory, skills, MCP servers, provider models and roster
# settings are owner choices and are never refreshed here (Restore defaults
# in the Agents page still resets tools and model on request).
#
# Two sources say which defaults were shipped earlier:
#
# 1. ``_PRE_RECORD_DEFAULTS``: SHA-256 fingerprints of every name, description
#    and prompt each built-in had in git history up to and including the
#    revision that introduced this tracking. Rows seeded before then carry no
#    record, so this frozen list is the only way to recognise them. Never
#    extend it.
# 2. The row's own record, stored in its config under
#    ``DEFAULTS_RECORD_KEY`` whenever seeding creates, refreshes or restores
#    the row: the defaults revision that wrote it and the fingerprint of each
#    field that was the shipped default at that revision. The agent editor
#    keeps config keys it does not model (``preserve_profile_presentation``),
#    so an owner edit leaves the record in place, and the mismatching
#    fingerprint marks the edited field as customised.
#
# A row with a valid record is decided by the record alone; the frozen list
# applies only to rows without one. Text an owner set after seeding therefore
# stays even when it equals an older shipped default.
#
# A record from a NEWER revision than this build (a newer worker refreshed the
# row and peer sync brought it here) makes this build leave the whole row
# alone, so two workers at different versions never trade defaults back and
# forth. The refresh itself is never published to peers (see
# ``refresh_builtin_subagent_defaults``): every updated worker refreshes its
# own copy, so a stale replica cannot overwrite a peer's edit.

#: Bump whenever any definition's name, description or prompt changes.
BUILTIN_DEFAULTS_REVISION = 3

#: Config key holding the row's record of the defaults it was seeded from.
DEFAULTS_RECORD_KEY = "builtin_defaults"

#: The definition fields start-up seeding may refresh.
DEFAULT_TEXT_FIELDS = ("name", "description", "prompt")

_PRE_RECORD_DEFAULTS: Dict[str, Dict[str, frozenset]] = {}

def _fingerprint(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def defaults_record(definition: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """The record stored with a row whose text fields hold ``values``.

    Only fields that equal this build's default are fingerprinted; a
    customised field has no entry and is never refreshed through the record.
    """
    return {
        "revision": BUILTIN_DEFAULTS_REVISION,
        "sha256": {
            field: _fingerprint(definition.get(field))
            for field in DEFAULT_TEXT_FIELDS
            if (values.get(field) or "") == (definition.get(field) or "")
        },
    }


def _read_record(record: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    revision, fingerprints = record.get("revision"), record.get("sha256")
    if type(revision) is not int or not isinstance(fingerprints, dict):
        return None
    return {"revision": revision, "sha256": fingerprints}


def plan_default_refresh(
    definition: Dict[str, Any], current: Dict[str, Any], record: Any,
) -> Dict[str, str]:
    """Which text fields of a stored built-in to move to ``definition``.

    ``current`` is the stored row in subagent shape and ``record`` the raw
    value of its config's ``DEFAULTS_RECORD_KEY``. Returns ``{field: new
    value}``, empty when nothing should change. Pure: no queries.
    """
    stored_record = _read_record(record)
    if stored_record and stored_record["revision"] > BUILTIN_DEFAULTS_REVISION:
        # Written by a newer build: its defaults are newer than ours.
        return {}
    shipped = _PRE_RECORD_DEFAULTS.get(definition["id"], {})
    changes: Dict[str, str] = {}
    for field in DEFAULT_TEXT_FIELDS:
        target = definition.get(field) or ""
        stored = current.get(field) or ""
        if stored == target:
            continue
        fingerprint = _fingerprint(stored)
        if stored_record is None:
            if fingerprint in shipped.get(field, ()):
                changes[field] = target
        elif (stored_record["revision"] < BUILTIN_DEFAULTS_REVISION
              and stored_record["sha256"].get(field) == fingerprint):
            # The record alone decides: text that differs from what it
            # fingerprinted was edited after seeding, even when the edit
            # matches an older shipped default.
            changes[field] = target
    return changes


# ============================================================================
# Seeding Functions
# ============================================================================

def _refresh_defaults(subagent_id: str) -> List[str]:
    """The refresh is optional upkeep: a failure keeps the row and start-up."""
    try:
        return database.refresh_builtin_subagent_defaults(subagent_id)
    except Exception:
        logger.warning(f"Could not refresh built-in subagent defaults: {subagent_id}", exc_info=True)
        return []


def seed_builtin_subagents() -> Dict[str, str]:
    """
    Create missing built-in subagents, flag existing rows as built-in, and
    move uncustomised default text on existing built-ins to the current
    definition (``plan_default_refresh``).

    Returns:
        Dict mapping subagent ID to action taken
        ("created", "promoted", "refreshed", "exists", "retired")
    """
    results = {}

    for definition in BUILTIN_SUBAGENTS:
        subagent_id = definition["id"]
        existing = database.get_subagent(subagent_id)

        if not existing:
            database.create_subagent(
                subagent_id=subagent_id,
                name=definition["name"],
                description=definition["description"],
                prompt=definition["prompt"],
                tools=definition.get("tools"),
                model=definition.get("model"),
                is_builtin=True,
                store_defaults=True,
                # Another worker booting on the same database may have just
                # created it; keep that row rather than fail start-up.
                if_absent=True,
            )
            logger.debug(f"Created built-in subagent: {subagent_id}")
            results[subagent_id] = "created"

        elif not existing.get("is_builtin"):
            database.set_subagent_builtin(subagent_id, True)
            logger.debug(f"Promoted to built-in subagent: {subagent_id}")
            results[subagent_id] = "promoted"

        elif _refresh_defaults(subagent_id):
            # Only text still equal to an earlier shipped default moved;
            # customised fields and owner settings are untouched.
            logger.info(f"Refreshed built-in subagent defaults: {subagent_id}")
            results[subagent_id] = "refreshed"

        else:
            results[subagent_id] = "exists"

    # Retire only these removed built-ins. Generic pruning would also delete
    # builtin PROFILE agents that have never been in BUILTIN_SUBAGENTS.
    for subagent_id in RETIRED_BUILTIN_IDS:
        if database.retire_builtin_subagent(subagent_id):
            results[subagent_id] = "retired"

    return results


# ============================================================================
# Read-only classification
# ============================================================================

# Tools that let an agent change the workspace. An agent whose declared tool
# surface holds none of them can only research, which is what makes it
# read-only (see task_dispatch's per-agent default mode).
#
# Bash is deliberately absent: classification uses explicit file-writing
# tools, not shell access, so research-only tool surfaces keep their existing
# dispatch behaviour.
WRITE_CAPABLE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})

# The wildcard an allowlist uses for "every tool" (app/core/tool_access.py).
_ALL_TOOLS = "*"


def is_read_only_tool_surface(tools: Optional[Sequence[Any]]) -> bool:
    """True when ``tools`` is an explicit surface that cannot modify files.

    ``None`` or an empty list means "inherit everything the caller has"
    (ruling CE12) and is never read-only — an unrestricted agent may well
    write. A non-empty list is the agent's ENTIRE tool surface, so it is
    read-only exactly when no entry is write-capable and none is the ``*``
    wildcard.
    """
    names = [str(t).strip().lower() for t in (tools or ()) if str(t).strip()]
    if not names or _ALL_TOOLS in names:
        return False
    return not any(name in WRITE_CAPABLE_TOOLS for name in names)


def get_builtin_subagent_ids() -> List[str]:
    """Get list of all built-in subagent IDs."""
    return [s["id"] for s in BUILTIN_SUBAGENTS]


def get_builtin_subagent_definition(subagent_id: str) -> Optional[Dict[str, Any]]:
    """Get the original definition for a built-in subagent."""
    for s in BUILTIN_SUBAGENTS:
        if s["id"] == subagent_id:
            return s.copy()
    return None



