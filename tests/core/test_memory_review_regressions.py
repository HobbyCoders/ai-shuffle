"""Review regressions for cache usefulness and best-effort counters."""
import sys
from types import SimpleNamespace

from app.core.git_cache import _GitResultCache


def test_second_large_repo_status_read_hits_cache(monkeypatch, tmp_path):
    import app.core.git_service as module
    cache = _GitResultCache()
    calls = []
    result = {"is_clean": False, "untracked": ["x" * (3 * 1024 * 1024)]}
    def status(path):
        calls.append(path)
        return result
    monkeypatch.setattr(module, "_git_cache", cache)
    monkeypatch.setattr(module, "_native_reader", SimpleNamespace(get_status=status))
    assert module.git_service.get_status(str(tmp_path)) is result
    assert module.git_service.get_status(str(tmp_path)) is result
    assert len(calls) == 1


def test_bulk_results_cannot_evict_long_ttl_markers(monkeypatch):
    import app.core.git_cache as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    cache = _GitResultCache()
    markers = {"default_branch:r": "main", "ahead_behind:r:main": (2, 3), "fetch:r": True}
    for key, value in markers.items():
        cache.put(key, value)
    for i in range(10):
        cache.put(f"status:large-{i}", {"untracked": [str(i) + "x" * (3 * 1024 * 1024)]})
    for i in range(20):
        cache.put(f"status:small-{i}", {"untracked": [str(i) + "x" * (1024 * 1024)]})
    assert len(cache._oversized) == 4
    for key, value in markers.items():
        assert cache.get(key, 30) == value
    cache.invalidate_prefix("status:")
    assert not cache._oversized
    assert cache.memory_stats()["git_cache_entries"] == 3
    now[0] += 301
    assert cache.purge_expired() == 3


def test_counter_failure_is_isolated_to_its_module(monkeypatch):
    from app.core.resource_monitor import _retention_metrics
    monkeypatch.setitem(sys.modules, "app.core.jsonl_parser", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "app.core.detached_tasks", SimpleNamespace(_DETACHED_TASKS=[None]))
    metrics = _retention_metrics()
    assert metrics["detached_tasks"] == 1
    assert "parse_cache_entries" not in metrics


async def test_collect_metrics_tolerates_unexpected_retention_failure(monkeypatch):
    from app.core import resource_monitor
    def broken():
        raise RuntimeError("counter unavailable")
    monkeypatch.setattr(resource_monitor, "_retention_metrics", broken)
    assert isinstance(await resource_monitor._collect_metrics(), dict)
