from __future__ import annotations

import pytest

from app.db import session_events as source
from app.db import transcript_projection_work as work
from app.db.profiles import create_profile
from app.db.sessions import create_session
from app.db.transcript_projections import ProjectionStoreUnavailable


def session():
    create_profile('raw-fixture', 'Fixture', None, {}, if_absent=True)
    create_session('raw-fixture', 'raw-fixture', derive_provider_from_profile=False)
    return 'raw-fixture'


def test_source_body_budget_returns_a_contiguous_prefix_with_serving_decoding(monkeypatch):
    sid = session()
    source.append_events(sid, [{'type': 'message.user', 'payload': {'content': 'x' * 80}},
                               {'type': 'message.user', 'payload': {'content': 'y' * 80}}])
    monkeypatch.setattr(work, 'SOURCE_CHARS', 130)
    first = work.source_rows(sid, 0, 2)
    assert first == source.get_events(sid, 0, 1)
    assert work.source_rows(sid, first[-1]['seq'], 2) == source.get_events(sid, 1, 1)
    assert work.source_rows(sid, 0, 1) == first
    assert work.source_rows(sid, 2, 2) == []


def test_oversized_first_source_row_is_never_skipped_or_fetched(monkeypatch):
    sid = session()
    source.append_events(sid, [{'type': 'tool.result', 'payload': {'content': 'x' * 1000}},
                               {'type': 'turn.completed', 'payload': {}}])
    original = source.get_events(sid)
    monkeypatch.setattr(work, 'SOURCE_CHARS', 130)
    def no_decode(_):
        raise AssertionError('oversized body must not reach the decoder')
    with monkeypatch.context() as decoder_patch:
        decoder_patch.setattr(source, '_parse_payload', no_decode)
        with pytest.raises(ProjectionStoreUnavailable, match='source_row_limit'):
            work.source_rows(sid, 0, 2)
    assert source.get_events(sid) == original
