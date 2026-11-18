"""Remembered approvals never reach further than the user saw.

* A remembered shell allow covers exactly one allowlisted read-only form
  (fluid's ``bash_risk.scoped_form``); any other command is one-shot.
* A credential-file call honours saved denies, but no saved allow answers
  it: each gets a fresh decision (watched, batch-resolved and unattended).
* Shell allows saved before exact forms keep their old breadth (pinned).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.permission_handler import PermissionHandler
from app.core.session_modes import CREDENTIAL_ASK_REASON, mode_approves_without_prompt
from app.db import database, interactions

SID = "shell-remember-session"


async def _prompt(handler, tool, tool_input, request_id="req", **kwargs):
    ready = asyncio.Event()

    async def broadcast(_frame):
        ready.set()

    task = asyncio.create_task(handler.request_permission(
        request_id, SID, "default", tool, tool_input, broadcast, **kwargs,
    ))
    await asyncio.wait_for(ready.wait(), 15)
    return task


async def _answered_at_once(handler, tool, tool_input, **kwargs):
    """The saved-rule answer, or None when the call raised a card."""
    broadcast = AsyncMock()
    task = asyncio.create_task(handler.request_permission(
        "probe", SID, "default", tool, tool_input, broadcast, **kwargs,
    ))
    for _ in range(200):
        if task.done() or broadcast.await_count:
            break
        await asyncio.sleep(0.01)
    if task.done():
        return task.result().behavior
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("remember", ["session", "profile"])
async def test_remembering_git_commit_is_one_shot(remember):
    handler = PermissionHandler()
    task = await _prompt(handler, "Bash", {"command": "git commit -m x"})
    await handler.respond("req", SID, "allow", remember=remember)
    assert (await task).behavior == "allow"
    assert not handler.get_session_rules(SID)
    assert not database.get_permission_rules(profile_id="default")
    assert handler._check_rules(SID, "default", "Bash", {"command": "rm -rf build"}) is None
    assert await _answered_at_once(handler, "Bash", {"command": "git commit -m y"}) is None


@pytest.mark.asyncio
async def test_remembering_an_allowlisted_form_covers_only_that_form():
    handler = PermissionHandler()
    task = await _prompt(handler, "Bash", {"command": "git status"})
    await handler.respond("req", SID, "allow", remember="session")
    await task
    assert [r["tool_pattern"] for r in handler.get_session_rules(SID)] == ["git status"]
    assert handler._check_rules(SID, "default", "Bash", {"command": "git status"})
    # The same words in other ASCII spacing are the same exact form.
    assert handler._check_rules(SID, "default", "Bash", {"command": "git  status"})
    assert handler._check_rules(SID, "default", "Bash", {"command": "git\tstatus"})
    for other in ("git status; rm -rf build", "git status --porcelain", "git log", "GIT STATUS",
                  "git\u00a0status"):
        assert handler._check_rules(SID, "default", "Bash", {"command": other}) is None
    # The match reads the command, never another field.
    assert handler._check_rules(
        SID, "default", "Bash", {"command": "rm -rf build", "description": "git status"},
    ) is None


@pytest.mark.asyncio
async def test_a_peer_written_tool_wide_shell_hint_is_not_installed():
    from app.core import interaction_wait

    handler = PermissionHandler()
    task = await _prompt(handler, "Bash", {"command": "git commit -m x"})
    row = interactions.get_pending_interactions(SID)[0]
    interactions.answer_interaction(row["id"], {
        "decision": "allow", "remember": "session", "pattern": None,
    })
    interaction_wait.notify_changed(SID)
    assert (await task).behavior == "allow"
    assert not handler.get_session_rules(SID)


# ─── credential files ───────────────────────────────────────────────────────

ENV = {"file_path": "/repo/.env", "content": "X=1"}
NOTES = {"file_path": "/repo/notes.md", "content": "hi"}


@pytest.mark.asyncio
async def test_a_remembered_write_does_not_answer_a_credential_write_in_accept_edits():
    """Ask: remember an ordinary Write. Accept Edits: write .env. A card shows."""
    handler = PermissionHandler()
    task = await _prompt(handler, "Write", NOTES)
    await handler.respond("req", SID, "allow", remember="session")
    await task
    assert await _answered_at_once(handler, "Write", NOTES) == "allow"
    # Accept Edits hands the credential write to the handler ...
    assert not mode_approves_without_prompt("acceptEdits", "Write", ENV)
    # ... and the tool-wide remembered allow does not answer it.
    assert await _answered_at_once(handler, "Write", ENV) is None
    # Nor a path the engine resolved to a credential (a symlink, say).
    assert await _answered_at_once(
        handler, "Write", NOTES, decision_reason=CREDENTIAL_ASK_REASON,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern,decision,expected", [
    ("/repo/.env", "allow", None),
    ("/repo/*", "allow", None),
    (None, "allow", None),
    ("/repo/.env", "deny", "deny"),
    (None, "deny", "deny"),
])
async def test_credential_calls_take_saved_denies_only(pattern, decision, expected):
    handler = PermissionHandler()
    handler._add_session_rule_idempotent(SID, "Write", pattern, decision)
    assert await _answered_at_once(handler, "Write", ENV) == expected


@pytest.mark.asyncio
async def test_a_saved_glob_allow_on_the_root_does_not_answer_a_credential_glob():
    handler = PermissionHandler()
    handler._add_session_rule_idempotent(SID, "Glob", "/repo", "allow")
    plain = {"path": "/repo", "pattern": "**/*.md"}
    assert await _answered_at_once(handler, "Glob", plain) == "allow"
    env_glob = {"path": "/repo", "pattern": "**/.env"}  # the engine asked about it
    assert await _answered_at_once(
        handler, "Glob", env_glob, decision_reason=CREDENTIAL_ASK_REASON,
    ) is None


@pytest.mark.parametrize("rule", [
    ("Bash", None),        # "always" for this agent before exact forms
    ("Bash", "git *"),
    ("*", None),           # a wildcard-tool allow
])
def test_shell_allows_saved_before_exact_forms_keep_their_old_breadth(rule):
    """Deliberate: existing approvals are not invalidated, so automations
    that rely on them keep running; only new approvals are exact-form."""
    tool, pattern = rule
    database.add_permission_rule("default", tool, pattern, "allow")
    handler = PermissionHandler()
    decided = handler._check_rules(SID, "default", "Bash", {"command": "git commit -m x"})
    assert decided is not None and decided.decision.value == "allow"


@pytest.mark.asyncio
async def test_remembering_a_write_does_not_batch_answer_a_pending_credential_write():
    handler = PermissionHandler()
    ordinary = await _prompt(handler, "Write", NOTES, request_id="ordinary")
    credential = await _prompt(handler, "Write", ENV, request_id="credential")
    try:
        result = await handler.respond("ordinary", SID, "allow", remember="session")
        assert result["auto_resolved_ids"] == []
        await ordinary
        assert not credential.done()
    finally:
        credential.cancel()
        await asyncio.gather(credential, return_exceptions=True)


@pytest.mark.asyncio
async def test_unattended_credential_write_ignores_a_tool_wide_saved_allow(mock_db):
    import app.core.provider.harness_adapter as ha
    from app.core.permission_handler import permission_handler
    from app.core.provider.types import PermissionRequestEvent, SessionOptions

    permission_handler._add_session_rule_idempotent(SID, "Write", None, "allow")
    try:
        adapter = ha.HarnessAdapter()
        adapter._options = SessionOptions(
            model="opus", permission_mode="acceptEdits",
            extra={"app_session_id": SID, "profile_id": "p-none"},
        )
        adapter._is_delegated = True
        answers = []

        async def respond(tool_use_id, allow, *, message=None, **_kw):
            answers.append((tool_use_id, allow))

        adapter.respond_to_permission = respond
        await adapter._answer_without_approver(
            PermissionRequestEvent(tool_use_id="env", name="Write", input=ENV),
        )
        await adapter._answer_without_approver(
            PermissionRequestEvent(tool_use_id="notes", name="Write", input=NOTES),
        )
        assert answers == [("env", False), ("notes", True)]
    finally:
        permission_handler.clear_session_rules(SID)
