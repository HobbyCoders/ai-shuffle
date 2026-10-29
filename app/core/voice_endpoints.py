"""Where streamed voice calls connect: the real providers, or the dev loop's fake.

The dev loop (``python -m scripts.devloop up``) runs a fake realtime voice
provider (``scripts/devloop/fake_voice.py``) and names it in
``SHUFFLE_DEV_FAKE_VOICE_URL``. A call then runs the real transports
(:class:`app.core.voice_stream.VoiceStream` for GPT Live,
:class:`app.core.voice_grok.GrokVoiceStream` for Grok) with only the remote
address replaced.

The fake is used only when every check passes:

* the process runs on the launcher's own disposable preview data
  (:func:`app.preview_run.disposable_preview_data`: ``AI_SHUFFLE_PREVIEW=1``
  and the ``.branch-preview`` marker naming ``DATA_DIR`` exactly, written only
  by ``app.preview_run.preview_environment``), and
* the URL is plain ``ws://`` to a loopback address with a port and nothing
  else (no path, query, credentials or TLS).

Anything else (a real install with a stray variable, raw preview mode, a
remote or malformed address) keeps the real endpoints. The same checks let a
dev instance's preview cookie place calls (:func:`dev_calls_allowed`): its
provider is the fake and its data is throwaway, so no call there can reach a
paid service.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from app.core.voice_models import GROK_URL, LIVE_11_MODEL

ENV_KEY = "SHUFFLE_DEV_FAKE_VOICE_URL"
LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_11_PROBE_URL = f"https://api.openai.com/v1/models/{LIVE_11_MODEL}"
# Paths on the fake, mirroring the real ones.
LIVE_PATH = "/v1/live/sessions"
GROK_PATH = "/v1/realtime?model=grok-voice-latest"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def fake_voice_origin() -> str | None:
    """``host:port`` of the dev loop's fake voice provider, or None.

    None unless this is the dev loop's disposable instance and the configured
    URL is a bare loopback ``ws://`` origin. Read on every call (cheap: one
    environment read and one small marker file), so nothing is cached past a
    change of environment.
    """
    raw = (os.environ.get(ENV_KEY) or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (parts.scheme != "ws" or not port or not _loopback(host) or parts.username
            or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    try:
        from app.preview_run import disposable_preview_data

        if not disposable_preview_data():
            return None
    except Exception:  # noqa: BLE001 - an unreadable marker is a real install
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def live_url() -> str:
    """The GPT Live socket a call opens."""
    origin = fake_voice_origin()
    return f"ws://{origin}{LIVE_PATH}" if origin else LIVE_URL


def grok_url() -> str:
    """The xAI realtime socket a Grok call opens."""
    origin = fake_voice_origin()
    return f"ws://{origin}{GROK_PATH}" if origin else GROK_URL


def live_11_probe_url() -> str:
    """Where the GPT-Live-1.1 access check asks (the fake always says 404)."""
    origin = fake_voice_origin()
    return f"http://{origin}/v1/models/{LIVE_11_MODEL}" if origin else LIVE_11_PROBE_URL


def dev_calls_allowed() -> bool:
    """A dev instance's preview cookie may place calls: they reach only the fake."""
    return fake_voice_origin() is not None
