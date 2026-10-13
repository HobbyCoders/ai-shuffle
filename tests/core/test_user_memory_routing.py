"""Authenticated session identity selects durable memory before tools run."""
from unittest.mock import patch
import pytest
from app.core.fluid_memory import session_memory
from app.core.config import settings


def test_database_sessions_fail_closed_without_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path))
    with pytest.raises(ValueError, match="without a session"):
        session_memory(None)
    with patch("app.db.sessions.get_session", return_value=None), patch("app.db.sessions.get_session_by_sdk_id", return_value=None):
        with pytest.raises(ValueError, match="does not exist"):
            session_memory("unknown")


def test_owner_and_project_follow_database_ids_not_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path))
    sessions = {
        "a1": {"api_user_id": "alice", "project_v2_id": "shared-project"},
        "a2": {"api_user_id": "alice", "project_v2_id": "shared-project"},
        "b": {"api_user_id": "bob", "project_v2_id": "shared-project"},
        "owner": {"api_user_id": None},
    }
    with patch("app.db.sessions.get_session", side_effect=sessions.get):
        a, pa = session_memory("a1", str(tmp_path / "checkout-one"))
        a.save_memory("Alice's private fact", protected=True, scope=pa)
        again, pb = session_memory("a2", str(tmp_path / "checkout-two"))
        b, _ = session_memory("b")
        owner, _ = session_memory("owner")
        assert pa == pb == "project:shared-project"
        assert again.memories()[0].text == "Alice's private fact"
        assert not b.memories() and not owner.memories()
        assert a.root != b.root != owner.root
