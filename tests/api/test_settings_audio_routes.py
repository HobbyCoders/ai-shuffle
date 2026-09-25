"""Audio settings routes (``app.api.settings_audio``) through the real app.

Covers the TTS/STT model list, the model and TTS-default setters, file
transcription (``POST /settings/transcribe``) and speech synthesis
(``POST /settings/tts/stream``). Every upstream call is served by an
``httpx.MockTransport`` handler installed on the module's own ``httpx`` name,
so nothing reaches fal.ai or OpenAI. The OpenAI key comes from a real
``model_providers`` row in the per-test SQLite database, the fal key from the
encrypted ``fal_api_key`` system setting.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.api import settings_audio
from app.api.auth import hash_api_key
from app.api.settings_crypto import set_encrypted_api_key
from app.core import encryption
from app.db import database
from app.services.fal_schema import ModelSchema, ParamField
from tests.fakes.http import mock_transport_client

BASE = "/api/v1/settings"
FAL_KEY = "fal-test-key-0123456789abcdefghijklmnop"
OPENAI_KEY = "sk-openai-test-0123456789abcdefghijklmnop"
FILE_URL = "https://v3.fal.media/files/uploaded-clip.webm"
UPLOAD_URL = "https://storage.fal.test/put/uploaded-clip"
CDN_URL = "https://cdn.fal.test/audio/out.mp3"
USER_KEY = "aih_audio_user_key"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class Upstream:
    """Serves fal.ai / OpenAI requests from canned responses, recording each.

    ``routes`` maps ``(METHOD, url-prefix)`` to a response, a callable taking
    the request, or an exception instance to raise.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for (method, prefix), outcome in self.routes.items():
            if request.method == method and url.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                if callable(outcome):
                    return outcome(request)
                return outcome
        return httpx.Response(599, text=f"unexpected {request.method} {url}")

    def json_of(self, prefix: str) -> dict:
        for request in self.calls:
            if str(request.url).startswith(prefix):
                return json.loads(request.content)
        raise AssertionError(f"no call to {prefix}: {[str(r.url) for r in self.calls]}")

    def request_to(self, prefix: str) -> httpx.Request:
        for request in self.calls:
            if str(request.url).startswith(prefix):
                return request
        raise AssertionError(f"no call to {prefix}")


def install(monkeypatch, routes) -> Upstream:
    upstream = Upstream(routes)
    monkeypatch.setattr(settings_audio, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(upstream),
        TimeoutException=httpx.TimeoutException,
    ))
    return upstream


def fal_upload_routes() -> dict:
    return {
        ("POST", "https://rest.alpha.fal.ai/storage/upload/initiate"):
            httpx.Response(200, json={"upload_url": UPLOAD_URL, "file_url": FILE_URL}),
        ("PUT", UPLOAD_URL): httpx.Response(200),
    }


@pytest.fixture
def c(authenticated_client):
    return authenticated_client


@pytest.fixture
def fal_key(c):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    return FAL_KEY


@pytest.fixture
def openai_row(c):
    database.create_model_provider(
        "openai-audio", "OpenAI", "openai", "https://api.openai.com/v1",
        auth_token=encryption.encrypt_value(OPENAI_KEY),
    )
    return OPENAI_KEY


@pytest.fixture
def api_user(mock_db):
    database.create_api_user("audio-user", "Audio User", hash_api_key(USER_KEY))
    return {"Authorization": f"Bearer {USER_KEY}"}


def _transcribe(c, data=b"RIFF-audio-bytes", name="clip.webm", ctype="audio/webm"):
    return c.post(f"{BASE}/transcribe", files={"file": (name, data, ctype)})


# ---------------------------------------------------------------------------
# GET /integrations/audio/models
# ---------------------------------------------------------------------------

def test_audio_models_without_keys_lists_everything_unavailable(c):
    body = c.get(f"{BASE}/integrations/audio/models").json()
    assert body["fal_configured"] is False
    assert body["openai_configured"] is False
    assert body["current_tts"] == "fal-ai/kokoro/american-english"
    assert body["current_stt"] == "fal-ai/whisper"
    assert [m["id"] for m in body["tts_models"]] == list(settings_audio.TTS_MODELS)
    assert [m["id"] for m in body["stt_models"]] == list(settings_audio.STT_MODELS)
    assert not any(m["available"] for m in body["tts_models"] + body["stt_models"])
    current = [m["id"] for m in body["tts_models"] if m["is_current"]]
    assert current == ["fal-ai/kokoro/american-english"]
    for model in body["tts_models"]:
        assert set(model) == {"id", "name", "description", "price_display", "provider", "available", "is_current"}


