"""Validated per-model voice controls, shared by settings and call producers."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PromptOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: str = Field(default="", max_length=16000)


class RealtimeOptions(PromptOptions):
    transcription_model: Literal[
        "gpt-realtime-whisper",
        "gpt-transcribe",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "whisper-1",
    ] = "gpt-4o-mini-transcribe"
    max_output_tokens: int | Literal["inf"] = Field(default="inf")
    turn_detection: Literal["server_vad", "semantic_vad"] = "semantic_vad"
    eagerness: Literal["auto", "low", "medium", "high"] = "medium"
    threshold: float = Field(default=0.5, ge=0, le=1)
    prefix_padding_ms: int = Field(default=300, ge=0, le=5000)
    silence_duration_ms: int = Field(default=500, ge=100, le=5000)
    idle_timeout_ms: int | None = Field(default=None, ge=5000, le=30000)


class LiveOptions(PromptOptions):
    opening: str = Field(default="", max_length=2000)
    delegation_type: Literal["client", "responses"] = "client"
    delegation_model: str = Field(default="", max_length=500)
    delegation_effort: str = Field(default="", max_length=30)
    delegation_instructions: str = Field(default="", max_length=16000)


def validate_options(model: str, value: dict) -> dict:
    from app.core.voice_models import LIVE_MODEL, PERSONAPLEX_MODEL

    cls = (
        LiveOptions
        if model == LIVE_MODEL
        else PromptOptions
        if model == PERSONAPLEX_MODEL
        else RealtimeOptions
    )
    options = cls.model_validate(value).model_dump()
    if cls is RealtimeOptions:
        tokens = options["max_output_tokens"]
        if tokens != "inf" and (isinstance(tokens, bool) or not 1 <= tokens <= 4096):
            raise ValueError("Max output tokens must be 1–4096, or inf.")
    return options


def realtime_session_options(options: dict | None = None) -> dict:
    """Exact GA configuration used at mint AND sideband attach/reconnect.

    Keep both VAD modes' saved tuning, but never send incompatible fields.
    Always keep automatic response generation and interruption enabled.
    """
    o = validate_options("gpt-realtime", options or {})
    vad = {
        "type": o["turn_detection"],
        "interrupt_response": True,
        "create_response": True,
    }
    if vad["type"] == "semantic_vad":
        vad["eagerness"] = o["eagerness"]
    else:
        vad.update(
            {k: o[k] for k in ("threshold", "prefix_padding_ms", "silence_duration_ms")}
        )
        if o["idle_timeout_ms"] is not None:
            vad["idle_timeout_ms"] = o["idle_timeout_ms"]
    return {
        "max_output_tokens": o["max_output_tokens"],
        "audio": {
            "input": {
                "transcription": {"model": o["transcription_model"]},
                "turn_detection": vad,
            }
        },
    }


def effective_options(config: dict) -> dict:
    return validate_options(config["model"], config.get("options") or {})
