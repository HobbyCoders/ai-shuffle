"""Pure benchmark checks: no services, database or credentials needed."""
import math

import pytest

from scripts.perf.sync_bench import committed_at, delta_lags, op_key, recall_visible, sanitize_log, stages, summarize


def test_project_creation_starts_at_first_domain_commit_not_response_or_read():
    result = {"trace": "create", "end": 7}
    metrics = {"events": [{"kind": "commit", "trace": "other", "t": 1},
                          {"kind": "commit", "trace": "create", "t": 4},
                          {"kind": "commit", "trace": "create", "t": 3}]}
    assert committed_at(result, metrics) == 3
    assert committed_at(result, {"events": []}) == 7


def test_stream_lags_match_common_seq_endpoints_not_frame_positions():
    owner = [{"seq": 1, "t": 1}, {"seq": 2, "t": 2}, {"seq": 4, "t": 4}]
    peer = [{"seq": 2, "t": 2.5}, {"seq": 3, "t": 3.5}, {"seq": 4, "t": 4.25}]
    assert delta_lags(owner, peer) == [500, 250]
    with pytest.raises(RuntimeError, match="not a steady streaming sample"):
        delta_lags(owner, peer[:1])


def test_nearest_rank_percentiles_match_js_rig():
    result = summarize([9, 1, 3, 2, math.nan, math.inf, "ignored"])
    assert result == {"n": 4, "p50": 2, "p75": 3, "max": 9}
    assert summarize([]) == {"n": 0, "p50": None, "p75": None, "max": None}


def test_stage_correlation_uses_signed_origin_and_sequence():
    source = {"events": [
        {"kind": "published", "t": 2, "op": ["A", 1, 7],
         "collection": "sessions", "record_id": "chat", "scope": "scan_targeted", "bytes": 80},
        {"kind": "push_send", "t": 3, "op": ["A", 1, 7]},
    ]}
    peer = {"events": [
        {"kind": "applied", "t": 2.5, "arrival": 2.4, "op": ["B", 1, 7]},
        {"kind": "applied", "t": 4, "arrival": 3.5, "op": ["A", 1, 7]},
    ]}
    result = stages(1, 5, source, peer, "sessions", "chat")
    assert result["commit_to_publish_ms"] == 1000
    assert result["publish_to_exchange_ms"] == 1000
    assert result["exchange_to_apply_ms"] == 500
    assert result["apply_ms"] == 500
    assert result["apply_to_visible_ms"] == 1000
    assert result["exchange_boundary"] == "push_send"
    assert stages(1, 5, source, peer, "sessions", "other") == {}
    assert stages(3, 5, source, peer, "sessions", "chat") == {}


def test_recall_visibility_ignores_replica_aliases():
    from types import SimpleNamespace
    class Store:
        def search_memories(self, text, *, limit):
            assert limit == 100
            return [(SimpleNamespace(id="peer-origin:m1", text="uniquefruit"), False)]
    assert recall_visible(Store(), "uniquefruit")
    assert not recall_visible(Store(), "unrelatedfruit")


def test_log_sanitization_covers_preview_and_enrollment_links():
    text = "http://lab/?token=fake-preview&next=/ http://lab/join?secret=fake-join\" 200"
    assert sanitize_log(text) == "http://lab/?token=[redacted]&next=/ http://lab/join?secret=[redacted]\" 200"


def test_op_key_does_not_conflate_incarnations():
    assert op_key(dict(origin_device_id="A", origin_incarnation=2, origin_seq=7)) == ["A", 2, 7]


def test_project_and_stream_only_cannot_silently_skip_every_journey(monkeypatch):
    from scripts.perf.sync_bench import main
    monkeypatch.setattr("sys.argv", ["sync_bench", "run", "--output", "unused.json",
                                    "--projects-only", "--stream-only"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_child_cannot_attach_to_unowned_app(monkeypatch):
    from scripts.perf.sync_bench import child
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    monkeypatch.delenv("SYNC_BENCH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="private devloop"):
        child()
