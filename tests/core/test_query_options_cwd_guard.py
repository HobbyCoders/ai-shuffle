"""
Regression tests: build_options_from_profile fails fast on a nonexistent cwd.

An invalid working directory otherwise survives until the SDK spawns the CLI
subprocess, where it dies as an opaque OS-level error (WinError 267 "The
directory name is invalid" on Windows). Shared-DB installs can carry project
rows whose paths only exist on another machine, so the resolved path must be
validated up front with an error that names the path and its source.

The default workspace directory is the app's own fallback and is created on
demand instead of erroring.

build_options_from_profile makes one unavoidable DB call
(get_security_instructions); we patch that single boundary.
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo
from tests.core.helpers import query_profile


def test_nonexistent_project_path_raises_with_path_and_name(tmp_path):
    missing = tmp_path / "gone-project"
    project = {"id": "p1", "name": "other-install", "path": str(missing)}
    with patch.object(qo, "get_security_instructions", return_value=""):
        with pytest.raises(ValueError) as exc_info:
            qo.build_options_from_profile(profile=query_profile(), project=project)
    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "other-install" in msg


def test_nonexistent_profile_config_cwd_raises(tmp_path):
    missing = tmp_path / "gone-cwd"
    with patch.object(qo, "get_security_instructions", return_value=""):
        with pytest.raises(ValueError) as exc_info:
            qo.build_options_from_profile(profile=query_profile({"cwd": str(missing)}))
    assert str(missing) in str(exc_info.value)


def test_valid_profile_config_cwd_accepted(tmp_path):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=query_profile({"cwd": str(tmp_path)}))
    assert str(opts.cwd) == str(tmp_path)


def test_missing_default_workspace_is_created(tmp_path, monkeypatch):
    ws = tmp_path / "fresh-workspace"
    assert not ws.exists()
    monkeypatch.setattr(qo.settings, "workspace_dir", ws)
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=query_profile())
    assert ws.is_dir()
    assert str(opts.cwd) == str(ws)


# ---------------------------------------------------------------------------
# The SPAWN cwd is resolved through the origin-keyed project_resolver for EVERY
# session (resolver-FIRST, no project_v2_id gate). resolve_cwd MATERIALIZES (git
# worktree add) here — this is the spawn path, unlike the pure-read
# message_resolver seam. There is no legacy worktree/project ladder anymore: when
# the resolver yields None the outcome is decided purely by whether the session
# resolves to a project_v2 (honest refusal) or not (config/workspace parity).
# ---------------------------------------------------------------------------


def test_session_spawn_cwd_from_resolver_base(tmp_path):
    """A session (no branch) → opts.cwd is the resolver's base checkout path,
    resolver-first with no gate."""
    base = tmp_path / "checkouts" / "proj" / "base"
    base.mkdir(parents=True)
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": None}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=str(base)
    ) as rc:
        opts, _ = qo.build_options_from_profile(
            profile=query_profile(), session_id="ses-1"
        )

    assert str(opts.cwd) == str(base)
    rc.assert_called_once_with(session)


def test_session_spawn_cwd_from_resolver_branch(tmp_path):
    """A branch-bound session → resolver's branch checkout path wins and
    base_branch is enriched from the legacy worktree row."""
    feat = tmp_path / "checkouts" / "proj" / "feat-x"
    feat.mkdir(parents=True)
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": "feat/x"}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch.object(
        qo.database, "get_worktree_by_session", return_value={"base_branch": "develop"}
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=str(feat)
    ) as rc:
        opts, _ = qo.build_options_from_profile(
            profile=query_profile(), session_id="ses-1"
        )

    assert str(opts.cwd) == str(feat)
    rc.assert_called_once_with(session)


def test_v2_project_not_materialized_here_raises(tmp_path):
    """resolve_cwd None but the session HAS a project_v2 (its checkout lives only
    on another runner) → refuse with an actionable error instead of running in the
    wrong place. No legacy ladder to fall through to."""
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": None}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2",
        return_value={"id": "pv2-1", "name": "MyRepo", "origin": "git:abc"},
    ):
        with pytest.raises(ValueError) as exc_info:
            qo.build_options_from_profile(profile=query_profile(), session_id="ses-1")
    assert "not checked out on this device" in str(exc_info.value)


def test_v2_worktree_not_materialized_here_raises_worktree_error(tmp_path):
    """A worktree-flagged session whose checkout isn't here raises
    WorktreeUnavailableError carrying the branch (the query engines convert it to
    a user-visible event rather than running in main)."""
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": "feat/x"}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2",
        return_value={"id": "pv2-1", "name": "MyRepo"},
    ):
        with pytest.raises(qo.WorktreeUnavailableError) as exc_info:
            qo.build_options_from_profile(profile=query_profile(), session_id="ses-1")
    assert exc_info.value.branch_name == "feat/x"
    assert "no longer exists" in str(exc_info.value)


def test_v2_worktree_on_wrong_branch_names_the_branch_instead_of_missing():
    """#1606: a bound checkout folder that exists on another branch is still
    refused, but the error names that branch instead of "no longer exists"."""
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": "feat/x"}
    cause = "its folder /w/feat-x exists but is on branch 'fix/y', not this session's branch 'feat/x'"

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2",
        return_value={"id": "pv2-1", "name": "MyRepo"},
    ), patch.object(
        qo, "_claim_and_run_pending_checkout", return_value=False
    ), patch(
        "app.core.project_resolver.describe_checkout_mismatch", return_value=cause
    ):
        with pytest.raises(qo.WorktreeUnavailableError) as exc_info:
            qo.build_options_from_profile(profile=query_profile(), session_id="ses-1")
    message = str(exc_info.value)
    assert cause in message
    assert "no longer exists" not in message
    assert "Refusing to run on the wrong branch" in message
    assert exc_info.value.branch_name == "feat/x"


def test_orphan_session_falls_to_workspace(tmp_path, monkeypatch):
    """A session with no project_v2 and no live legacy project (resolve_cwd None,
    resolve_project_v2 None) → default workspace (legacy parity)."""
    ws = tmp_path / "ws"
    monkeypatch.setattr(qo.settings, "workspace_dir", ws)
    session = {"id": "ses-1", "project_v2_id": None, "branch": None}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2", return_value=None
    ):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile(), session_id="ses-1"
        )

    assert str(opts.cwd) == str(ws)


def test_orphan_session_uses_profile_config_cwd(tmp_path):
    """An orphan session with a profile-config cwd → that cwd (legacy parity)."""
    cfg_dir = tmp_path / "cfgcwd"
    cfg_dir.mkdir()
    session = {"id": "ses-1", "project_v2_id": None, "branch": None}

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2", return_value=None
    ):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile({"cwd": str(cfg_dir)}), session_id="ses-1"
        )

    assert str(opts.cwd) == str(cfg_dir)


def test_sync_builder_lost_claim_does_not_wait(tmp_path):
    """Rescue + post-resolve stay no-wait; one async preflight owns the budget."""
    session = {"id": "ses-1", "project_v2_id": "pv2-1", "branch": "feat/x"}
    slept: list = []

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch(
        "app.core.project_resolver.resolve_cwd", return_value=None
    ), patch(
        "app.core.project_resolver.resolve_project_v2",
        return_value={"id": "pv2-1", "name": "MyRepo"},
    ), patch.object(
        qo, "_claim_and_run_pending_checkout", return_value=False
    ) as claim, patch(
        "time.sleep", side_effect=lambda s: slept.append(s)
    ):
        with pytest.raises(qo.WorktreeUnavailableError):
            qo.build_options_from_profile(profile=query_profile(), session_id="ses-1")

    assert slept == []
    assert claim.called
