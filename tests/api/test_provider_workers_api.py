"""The additive ``/live`` attribution field for ProviderAgent workers.

Per-turn worker token/spend is attributed to the worker's PROVIDER and shown
while the turn runs. The dedicated ``/provider-workers`` routes that once
exposed this report had no client and were removed; the report itself is what
``/live`` attaches when workers are live. The supervisor's kill path is covered
in ``tests/core/test_provider_agent_safety.py`` against the real registry.
"""

import pytest

from app.core import provider_agent_supervisor as sup


SESSION = "parent-session"


@pytest.fixture(autouse=True)
def _clean_supervisor():
    sup.reset()
    yield
    sup.reset()


def _worker(worker_id, provider="prov-openai", model="gpt-5.5"):
    return sup.register_worker(sup.WorkerRecord(
        parent_session_id=SESSION,
        worker_session_id=worker_id,
        task_id=worker_id,
        tool_use_id=worker_id,
        provider_id=provider,
        model_id=model,
    ))


class TestLiveAttributionField:
    """Attached ONLY when workers are live, so the pinned /live wire shape is
    unchanged for every ordinary session."""

    def test_absent_when_no_workers_are_live(self):
        assert sup.has_live_workers(SESSION) is False

    def test_present_and_shaped_once_workers_are_live(self):
        _worker("w1")
        sup.record_usage(SESSION, "w1", {"tokens_out": 12})

        assert sup.has_live_workers(SESSION) is True
        report = sup.attribution(SESSION)
        assert set(report) == {"workers", "providers", "totals"}
        assert report["workers"][0]["workerSessionId"] == "w1"
        assert report["totals"]["tokens_out"] == 12

    def test_reports_per_provider_attribution(self):
        _worker("w1", provider="prov-openai")
        _worker("w2", provider="prov-openai", model="gpt-5.5-mini")
        _worker("w3", provider="prov-codex", model="gpt-5.5-codex")
        sup.record_usage(SESSION, "w1", {"tokens_in": 100, "total_cost_usd": 0.5})
        sup.record_usage(SESSION, "w3", {"tokens_in": 10, "total_cost_usd": 0.1})

        report = sup.attribution(SESSION)

        assert report["totals"]["workers"] == 3
        assert report["totals"]["tokens_in"] == 110
        assert report["providers"]["prov-openai"]["workers"] == 2
        assert report["providers"]["prov-codex"]["tokens_in"] == 10
