"""The dev loop's fake voice provider is reachable only from a dev instance.

``SHUFFLE_DEV_FAKE_VOICE_URL`` moves GPT Live, Grok and the GPT-Live-1.1
check to the fake, and lets the preview cookie place calls, only on the
launcher's disposable preview data and only for a bare loopback ``ws://`` URL.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core import preview_access, voice_endpoints
from app.core.voice_models import GROK_URL
from app.preview_run import preview_environment

FAKE = "ws://127.0.0.1:18991"
CALL_PATHS = [
    "/api/v1/voice/sessions/abc/attach",
    "/api/v1/voice/sessions/abc/audio",
    "/api/v1/voice/sessions/abc/hangup",
]


def _dev_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, marker: str | None = "own",
                  preview: str = "1", url: str = FAKE) -> None:
    from app.core.config import settings

    env = preview_environment(tmp_path, 18880, 18881, extra={"SHUFFLE_DEV_FAKE_VOICE_URL": url})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", preview)
    monkeypatch.setattr(settings, "data_dir", Path(env["DATA_DIR"]))
    if marker is None:
        (tmp_path / ".branch-preview").unlink()
    elif marker == "other":
        (tmp_path / ".branch-preview").write_text("/somewhere/else/data")


def _real() -> None:
    assert voice_endpoints.fake_voice_origin() is None
    assert voice_endpoints.live_url() == "wss://api.openai.com/v1/live/sessions"
    assert voice_endpoints.grok_url() == GROK_URL
    assert voice_endpoints.live_11_probe_url() == "https://api.openai.com/v1/models/gpt-live-1.1"
    assert voice_endpoints.dev_calls_allowed() is False
    for path in CALL_PATHS:
        assert preview_access.mutation_allowed(path, "POST") is False
        assert preview_access.mutation_allowed(path, "WEBSOCKET") is False


def test_a_dev_instance_calls_the_fake(tmp_path, monkeypatch):
    _dev_instance(tmp_path, monkeypatch)
    assert voice_endpoints.live_url() == "ws://127.0.0.1:18991/v1/live/sessions"
    assert voice_endpoints.grok_url() == "ws://127.0.0.1:18991/v1/realtime?model=grok-voice-latest"
    assert voice_endpoints.live_11_probe_url() == "http://127.0.0.1:18991/v1/models/gpt-live-1.1"
    for path in CALL_PATHS:
        assert preview_access.mutation_allowed(path, "POST") is True
        assert preview_access.mutation_allowed(path, "WEBSOCKET") is True
    # Only placing a call: the rest of the voice API stays closed to a preview.
    assert preview_access.mutation_allowed("/api/v1/voice/sessions", "POST") is False
    assert preview_access.mutation_allowed("/api/v1/voice/preview", "POST") is False
    assert preview_access.mutation_allowed("/api/v1/voice/sessions/abc/secret", "POST") is False
    assert preview_access.mutation_allowed("/api/v1/settings/voice", "PUT") is False


def test_the_transports_connect_where_the_endpoints_say(tmp_path, monkeypatch):
    from app.core.voice_grok import GrokVoiceStream
    from app.core.voice_stream import VoiceStream

    live = VoiceStream.__new__(VoiceStream)
    live.config = {"model": "gpt-live-1"}
    grok = GrokVoiceStream.__new__(GrokVoiceStream)
    assert live.upstream_url == "wss://api.openai.com/v1/live/sessions"
    assert grok.upstream_url == GROK_URL
    _dev_instance(tmp_path, monkeypatch)
    assert live.upstream_url == "ws://127.0.0.1:18991/v1/live/sessions"
    assert grok.upstream_url.startswith("ws://127.0.0.1:18991/v1/realtime")


def test_a_normal_install_never_uses_the_fake(monkeypatch):
    monkeypatch.delenv("SHUFFLE_DEV_FAKE_VOICE_URL", raising=False)
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    _real()
    # A stray variable on a real install changes nothing.
    monkeypatch.setenv("SHUFFLE_DEV_FAKE_VOICE_URL", FAKE)
    _real()


@pytest.mark.parametrize("marker,preview", [(None, "1"), ("other", "1"), ("own", "0")])
def test_only_the_launchers_disposable_data_counts(tmp_path, monkeypatch, marker, preview):
    _dev_instance(tmp_path, monkeypatch, marker=marker, preview=preview)
    _real()


@pytest.mark.parametrize("url", [
    "wss://127.0.0.1:18991",            # TLS: not the dev loop's fake
    "ws://10.0.0.5:18991",              # not loopback
    "ws://api.openai.com:443",          # a real host
    "ws://127.0.0.1",                   # no port
    "ws://127.0.0.1:18991/v1/other",    # a path
    "ws://127.0.0.1:18991/?x=1",        # a query
    "ws://user:pw@127.0.0.1:18991",     # credentials
    "http://127.0.0.1:18991",           # not a socket URL
    "ws://127.0.0.1:99999",             # not a port
    "",
])
def test_anything_but_a_bare_loopback_socket_is_ignored(tmp_path, monkeypatch, url):
    _dev_instance(tmp_path, monkeypatch, url=url)
    _real()
