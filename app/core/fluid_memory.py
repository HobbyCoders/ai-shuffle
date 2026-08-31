"""Shared access to the fluid continual store (skills, journal, memories).

One resolver, two consumers: the native leg's engine build
(``app/core/provider/harness_adapter.py``) and the delegated leg's memory
bridge (``app/mcp/builtin_tools.py``) must open the SAME store — a second
root would mean two memories that silently diverge. See
docs/MEMORY_ARCHITECTURE.md for the full map of what lives here.
"""

from __future__ import annotations

from pathlib import Path


def skills_root() -> Path:
    """The continual store's home: saved skills, the journal, memories.json.

    ``fluid_skills_dir`` relocates it; unset, it lives under the app data dir
    (never fluid's ``~/.fluid`` default).
    """
    from app.core.config import settings

    return (
        Path(settings.fluid_skills_dir)
        if settings.fluid_skills_dir
        else settings.effective_data_dir / "fluid" / "skills"
    )