def test_audio_models_availability_follows_each_provider_key(c, fal_key, openai_row):
    database.set_system_setting("tts_model", "openai/tts-1")
    body = c.get(f"{BASE}/integrations/audio/models").json()
    assert body["fal_configured"] is True and body["openai_configured"] is True
    assert body["current_tts"] == "openai/tts-1"
    by_id = {m["id"]: m for m in body["tts_models"]}
    assert by_id["openai/tts-1"]["provider"] == "openai" and by_id["openai/tts-1"]["is_current"]
    assert by_id["fal-ai/kokoro/american-english"]["provider"] == "fal-ai"
    assert all(m["available"] for m in body["tts_models"])
    assert FAL_KEY not in c.get(f"{BASE}/integrations/audio/models").text
    assert OPENAI_KEY not in c.get(f"{BASE}/integrations/audio/models").text


def test_audio_models_openai_only_marks_fal_models_unavailable(c, openai_row):
    body = c.get(f"{BASE}/integrations/audio/models").json()
    for model in body["tts_models"] + body["stt_models"]:
        assert model["available"] is (model["provider"] == "openai"), model["id"]


def test_codex_subscription_row_is_not_an_openai_audio_key(c):
    database.create_model_provider(
        "chatgpt", "ChatGPT", "chatgpt", "https://chatgpt.com",
        auth_token=encryption.encrypt_value("oauth-token-value-0123456789"),
    )
    assert c.get(f"{BASE}/integrations/audio/models").json()["openai_configured"] is False


# ---------------------------------------------------------------------------
# Model and default setters
# ---------------------------------------------------------------------------

