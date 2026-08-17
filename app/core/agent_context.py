"""Which DB agents a profile declares into its model's CONTEXT.

2026-08-17 ruling (supersedes A10): ``enabled_agents`` and ``agent_invoke_mode``
are a context-loading concern, nothing more. They decide which agent
definitions are advertised to the model in the compiled prompt's agents index —
they never gate dispatch. ``app.core.task_dispatch`` will run ANY resolvable
agent (a row in ``agents``, or a file/plugin definition fluid discovered) by
name, declared or not, so a model that already knows a name can always delegate
to it.

Mapping, from the calling profile's config:

* ``agent_invoke_mode == "all"``     → every DB agent is declared;
* ``agent_invoke_mode == "custom"``  → exactly the ids in ``enabled_agents``
  (an empty/missing list declares nobody);
* no mode set (legacy rows)          → a non-empty ``enabled_agents`` declares
  those ids, an empty/absent one declares everything (the old "empty means
  unrestricted" semantics, read as "empty means declare all").

What is declared is NAME + DESCRIPTION only (ruling CE6's index shape): the
agent's own system prompt, tools and model are resolved at dispatch time, never
serialized into the caller's context.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def declared_agent_ids(config: Optional[Dict[str, Any]]) -> Optional[List[str]]:
    """The agent ids a profile declares into context; ``None`` means "all"."""
    config = config or {}
    mode = config.get("agent_invoke_mode")
    raw = config.get("enabled_agents")
    listed = (
        [str(a).strip() for a in raw if str(a).strip()]
        if isinstance(raw, (list, tuple))
        else None
    )
    if mode == "all":
        return None
    if mode == "custom":
        return listed or []
    # Legacy rows: a non-empty list declares those ids, anything else = all.
    return listed or None


def declared_agents(
    config: Optional[Dict[str, Any]], *, exclude_id: Optional[str] = None
) -> List[Dict[str, str]]:
    """``[{"name", "description"}]`` for the agents this profile declares.

    Ordered by name so the block feeds a stable (cache-friendly) prompt prefix.
    ``exclude_id`` drops the calling agent itself — an agent listing itself as a
    delegation target is noise, and the dispatcher refuses the cycle anyway.
    Never raises: a DB failure degrades to "declare nothing".
    """
    ids = declared_agent_ids(config)
    if ids is not None and not ids:
        return []
    try:
        from app.db import database

        rows = database.get_all_profiles()
    except Exception:  # noqa: BLE001 — declaration is best-effort context
        logger.warning("could not read agents for context declaration", exc_info=True)
        return []

    if ids is None:
        selected = list(rows)
    else:
        by_id = {row["id"]: row for row in rows}
        selected = [by_id[agent_id] for agent_id in ids if agent_id in by_id]

    out: List[Dict[str, str]] = []
    for row in selected:
        if exclude_id and row.get("id") == exclude_id:
            continue
        out.append({
            "name": str(row.get("name") or row.get("id") or "").strip(),
            "description": str(row.get("description") or "").strip(),
        })
    out = [entry for entry in out if entry["name"]]
    out.sort(key=lambda entry: entry["name"].lower())
    return out
