"""Unit tests for the mirror-mode drift verifier (app/core/session_store_verify.py).

These exercise the drift classification logic without a live Postgres by
patching the two DB query helpers and the JSONL readers. They assert both the
control flow (mode gating, pre-mirror skip) and the log signal each divergence
produces.
"""

import logging

import pytest

from app.core import session_store_verify as ssv
from app.core.config import settings


@pytest.fixture
def mirror_mode(monkeypatch):
    monkeypatch.setattr(settings, "session_store_mode", "mirror", raising=False)


def _patch_jsonl(monkeypatch, *, path="/fake/x.jsonl", uuids=None, agents=None):
    """Patch the jsonl_parser functions the verifier imports at call time."""
    import app.core.jsonl_parser as jp

    monkeypatch.setattr(jp, "get_session_jsonl_path", lambda sid, wd="/workspace": path)
    monkeypatch.setattr(
        jp, "parse_jsonl_file",
        lambda p: [{"uuid": u} for u in (uuids or [])],
    )
    monkeypatch.setattr(
        jp, "get_agent_jsonl_paths",
        lambda sid, wd="/workspace": dict(agents or {}),
    )


def _patch_db(monkeypatch, *, main_uuids, subpaths=None):
    monkeypatch.setattr(ssv, "_db_main_uuids", lambda sid: list(main_uuids))
    monkeypatch.setattr(ssv, "_db_subagent_subpaths", lambda sid: set(subpaths or set()))


def test_noop_when_not_mirror_mode(monkeypatch):
    """Outside mirror mode the verifier must not even touch the DB."""
    monkeypatch.setattr(settings, "session_store_mode", "disabled", raising=False)
    called = {"db": False}

    def _boom(sid):
        called["db"] = True
        raise AssertionError("DB must not be queried outside mirror mode")

    monkeypatch.setattr(ssv, "_db_main_uuids", _boom)
    ssv.log_session_store_drift("sess-1", "/workspace")
    assert called["db"] is False


def test_pre_mirror_session_skipped(monkeypatch, mirror_mode, caplog):
    """Zero DB entries = session predates mirror; debug skip, no warning."""
    _patch_db(monkeypatch, main_uuids=[])
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    assert "no DB entries yet" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_clean_match_no_warning(monkeypatch, mirror_mode, caplog):
    _patch_db(monkeypatch, main_uuids=["a", "b", "c"], subpaths={"subagents/agent-1"})
    _patch_jsonl(monkeypatch, uuids=["a", "b", "c"], agents={"agent-1": "/p/agent-1.jsonl"})
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    assert "OK" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_count_drift_warns(monkeypatch, mirror_mode, caplog):
    _patch_db(monkeypatch, main_uuids=["a", "b", "c", "d", "e", "f"])
    _patch_jsonl(monkeypatch, uuids=["a", "b"])  # delta = +4 > tolerance
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "DRIFT count" in warnings[0].getMessage()


def test_small_delta_is_jitter_not_warning(monkeypatch, mirror_mode, caplog):
    """A 1-entry delta is treated as active-stream flush jitter (INFO)."""
    _patch_db(monkeypatch, main_uuids=["a", "b", "c", "d"])
    _patch_jsonl(monkeypatch, uuids=["a", "b", "c"])  # delta = +1 <= tolerance
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "jitter" in caplog.text


def test_uuid_drift_warns_when_counts_match(monkeypatch, mirror_mode, caplog):
    _patch_db(monkeypatch, main_uuids=["a", "b", "c"])
    _patch_jsonl(monkeypatch, uuids=["a", "X", "c"])  # same count, entry 1 differs
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "DRIFT uuid" in warnings[0].getMessage()


def test_subagent_drift_warns(monkeypatch, mirror_mode, caplog):
    """Main transcript matches but a subagent transcript is missing in PG."""
    _patch_db(monkeypatch, main_uuids=["a", "b"], subpaths={"subagents/agent-1"})
    _patch_jsonl(
        monkeypatch, uuids=["a", "b"],
        agents={"agent-1": "/p/agent-1.jsonl", "agent-2": "/p/agent-2.jsonl"},
    )
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "DRIFT subagents" in warnings[0].getMessage()


def test_db_entries_but_no_jsonl_warns(monkeypatch, mirror_mode, caplog):
    _patch_db(monkeypatch, main_uuids=["a", "b"])
    _patch_jsonl(monkeypatch, path=None)  # transcript file not found
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "no JSONL transcript" in warnings[0].getMessage()


def test_never_raises_on_internal_error(monkeypatch, mirror_mode, caplog):
    """A failure inside the verifier must be swallowed, not propagated."""
    def _boom(sid):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ssv, "_db_main_uuids", _boom)
    # Must not raise.
    with caplog.at_level(logging.DEBUG, logger=ssv.logger.name):
        ssv.log_session_store_drift("sess-1", "/workspace")
    assert "skipped for" in caplog.text
