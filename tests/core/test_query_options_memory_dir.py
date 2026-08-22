"""The native leg's project-memory dir is stamped on every built options object.

Ruling MEM1/MEM3: a native (fluid NativeLoop) session keeps project memory under
``<fluid_projects_dir>/<slug>/memory``, path-slugged exactly like
~/.claude/projects so same-named projects never collide. query_options resolves
it and stamps ``aih_memory_dir``; the harness adapter projects that onto
``SessionOptions.extra["memory_dir"]`` and only a native leg reads it.

Stamped unconditionally — the leg is not settled until the adapter connects —
so these tests assert the value, not a leg.
"""

from pathlib import Path
from unittest.mock import patch

import app.core.query_options as qo
from app.core.jsonl_parser import get_project_dir_name


def _profile(config=None):
    cfg = {"model": "opus", "tools_preset": "claude_code"}
    if config:
        cfg.update(config)
    return {"id": "t", "config": cfg}


def test_memory_dir_is_slugged_from_the_project_path(tmp_path, monkeypatch):
    from app.core.config import settings

    projects_root = tmp_path / "fluid-projects"
    monkeypatch.setattr(settings, "fluid_projects_dir", str(projects_root))
    checkout = tmp_path / "repo"
    checkout.mkdir()

    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=_profile(), project={"id": "p1", "path": str(checkout)}
        )

    slug = get_project_dir_name(str(checkout))
    assert opts.aih_memory_dir is not None
    assert Path(opts.aih_memory_dir) == projects_root / slug / "memory"
    assert opts.aih_memory_dir.endswith(str(Path(slug) / "memory"))


def test_memory_dir_is_stamped_without_a_project(tmp_path, monkeypatch):
    """No project row still yields a dir — it falls back to the session cwd,
    so a workspace-only session can still keep memory."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_projects_dir", str(tmp_path / "fp"))

    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=_profile())

    assert opts.aih_memory_dir is not None
    assert opts.aih_memory_dir.endswith("memory")
    assert str(tmp_path / "fp") in opts.aih_memory_dir
