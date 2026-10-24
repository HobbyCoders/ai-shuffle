"""#553: upgrade legacy projectless owners without rewriting identity or history."""
from typing import Any

import pytest

from app.core.session_modes import runtime_session_mode
from app.db import database
from app.db.base import get_db
from app.db.session_mode_migration import migrate


@pytest.mark.parametrize("mode", [None, "code"])
def test_upgrade_confines_only_ordinary_projectless_rows(mock_db: Any, mode: str | None) -> None:
    from app.db import automations, projects_v2

    database.create_api_user("api", "API user", "test-hash")
    database.create_project("project", "Project", None, "/tmp/project")
    project = projects_v2.upsert_project("folder:project-v2", name="Project V2")
    automations.create_automation("automation", "Automation", "cron", {}, "default")
    rows = {
        "owner": {},
        "api": {"api_user_id": "api"},
        "worker": {"is_worker": True},
        "child": {"parent_session_id": "owner"},
        "automation": {"automation_id": "automation"},
        "hidden": {"hidden": True},
        "project": {"project_id": "project"},
        "v2-project": {"project_v2_id": project["id"]},
    }
    for sid, fields in rows.items():
        database.create_session(sid, "default", mode=mode, **fields)
        database.add_session_message(sid, "user", "preserved history")
        # Recreate the literal historical spelling too; create_session stores NULL.
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET mode=%s WHERE id=%s", (mode, sid))
    with get_db() as conn:
        cursor = conn.cursor()
        assert migrate(cursor) == ["sessions.projectless_chat(1)"]
        assert migrate(cursor) == []
    for sid in rows:
        row = database.get_session(sid)
        expected = "chat" if sid == "owner" else "code"
        assert runtime_session_mode(row) == expected
        assert row["mode"] == ("chat" if sid == "owner" else mode)
        assert database.get_session_messages(sid)[0]["content"] == "preserved history"

    # The same scope must hold for a subsequent upsert from an older peer.
    from app.network import engine, policy

    with get_db() as conn:
        for sid in rows:
            result = engine.materialize(
                conn.cursor(), policy.collection("sessions"), sid, {"mode": mode},
            )
            assert result["mode"] == ("chat" if sid == "owner" else mode)


@pytest.mark.parametrize("mode", [None, "code"])
def test_runtime_fences_old_peer_before_next_upgrade(mock_db: Any, mode: str | None) -> None:
    row = database.create_session("old-peer-owner", "default", mode=mode)
    assert row["mode"] is None
    assert runtime_session_mode(row) == "chat"
    assert runtime_session_mode(None) == "code"


def test_migration_keeps_foreign_principals_unchanged(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import preference_identity
    from app.db import mobile_tool_identity

    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "owner-account")
    mobile_tool_identity.init_schema()
    for sid, label in (("owner-phone", "owner-account"), ("foreign-phone", "other-account")):
        database.create_session(sid, "default")
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO mobile_tool_identity "
                "(session_id,owner,kind,principal_id,credential_hash) VALUES (%s,%s,%s,%s,%s)",
                (sid, "account:" + label, "paired", sid, "test-credential-hash"),
            )
    with get_db() as conn:
        assert migrate(conn.cursor()) == ["sessions.projectless_chat(1)"]
    owner = database.get_session("owner-phone")
    foreign = database.get_session("foreign-phone")
    assert owner["mode"] == "chat"
    assert foreign["mode"] is None
    assert runtime_session_mode(foreign) == "code"

    from app.network import engine, policy

    with get_db() as conn:
        for sid, expected in (("owner-phone", "chat"), ("foreign-phone", None)):
            result = engine.materialize(
                conn.cursor(), policy.collection("sessions"), sid, {"mode": None},
            )
            assert result["mode"] == expected


def test_upgraded_owner_can_attach_without_losing_history(mock_db: Any) -> None:
    database.create_project("attach-project", "Project", None, "/tmp/project")
    database.create_session("legacy-owner", "default", permission_mode="bypassPermissions")
    database.add_session_message("legacy-owner", "user", "original request")
    with get_db() as conn:
        migrate(conn.cursor())
    assert database.get_session("legacy-owner")["mode"] == "chat"
    attached = database.attach_session_project("legacy-owner", "attach-project", None)
    assert attached["mode"] is None
    assert attached["permission_mode"] == "default"
    assert runtime_session_mode(attached) == "code"
    assert database.get_session_messages("legacy-owner")[0]["content"] == "original request"
