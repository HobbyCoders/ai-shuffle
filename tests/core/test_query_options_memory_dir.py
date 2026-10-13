"""The old ``aih_memory_dir`` stamping is gone — and stays gone.

Until 2026-08-31 query_options resolved a per-project memory dir (rulings
MEM1/MEM3) and stamped it as ``aih_memory_dir``, which the native leg turned
into a "# Project memory" prompt section. The section's write path — the
Memory tool — was removed in PR #997, leaving the prompt promising a memory
nothing could write, so the section and its plumbing were removed (see
docs/MEMORY_ARCHITECTURE.md). Durable facts travel through the continual
learner instead. This file pins the removal: options must not resurrect the
attribute, and building options must not create memory dirs as a side effect.
"""

from unittest.mock import patch

import app.core.query_options as qo
from tests.core.helpers import query_profile


def test_memory_dir_is_not_stamped(tmp_path, monkeypatch):
    from app.core.config import settings

    projects_root = tmp_path / "fluid-projects"
    monkeypatch.setattr(settings, "fluid_projects_dir", str(projects_root))
    checkout = tmp_path / "repo"
    checkout.mkdir()

    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile(), project={"id": "p1", "path": str(checkout)}
        )

    assert getattr(opts, "aih_memory_dir", None) is None


def test_building_options_creates_no_memory_dir(tmp_path, monkeypatch):
    """The dir used to be created eagerly because the prompt promised it
    existed. No prompt section, no promise — building options must not touch
    the filesystem for memory."""
    from app.core.config import settings

    projects_root = tmp_path / "fluid-projects"
    monkeypatch.setattr(settings, "fluid_projects_dir", str(projects_root))
    checkout = tmp_path / "brand-new-repo"
    checkout.mkdir()

    with patch.object(qo, "get_security_instructions", return_value=""):
        qo.build_options_from_profile(
            profile=query_profile(), project={"id": "p1", "path": str(checkout)}
        )

    assert not projects_root.exists()