def test_tts_model_setter_trims_and_rejects_blank(c):
    r = c.patch(f"{BASE}/integrations/audio/tts", json={"model": "  fal-ai/some-new-tts  "})
    assert r.status_code == 200
    assert r.json() == {"success": True, "model": "fal-ai/some-new-tts"}
    assert database.get_system_setting("tts_model") == "fal-ai/some-new-tts"
    blank = c.patch(f"{BASE}/integrations/audio/tts", json={"model": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "Model cannot be empty"
    assert database.get_system_setting("tts_model") == "fal-ai/some-new-tts"


def test_stt_model_setter_keeps_the_microphone_choice(c):
    """Changing the transcription-tool model must not move voice input: the
    old shared choice is seeded into ``voice_input_model`` first."""
    database.set_system_setting("stt_model", "fal-ai/elevenlabs/speech-to-text")
    r = c.patch(f"{BASE}/integrations/audio/stt", json={"model": "openai/whisper-1"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "model": "openai/whisper-1"}
    assert database.get_system_setting("stt_model") == "openai/whisper-1"
    assert database.get_system_setting("voice_input_model") == "fal-ai/elevenlabs/speech-to-text"
    assert c.patch(f"{BASE}/integrations/audio/stt", json={"model": ""}).status_code == 400


def test_tts_defaults_save_normalised_values(c):
    r = c.patch(f"{BASE}/integrations/tts", json={
        "model": " fal-ai/orpheus-tts ", "voice": " tara ", "speed": 1.25, "output_format": " FLAC ",
    })
    assert r.status_code == 200 and r.json() == {"success": True}
    settings = c.get(f"{BASE}/integrations").json()
    assert settings["tts_model"] == "fal-ai/orpheus-tts"
    assert settings["tts_voice"] == "tara"
    assert settings["tts_speed"] == 1.25
    assert settings["tts_output_format"] == "flac"


def test_tts_defaults_ignore_unknown_format_and_blank_fields(c):
    database.set_system_setting("tts_voice", "am_adam")
    database.set_system_setting("tts_output_format", "mp3")
    r = c.patch(f"{BASE}/integrations/tts", json={"model": "   ", "voice": "   ", "output_format": "aiff"})
    assert r.status_code == 200
    assert database.get_system_setting("tts_output_format") == "mp3"
    assert database.get_system_setting("tts_model") is None
    # Documented: an empty voice is ignored, so the saved voice survives.
    # Regression: a whitespace voice used to be saved as "" and read back as
    # the default voice.
    assert database.get_system_setting("tts_voice") == "am_adam"
    assert c.get(f"{BASE}/integrations").json()["tts_voice"] == "am_adam"


@pytest.mark.parametrize("speed", [0.05, 5.5, -1])
def test_tts_speed_out_of_range_is_400_but_model_and_voice_still_save(c, speed):
    r = c.patch(f"{BASE}/integrations/tts", json={"model": "fal-ai/f5-tts", "voice": "x", "speed": speed})
    assert r.status_code == 400
    assert r.json()["detail"] == "Speed must be between 0.1 and 5.0"
    assert database.get_system_setting("tts_speed") is None
    assert database.get_system_setting("tts_model") == "fal-ai/f5-tts"
    assert database.get_system_setting("tts_voice") == "x"


@pytest.mark.parametrize("method,path,body", [
    ("patch", "/integrations/audio/tts", {"model": "fal-ai/x"}),
    ("patch", "/integrations/audio/stt", {"model": "fal-ai/x"}),
    ("patch", "/integrations/tts", {"voice": "af_bella"}),
])
def test_audio_setters_are_owner_only(client, api_user, method, path, body):
    r = getattr(client, method)(BASE + path, json=body, headers=api_user)
    assert r.status_code == 403
    assert r.json()["detail"] == "Owner access required"
    for key in ("tts_model", "stt_model", "tts_voice"):
        assert database.get_system_setting(key) is None


def test_audio_model_list_is_open_to_api_users(client, api_user):
    r = client.get(f"{BASE}/integrations/audio/models", headers=api_user)
    assert r.status_code == 200
    assert r.json()["current_tts"] == "fal-ai/kokoro/american-english"


def test_audio_model_list_requires_authentication(client):
    assert client.get(f"{BASE}/integrations/audio/models").status_code in (401, 403)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/settings/integrations/tts/models"),
    ("get", "/api/v1/settings/integrations/stt/models"),
    ("post", "/api/v1/settings/tts/generate"),
    ("get", "/api/v1/settings/internal/api-keys/fal_api_key"),
    ("get", "/api/v1/settings/ai-tools/available"),
    ("get", "/api/v1/catalog/models/fal-ai/flux/dev"),
])
def test_removed_routes_are_gone(c, fal_key, method, path):
    r = getattr(c, method)(path) if method == "get" else c.post(path, json={"text": "hi"})
    assert r.status_code == 404, (path, r.status_code)
    assert FAL_KEY not in r.text


# ---------------------------------------------------------------------------
# POST /transcribe
# ---------------------------------------------------------------------------

def test_transcribe_fal_uploads_then_transcribes(c, fal_key, monkeypatch):
    upstream = install(monkeypatch, {
        **fal_upload_routes(),
        ("POST", "https://fal.run/fal-ai/whisper"): httpx.Response(200, json={"text": "  hello world  "}),
    })
    r = _transcribe(c, ctype="audio/webm;codecs=opus")
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello world", "duration_seconds": None}
    initiate = upstream.json_of("https://rest.alpha.fal.ai/storage/upload/initiate")
    assert initiate == {"content_type": "audio/webm", "file_name": "clip.webm"}
    assert upstream.request_to(UPLOAD_URL).content == b"RIFF-audio-bytes"
    assert upstream.json_of("https://fal.run/fal-ai/whisper") == {
        "audio_url": FILE_URL, "task": "transcribe", "chunk_level": "segment",
    }
    assert upstream.request_to("https://fal.run/").headers["Authorization"] == f"Key {FAL_KEY}"


def test_transcribe_uses_voice_input_model_not_the_tool_model(c, fal_key, monkeypatch):
    database.set_system_setting("voice_input_model", "fal-ai/elevenlabs/speech-to-text")
    database.set_system_setting("stt_model", "fal-ai/whisper")
    upstream = install(monkeypatch, {
        **fal_upload_routes(),
        ("POST", "https://fal.run/fal-ai/elevenlabs/speech-to-text"): httpx.Response(200, json={"text": "ok"}),
    })
    assert _transcribe(c).json()["text"] == "ok"
    assert not any(str(r.url) == "https://fal.run/fal-ai/whisper" for r in upstream.calls)


def test_transcribe_joins_chunks_when_text_is_empty(c, fal_key, monkeypatch):
    install(monkeypatch, {
        **fal_upload_routes(),
        ("POST", "https://fal.run/"): httpx.Response(200, json={
            "text": "", "chunks": [{"text": "first"}, {"text": "second"}]}),
    })
    assert _transcribe(c).json()["text"] == "first second"


def test_transcribe_silence_is_200_with_empty_text(c, fal_key, monkeypatch):
    install(monkeypatch, {**fal_upload_routes(), ("POST", "https://fal.run/"): httpx.Response(200, json={})})
    r = _transcribe(c)
    assert r.status_code == 200
    assert r.json() == {"text": "", "duration_seconds": None}


@pytest.mark.parametrize("status,expected,detail", [
    (401, 400, "fal.ai API key is invalid. Please update it in Settings > Integrations."),
    (429, 429, "fal.ai rate limit exceeded. Please wait a moment and try again."),
    (422, 500, "Transcription failed: bad audio"),
    (503, 500, "Transcription failed: bad audio"),
])
def test_transcribe_maps_fal_errors(c, fal_key, monkeypatch, status, expected, detail):
    install(monkeypatch, {**fal_upload_routes(), ("POST", "https://fal.run/"): httpx.Response(status, text="bad audio")})
    r = _transcribe(c)
    assert r.status_code == expected
    assert r.json()["detail"] == detail
    assert FAL_KEY not in r.text


@pytest.mark.parametrize("routes,detail_start", [
    ({("POST", "https://rest.alpha.fal.ai/"): httpx.Response(403, text="nope")},
     "fal.ai storage initiate failed: 403"),
    ({("POST", "https://rest.alpha.fal.ai/"): httpx.Response(200, json={"file_url": FILE_URL})},
     "fal.ai storage initiate returned no upload_url/file_url"),
    ({("POST", "https://rest.alpha.fal.ai/"): httpx.Response(200, json={"upload_url": UPLOAD_URL, "file_url": FILE_URL}),
      ("PUT", UPLOAD_URL): httpx.Response(500, text="disk full")},
     "fal.ai storage upload failed: 500"),
])
def test_transcribe_storage_failures_are_502(c, fal_key, monkeypatch, routes, detail_start):
    upstream = install(monkeypatch, routes)
    r = _transcribe(c)
    assert r.status_code == 502
    assert r.json()["detail"].startswith(detail_start)
    assert not any(str(req.url).startswith("https://fal.run/") for req in upstream.calls)


def test_transcribe_timeout_is_504(c, fal_key, monkeypatch):
    install(monkeypatch, {
        **fal_upload_routes(),
        ("POST", "https://fal.run/"): httpx.ReadTimeout("slow"),
    })
    r = _transcribe(c)
    assert r.status_code == 504
    assert r.json()["detail"] == "Transcription timed out. Please try with a shorter audio clip."


def test_transcribe_unexpected_transport_error_is_generic_500(c, fal_key, monkeypatch):
    install(monkeypatch, {("POST", "https://rest.alpha.fal.ai/"): httpx.ConnectError("refused")})
    r = _transcribe(c)
    assert r.status_code == 500
    assert r.json()["detail"] == "Transcription failed"


def test_transcribe_openai_model_goes_to_openai_with_provider_key(c, openai_row, monkeypatch):
    database.set_system_setting("voice_input_model", "openai/gpt-4o-mini-transcribe")
    upstream = install(monkeypatch, {
        ("POST", settings_audio.OPENAI_TRANSCRIBE_URL): httpx.Response(200, json={"text": " from gpt "}),
    })
    r = _transcribe(c, name="note.m4a", ctype="audio/m4a")
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "from gpt"
    request = upstream.request_to(settings_audio.OPENAI_TRANSCRIBE_URL)
    assert request.headers["Authorization"] == f"Bearer {OPENAI_KEY}"
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in request.content
    assert all("fal" not in str(req.url) for req in upstream.calls)


@pytest.mark.parametrize("status,expected", [(401, 400), (429, 429), (500, 500)])
def test_transcribe_maps_openai_errors(c, openai_row, monkeypatch, status, expected):
    database.set_system_setting("voice_input_model", "openai/whisper-1")
    install(monkeypatch, {("POST", settings_audio.OPENAI_TRANSCRIBE_URL): httpx.Response(status, text="upstream")})
    r = _transcribe(c)
    assert r.status_code == expected
    assert OPENAI_KEY not in r.text


def test_transcribe_requires_the_key_of_the_models_provider(c, fal_key, monkeypatch):
    upstream = install(monkeypatch, {})
    database.set_system_setting("voice_input_model", "openai/gpt-4o-transcribe")
    r = _transcribe(c)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("No OpenAI provider with a stored API key")
    assert upstream.calls == []


def test_transcribe_without_fal_key_is_400(c, openai_row, monkeypatch):
    upstream = install(monkeypatch, {})
    r = _transcribe(c)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("fal.ai API key not configured")
    assert upstream.calls == []


def test_transcribe_refuses_the_live_only_model(c, fal_key, openai_row, monkeypatch):
    upstream = install(monkeypatch, {})
    database.set_system_setting("voice_input_model", "openai/gpt-live-transcribe")
    r = _transcribe(c)
    assert r.status_code == 400
    assert "requires live microphone streaming" in r.json()["detail"]
    assert upstream.calls == []


@pytest.mark.parametrize("name,ctype,data,detail", [
    ("notes.txt", "text/plain", b"abc", "Unsupported audio format: text/plain"),
    ("noext", "", b"abc", "Unsupported audio format: . Supported"),
    ("clip.webm", "audio/webm", b"", "Audio file is empty"),
])
def test_transcribe_rejects_bad_uploads_before_any_upstream_call(c, fal_key, monkeypatch, name, ctype, data, detail):
    upstream = install(monkeypatch, {})
    r = _transcribe(c, data=data, name=name, ctype=ctype)
    assert r.status_code == 400
    assert r.json()["detail"].startswith(detail)
    assert upstream.calls == []


def test_transcribe_accepts_a_known_extension_with_generic_type(c, fal_key, monkeypatch):
    upstream = install(monkeypatch, {**fal_upload_routes(), ("POST", "https://fal.run/"): httpx.Response(200, json={"text": "x"})})
    r = _transcribe(c, name="memo.OGG", ctype="application/octet-stream")
    assert r.status_code == 200
    assert upstream.json_of("https://rest.alpha.fal.ai/")["content_type"] == "application/octet-stream"


def test_transcribe_rejects_files_over_25mb(c, fal_key, monkeypatch):
    upstream = install(monkeypatch, {})
    r = _transcribe(c, data=b"\0" * (25 * 1024 * 1024 + 1))
    assert r.status_code == 400
    assert r.json()["detail"] == "Audio file too large. Maximum size is 25MB."
    assert upstream.calls == []


def test_transcribe_requires_authentication(client):
    assert _transcribe(client).status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /tts/stream
# ---------------------------------------------------------------------------

def _fal_tts_routes(model: str, result: dict | None = None) -> dict:
    return {
        ("POST", f"https://fal.run/{model}"): httpx.Response(200, json=result or {"audio": {"url": CDN_URL}}),
        ("GET", CDN_URL): httpx.Response(200, content=b"ID3-mp3-bytes"),
    }


def test_tts_stream_default_kokoro_downloads_the_audio(c, fal_key, monkeypatch):
    upstream = install(monkeypatch, _fal_tts_routes("fal-ai/kokoro/american-english"))
    r = c.post(f"{BASE}/tts/stream", json={"text": "  Hello there  "})
    assert r.status_code == 200, r.text
    assert r.content == b"ID3-mp3-bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert "no-cache" in r.headers["cache-control"]
    assert upstream.json_of("https://fal.run/") == {"prompt": "Hello there", "voice": "af_heart"}
    assert upstream.request_to("https://fal.run/").headers["Authorization"] == f"Key {FAL_KEY}"


def test_tts_stream_uses_saved_defaults_and_params(c, fal_key, monkeypatch):
    database.set_system_setting("tts_voice", "am_liam")
    database.set_system_setting("tts_speed", "1.5")
    database.set_system_setting("tts_model_params", json.dumps({"seed": 7}))
    upstream = install(monkeypatch, _fal_tts_routes("fal-ai/kokoro/american-english"))
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "output_format": "wav"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert upstream.json_of("https://fal.run/") == {"prompt": "hi", "voice": "am_liam", "speed": 1.5, "seed": 7}


def test_tts_stream_request_params_replace_saved_params(c, fal_key, monkeypatch):
    database.set_system_setting("tts_model_params", json.dumps({"seed": 7}))
    upstream = install(monkeypatch, _fal_tts_routes("fal-ai/kokoro/american-english"))
    c.post(f"{BASE}/tts/stream", json={"text": "hi", "params": {}})
    assert "seed" not in upstream.json_of("https://fal.run/")


@pytest.mark.parametrize("model,expected", [
    ("fal-ai/minimax/speech-2.8-hd",
     {"prompt": "hi", "output_format": "url", "voice_setting": {"voice_id": "Wise_Woman", "speed": 1.2}}),
    ("fal-ai/minimax/speech-02-hd",
     {"text": "hi", "output_format": "url", "voice_setting": {"voice_id": "Wise_Woman", "speed": 1.2}}),
    ("fal-ai/chatterbox/text-to-speech", {"text": "hi"}),
    ("fal-ai/dia-tts/voice-clone", {"text": "hi"}),
    ("fal-ai/elevenlabs/tts/turbo-v2.5", {"text": "hi", "voice": "Wise_woman"}),
    ("fal-ai/orpheus-tts", {"text": "hi", "voice": "Wise_Woman"}),
    ("fal-ai/f5-tts", {"gen_text": "hi"}),
    ("fal-ai/vibevoice", {"script": "Speaker 0: hi", "speakers": [{"preset": "Wise_Woman"}]}),
])
def test_tts_stream_builds_each_curated_models_payload(c, fal_key, monkeypatch, model, expected):
    upstream = install(monkeypatch, _fal_tts_routes(model))
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": model, "voice": "Wise_Woman", "speed": 1.2})
    assert r.status_code == 200, r.text
    assert upstream.json_of("https://fal.run/") == expected


def test_tts_stream_uncurated_model_maps_onto_its_schema(c, fal_key, monkeypatch):
    seen = {}

    async def get_schema(endpoint_id, api_key=None, force=False):
        seen.update(endpoint_id=endpoint_id, api_key=api_key)
        return ModelSchema(endpoint_id=endpoint_id, primary_text_field="input_text", fields=[
            ParamField(name="input_text", is_primary_text=True),
            ParamField(name="voice_id"),
            ParamField(name="rate", type="number", minimum=0.5, maximum=2.0),
        ])

    monkeypatch.setattr(settings_audio.schema_service, "get_schema", get_schema)
    database.set_system_setting("tts_model_params", json.dumps({"voice_id": "chosen-in-form"}))
    model = "fal-ai/brand-new-tts"
    upstream = install(monkeypatch, _fal_tts_routes(model, {"audio_url": CDN_URL}))
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": model, "voice": "v1", "speed": 9})
    assert r.status_code == 200, r.text
    assert seen == {"endpoint_id": model, "api_key": FAL_KEY}
    # Speed is clamped to the schema's maximum; saved form params win over the inferred voice.
    assert upstream.json_of("https://fal.run/") == {"input_text": "hi", "voice_id": "chosen-in-form", "rate": 2.0}


def test_tts_stream_uncurated_model_without_schema_falls_back_to_text(c, fal_key, monkeypatch):
    async def no_schema(endpoint_id, api_key=None, force=False):
        return None

    monkeypatch.setattr(settings_audio.schema_service, "get_schema", no_schema)
    upstream = install(monkeypatch, _fal_tts_routes("fal-ai/mystery", {"audio": CDN_URL}))
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": "fal-ai/mystery", "voice": "v"})
    assert r.status_code == 200
    assert upstream.json_of("https://fal.run/") == {"text": "hi", "voice": "v"}


def test_tts_stream_schema_without_speed_bounds_uses_default_range(c, fal_key, monkeypatch):
    async def get_schema(endpoint_id, api_key=None, force=False):
        return ModelSchema(endpoint_id=endpoint_id, fields=[ParamField(name="prompt"), ParamField(name="speed")])

    monkeypatch.setattr(settings_audio.schema_service, "get_schema", get_schema)
    upstream = install(monkeypatch, _fal_tts_routes("fal-ai/other"))
    c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": "fal-ai/other", "speed": 0.01, "params": {}})
    assert upstream.json_of("https://fal.run/") == {"prompt": "hi", "speed": 0.1}


@pytest.mark.parametrize("status,expected,detail", [
    (401, 400, "fal.ai API key is invalid."),
    (429, 429, "Rate limit exceeded."),
    (500, 500, "TTS generation failed: model crashed"),
])
def test_tts_stream_maps_fal_errors(c, fal_key, monkeypatch, status, expected, detail):
    install(monkeypatch, {("POST", "https://fal.run/"): httpx.Response(status, text="model crashed")})
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == expected
    assert r.json()["detail"] == detail


def test_tts_stream_without_audio_url_is_500(c, fal_key, monkeypatch):
    install(monkeypatch, {("POST", "https://fal.run/"): httpx.Response(200, json={"audio": {"url": ""}})})
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == 500
    assert r.json()["detail"] == "No audio URL in fal.ai response"


def test_tts_stream_failed_download_is_500(c, fal_key, monkeypatch):
    install(monkeypatch, {
        ("POST", "https://fal.run/"): httpx.Response(200, json={"audio": {"url": CDN_URL}}),
        ("GET", CDN_URL): httpx.Response(404),
    })
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Failed to download generated audio"


def test_tts_stream_timeout_is_504_and_other_errors_500(c, fal_key, monkeypatch):
    install(monkeypatch, {("POST", "https://fal.run/"): httpx.ConnectTimeout("slow")})
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == 504
    assert r.json()["detail"] == "TTS generation timed out. Try shorter text."
    install(monkeypatch, {("POST", "https://fal.run/"): httpx.ConnectError("down")})
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == 500
    assert r.json()["detail"] == "TTS generation failed"


@pytest.mark.parametrize("text,detail", [
    ("   ", "Text cannot be empty"),
    ("x" * 4097, "Text too long. Maximum 4096 characters."),
])
def test_tts_stream_validates_text(c, fal_key, monkeypatch, text, detail):
    upstream = install(monkeypatch, {})
    r = c.post(f"{BASE}/tts/stream", json={"text": text})
    assert r.status_code == 400
    assert r.json()["detail"] == detail
    assert upstream.calls == []


def test_tts_stream_requires_the_key_of_the_models_provider(c, fal_key, openai_row, monkeypatch):
    upstream = install(monkeypatch, {})
    database.delete_system_setting("fal_api_key")
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("fal.ai API key not configured")
    database.set_system_setting("fal_api_key", encryption.encrypt_value(FAL_KEY))
    database.delete_model_provider("openai-audio")
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": "openai/tts-1"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("No OpenAI provider with a stored API key")
    assert upstream.calls == []


def test_tts_stream_openai_model_maps_voice_and_format(c, openai_row, monkeypatch):
    upstream = install(monkeypatch, {
        ("POST", settings_audio.OPENAI_SPEECH_URL): httpx.Response(200, content=b"OggS-opus"),
    })
    # The saved fal voice is not an OpenAI voice, so the model's first voice is used.
    database.set_system_setting("tts_voice", "af_heart")
    r = c.post(f"{BASE}/tts/stream", json={
        "text": "hi", "model": "openai/tts-1-hd", "speed": 1.5, "output_format": "opus"})
    assert r.status_code == 200, r.text
    assert r.content == b"OggS-opus"
    assert r.headers["content-type"] == "audio/opus"
    assert upstream.json_of(settings_audio.OPENAI_SPEECH_URL) == {
        "model": "tts-1-hd", "input": "hi", "voice": "alloy", "response_format": "opus", "speed": 1.5,
    }
    assert upstream.request_to(settings_audio.OPENAI_SPEECH_URL).headers["Authorization"] == f"Bearer {OPENAI_KEY}"


@pytest.mark.parametrize("status,expected", [(401, 400), (429, 429), (500, 500)])
def test_tts_stream_maps_openai_errors(c, openai_row, monkeypatch, status, expected):
    install(monkeypatch, {("POST", settings_audio.OPENAI_SPEECH_URL): httpx.Response(status, text="upstream")})
    r = c.post(f"{BASE}/tts/stream", json={"text": "hi", "model": "openai/gpt-4o-mini-tts", "voice": "nova"})
    assert r.status_code == expected
    assert OPENAI_KEY not in r.text


def test_tts_stream_requires_authentication(client):
    assert client.post(f"{BASE}/tts/stream", json={"text": "hi"}).status_code in (401, 403)
