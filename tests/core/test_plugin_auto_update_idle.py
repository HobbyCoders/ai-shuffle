"""Plugin auto-update idle behaviour + defaults (F10).

Two things are pinned:

  * a DISABLED auto-updater does not wake on a timer. It used to re-read a JSON
    settings file every 5 minutes — 288 wake-ups a day to learn nothing, on a
    file that only ever changes through an API call that already signals the
    task directly.
  * the shipped defaults do not schedule an unrequested git+network burst.

The loop is driven with a fake plugin service, so no marketplace, git binary or
network is touched.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import plugin_auto_update as pau
from app.core import plugin_updates


class _FakeService:
    def __init__(self, settings):
        self._settings = settings
        self.reads = 0
        self.runs = 0

    def get_auto_update_settings(self):
        self.reads += 1
        return self._settings

    def run_auto_update(self):
        self.runs += 1
        return {}


def _install_service(monkeypatch, service):
    import app.core.plugin_service as ps

    monkeypatch.setattr(ps, "get_plugin_service", lambda: service, raising=False)


@pytest.fixture(autouse=True)
def fast_startup(monkeypatch):
    monkeypatch.setattr(pau, "STARTUP_DELAY_SECONDS", 0.01)
    yield


# ─── Disabled: no timer ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_updater_does_not_wake_on_a_timer(monkeypatch):
    service = _FakeService(SimpleNamespace(enabled=False))
    _install_service(monkeypatch, service)

    slept = []
    real_wait_for = asyncio.wait_for

    async def _tracking_wait_for(aw, timeout=None):
        slept.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(pau.asyncio, "wait_for", _tracking_wait_for)

    task = asyncio.create_task(pau._plugin_auto_update_loop())
    try:
        await asyncio.sleep(0.25)
        assert service.reads == 1, (
            f"the disabled loop re-read settings {service.reads} times — it is "
            f"still polling instead of parking on the reschedule event"
        )
        assert slept == [], "the disabled loop armed a timeout"
        assert service.runs == 0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_disabled_updater_wakes_when_settings_change(monkeypatch):
    """Parking forever is only correct because the API pokes the event."""
    settings = SimpleNamespace(enabled=False)
    service = _FakeService(settings)
    _install_service(monkeypatch, service)

    task = asyncio.create_task(pau._plugin_auto_update_loop())
    try:
        await asyncio.sleep(0.1)
        assert service.reads == 1

        pau.reschedule_auto_update()
        await asyncio.sleep(0.1)

        assert service.reads >= 2, "the reschedule signal did not wake the task"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ─── Jitter ───────────────────────────────────────────────────────────────


def test_jitter_only_ever_adds():
    for base in (1.0, 60.0, 21600.0):
        for _ in range(50):
            value = pau._jittered(base)
            assert base <= value <= base * (1 + pau.JITTER_FRACTION)


def test_jitter_spreads_the_burst():
    values = {pau._jittered(3600.0) for _ in range(50)}

    assert len(values) > 1, "every install would burst at the same instant"


def test_jitter_of_zero_is_zero():
    assert pau._jittered(0) == 0.0


# ─── Defaults ─────────────────────────────────────────────────────────────


def test_auto_update_is_off_by_default():
    """An unrequested 6-hourly git+network burst is not a sane default."""

    class _Svc:
        AUTO_UPDATE_FILE = "auto_update_settings.json"

        def _read_json_file(self, _path, default):
            return default

    settings = plugin_updates.get_auto_update_settings(_Svc())

    assert settings.enabled is False
    assert settings.interval_hours >= 24


def test_an_explicit_opt_in_is_honoured():
    """Changing the default must not switch off an install that opted in."""

    class _Svc:
        AUTO_UPDATE_FILE = "auto_update_settings.json"

        def _read_json_file(self, _path, _default):
            return {"enabled": True, "intervalHours": 6}

    settings = plugin_updates.get_auto_update_settings(_Svc())

    assert settings.enabled is True
    assert settings.interval_hours == 6
