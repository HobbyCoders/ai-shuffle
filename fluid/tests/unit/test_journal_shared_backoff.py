"""Fresh consumers of one full journal must share process-local backoff."""
import logging

import pytest
from fluid.codemode import skills


def test_fresh_stores_share_quota_warning_and_reap_window(tmp_path, monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr(skills.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(skills, "JOURNAL_MAX_BYTES", 64)
    reaps = []
    original = skills.SkillStore.reap_journal

    def reap(store):
        reaps.append(store.root)
        return original(store)

    monkeypatch.setattr(skills.SkillStore, "reap_journal", reap)
    before = skills.journal_metrics()["skipped_quota"]
    with caplog.at_level(logging.WARNING, logger=skills.__name__):
        for _ in range(5):
            skills.SkillStore(tmp_path).journal_append({"kind": "cell", "text": "x" * 100})
        assert len(reaps) == 1
        assert len(caplog.records) == 1
        assert skills.journal_metrics()["skipped_quota"] - before == 5
        now[0] += 15
        skills.SkillStore(tmp_path).journal_append({"kind": "cell", "text": "x" * 100})
        assert len(reaps) == 2
        assert len(caplog.records) == 1
        now[0] += 45
        skills.SkillStore(tmp_path).journal_append({"kind": "cell", "text": "x" * 100})
        assert len(caplog.records) == 2
        assert "6 skipped since last warning" in caplog.records[-1].getMessage()
        assert str(tmp_path) not in caplog.text


def test_explicit_write_still_reaps_and_raises_inside_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "JOURNAL_MAX_BYTES", 64)
    skills.SkillStore(tmp_path).journal_append({"kind": "cell", "text": "x" * 100})
    reaps = []
    monkeypatch.setattr(skills.SkillStore, "reap_journal", lambda self: reaps.append(1) or 0)
    for _ in range(2):
        with pytest.raises(OSError, match="byte quota"):
            skills.SkillStore(tmp_path).journal_append({"kind": "remember", "text": "x" * 100})
    assert len(reaps) == 2


def test_aliases_share_windows_but_other_journals_do_not(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(skills, "JOURNAL_MAX_BYTES", 64)
    (tmp_path / "child").mkdir()
    roots = [tmp_path, tmp_path / "child" / "..", tmp_path / "other"]
    with caplog.at_level(logging.WARNING, logger=skills.__name__):
        for root in roots:
            skills.SkillStore(root).journal_append({"kind": "cell", "text": "x" * 100})
    assert len(caplog.records) == 2


def test_window_reservation_is_thread_safe_and_bounded(monkeypatch):
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(skills, "_JOURNAL_BACKOFF", OrderedDict())
    monkeypatch.setattr(skills.time, "monotonic", lambda: 100.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: skills._journal_window_count("one", "quota", 60), range(40)))
    assert sum(counts) == 1
    for index in range(skills._JOURNAL_BACKOFF_LIMIT + 10):
        skills._journal_window_count(str(index), "reap", 15)
    assert len(skills._JOURNAL_BACKOFF) == skills._JOURNAL_BACKOFF_LIMIT

