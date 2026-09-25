"""The desktop update bridge tolerates the desktop's 30 s report interval."""
import json
import time

import pytest

from app.core import application_updates as updates

DESKTOP_INTERVAL = 30  # BRIDGE_INTERVAL_MS in desktop/src/main/updater.ts


@pytest.fixture
def bridge(monkeypatch):
    reads = []

    def setting(key):
        reads.append(key)
        return None

    monkeypatch.setattr(updates.database, "get_system_setting", setting)
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES", raising=False)
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    monkeypatch.setattr(updates, "_desktop_state", {})
    monkeypatch.setattr(updates, "_desktop_request", None)
    return reads


def test_desktop_stays_present_between_30_second_reports(bridge, monkeypatch):
    updates.desktop_exchange({"phase": "idle"})
    monkeypatch.setattr(updates, "_desktop_seen", time.monotonic() - DESKTOP_INTERVAL - 5)
    status = updates.status()
    assert status["kind"] == "desktop" and status["supported"] is True
    updates.enqueue("check")  # accepted: the updater still counts as present
    assert updates._desktop_request["action"] == "check"


def test_desktop_stays_present_through_one_missed_report(bridge, monkeypatch):
    """R11-10: presence was 45 s, shorter than the ~60 s of silence one missed
    30 s report leaves, so for ~15 s the web UI lost the desktop updater and
    ``enqueue`` refused with "application updater is unavailable"."""
    updates.desktop_exchange({"phase": "idle"})
    monkeypatch.setattr(updates, "_desktop_seen", time.monotonic() - 2 * DESKTOP_INTERVAL - 5)
    assert updates.status()["kind"] == "desktop"
    updates.enqueue("check")
    assert updates._desktop_request["action"] == "check"
    assert updates.DESKTOP_REQUEST_SECONDS >= updates.DESKTOP_PRESENCE_SECONDS


def test_desktop_interval_matches_the_desktop_shell():
    from pathlib import Path
    import re

    source = (Path(__file__).resolve().parents[2] / "desktop/src/main/updater.ts").read_text()
    interval_ms = int(re.search(r"BRIDGE_INTERVAL_MS = ([\d_]+);", source)[1].replace("_", ""))
    assert interval_ms == DESKTOP_INTERVAL * 1000


def test_desktop_is_gone_after_the_presence_window(bridge, monkeypatch):
    updates.desktop_exchange({"phase": "idle"})
    monkeypatch.setattr(updates, "_desktop_seen", time.monotonic() - updates.DESKTOP_PRESENCE_SECONDS - 1)
    assert "kind" not in updates.status()
    with pytest.raises(ValueError, match="unavailable"):
        updates.enqueue("check")


def test_queued_request_survives_until_the_next_report(bridge, monkeypatch):
    updates.desktop_exchange({"phase": "available"})
    updates.enqueue("download")
    now = time.time()
    monkeypatch.setattr(updates.time, "time", lambda: now + DESKTOP_INTERVAL + 5)
    assert updates.desktop_exchange({"phase": "available"})["action"] == "download"
    assert updates.desktop_exchange({"phase": "available"}) == {}


def test_queued_request_lapses_after_the_request_window(bridge, monkeypatch):
    updates.desktop_exchange({"phase": "available"})
    updates.enqueue("download")
    now = time.time()
    monkeypatch.setattr(updates.time, "time", lambda: now + updates.DESKTOP_REQUEST_SECONDS + 1)
    assert updates.desktop_exchange({"phase": "available"}) == {}


def test_idle_report_skips_the_settings_read(bridge):
    assert updates.desktop_exchange({"phase": "idle"}) == {}
    assert bridge == []
    updates.enqueue("check")
    bridge.clear()
    assert updates.desktop_exchange({"phase": "idle"})["action"] == "check"
    assert bridge == [updates.KEY]  # a queued request is still gated by the account policy


def test_queued_request_is_dropped_when_updates_are_disabled_meanwhile(bridge, monkeypatch):
    updates.desktop_exchange({"phase": "idle"})
    updates.enqueue("check")
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: "false")
    assert updates.desktop_exchange({"phase": "idle"}) == {}
    assert updates._desktop_request is None


def test_container_request_keeps_its_short_expiry(bridge, monkeypatch, tmp_path):
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    before = time.time()
    updates.enqueue("check")
    request = json.loads((tmp_path / "request.json").read_text())
    assert request["action"] == "check" and request["expires_at"] <= before + 11
