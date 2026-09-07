"""The <env> block has to say WHERE the session is running.

Session 015c1d77 spent nine minutes and a user interruption starting the app
inside its runner container and pointing the browser tool at that container's
loopback address. The app knew: ``sessions.placement = {"where":
"runner:ai-shuffle-46dd6be2"}``. The prompt did not — it carried working dir,
git, platform, OS, shell and execution mode, and nothing about placement.

These pin the line's three states: present for a runner pin, absent on the
local lane, and regenerated (not stale) when the session is re-placed.
"""

from __future__ import annotations

import pytest

from app.core.environment import (
    PLACEMENT_RUNNER_NOTE,
    generate_environment_details,
)


@pytest.fixture(autouse=True)
def _not_in_a_container(monkeypatch):
    """Detection is a second, independent trigger — pin it off by default so
    these tests read the placement argument alone (the suite itself runs both
    on the user's Windows box and in a Linux container)."""
    monkeypatch.setattr("app.core.environment._in_container", lambda: False)


def _block(tmp_path, placement_where=None):
    return generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/x", "base_branch": "main"},
        include_volatile=False,
        placement_where=placement_where,
    )


def test_runner_placement_names_the_runner_and_the_rule(tmp_path):
    block = _block(tmp_path, "runner:ai-shuffle-4aac1979")
    assert "Runtime placement: runner:ai-shuffle-4aac1979 (shared runner)." in block
    assert PLACEMENT_RUNNER_NOTE in block
    # The point of the line: an app started here is not the app the user sees.
    assert "not one you can start here" in block
    assert "browser tool" in block


def test_local_lane_says_nothing(tmp_path):
    """A session with no explicit placement is the user's own checkout — no
    line, and above all no shared-runner warning on a machine that has none."""
    block = _block(tmp_path)
    assert "Runtime placement" not in block
    assert PLACEMENT_RUNNER_NOTE not in block


def test_elected_placement_is_stated_without_the_warning(tmp_path):
    """``anywhere``/capability placements are elected, not pinned to a shared
    runner: say where the session is, skip the doctrine."""
    block = _block(tmp_path, "anywhere")
    assert "Runtime placement: anywhere" in block
    assert PLACEMENT_RUNNER_NOTE not in block


def test_a_container_triggers_the_note_without_a_placement(tmp_path, monkeypatch):
    """The other half of 015c1d77: the shell was in a container whatever the
    placement row said."""
    monkeypatch.setattr("app.core.environment._in_container", lambda: True)
    block = _block(tmp_path)
    assert "Runtime placement: local (shared runner)." in block
    assert PLACEMENT_RUNNER_NOTE in block


def test_the_line_moves_when_the_session_is_re_placed(tmp_path):
    """4ce681e2 was moved from the Unraid runner onto the user's Windows box
    mid-session. The adopting runner recompiles the block, so the line follows
    the session instead of naming the host it left."""
    before = _block(tmp_path, "runner:ai-shuffle-4aac1979")
    after = _block(tmp_path, "runner:ai-shuffle-46dd6be2")
    assert "runner:ai-shuffle-4aac1979" in before
    assert "runner:ai-shuffle-4aac1979" not in after
    assert "runner:ai-shuffle-46dd6be2" in after


def test_the_line_is_stable_across_turns(tmp_path, monkeypatch):
    """It rides the cached prefix, so it must not move on its own. Same
    placement, a new day, a new branch: byte-identical."""
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-05")
    one = generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/one", "base_branch": "main"},
        include_volatile=False,
        placement_where="runner:ai-shuffle-4aac1979",
    )
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-06")
    two = generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/two", "base_branch": "develop"},
        include_volatile=False,
        placement_where="runner:ai-shuffle-4aac1979",
    )
    assert one == two


def test_placement_is_optional_for_every_existing_caller(tmp_path):
    """The argument is new; a caller that never passes it renders what it
    always did."""
    assert "Runtime placement" not in generate_environment_details(str(tmp_path))


# ─── and it has to be read off the session row ───────────────────────────────


def _system_prompt(placement):
    """``build_options_from_profile`` for a profile that injects <env>, with
    one session row. Only the security-instructions DB call is stubbed (the
    boundary tests/core/test_builtin_browser.py already patches)."""
    from unittest.mock import patch

    import app.core.query_options as qo

    profile = {
        "id": "t",
        "config": {
            "model": "opus",
            "tools_preset": "claude_code",
            "system_prompt": {
                "type": "custom",
                "content": "BASE",
                "inject_env_details": True,
            },
        },
    }
    row = {"id": "sess1", "mode": None, "placement": placement}
    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=row
    ):
        options, _ = qo.build_options_from_profile(profile=profile, session_id="sess1")
    return options.system_prompt or ""


def test_the_placement_on_the_session_row_reaches_the_prompt():
    """JSONB comes back as a dict from Postgres and as a string from the test
    shim; both have to render."""
    prompt = _system_prompt('{"where": "runner:ai-shuffle-4aac1979"}')
    assert "Runtime placement: runner:ai-shuffle-4aac1979 (shared runner)." in prompt
    assert PLACEMENT_RUNNER_NOTE in prompt


def test_a_session_row_without_a_placement_renders_no_line():
    assert "Runtime placement" not in _system_prompt(None)
