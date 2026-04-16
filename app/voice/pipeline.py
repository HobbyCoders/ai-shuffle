"""
Realtime voice — Pipecat pipeline factory.

Assembles the STT → LLM → TTS pipeline with VAD/turn detection and
wires Pipecat metrics for latency observability (Phase 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.voice.providers import (
    VoiceConfig,
    build_stt_service,
    build_tts_service,
    build_llm_service,
)

logger = logging.getLogger(__name__)


@dataclass
class PipelineComponents:
    """Pipecat pipeline + task handle + service refs for later inspection."""
    pipeline: Any
    task: Any
    runner: Any
    stt: Any
    tts: Any
    llm: Any
    context_aggregator: Any


def build_pipeline(
    transport: Any,
    cfg: VoiceConfig,
    system_prompt: str,
    on_transcript: Optional[Callable[[str, bool], None]] = None,
    session_options: Optional[Any] = None,
    tool_observer: Optional[Callable[[str, str, Any], None]] = None,
    on_tts_chars: Optional[Callable[[int], None]] = None,
) -> PipelineComponents:
    """
    Build a Pipecat pipeline. `transport` is a SmallWebRTCTransport
    (or any BaseTransport) already bound to the peer connection.

    Args:
        transport: Pipecat transport (input + output)
        cfg: resolved VoiceConfig
        system_prompt: prepended to LLM context
        on_transcript: optional hook — called on interim + final transcripts
            signature: (text, is_final)

    Returns: PipelineComponents
    """
    # Defer imports — Pipecat is an optional runtime dependency
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.openai_llm_context import (
        OpenAILLMContext,
    )

    stt = build_stt_service(cfg)
    tts = build_tts_service(cfg)
    llm = build_llm_service(
        cfg,
        session_options=session_options,
        tool_observer=tool_observer,
    )

    # Seed context with system prompt.
    context = OpenAILLMContext(
        messages=[{"role": "system", "content": system_prompt}]
    )
    context_aggregator = llm.create_context_aggregator(context)

    # Optional transcript tap — publish partial/final transcripts upstream.
    processors: list[Any] = [
        transport.input(),
        stt,
    ]

    if on_transcript is not None:
        from app.voice.processors.transcript_tap import TranscriptTap
        processors.append(TranscriptTap(on_transcript))

    processors.extend([
        context_aggregator.user(),
        llm,
    ])

    # Optional cost/usage tap — counts chars entering TTS.
    if on_tts_chars is not None:
        from app.voice.processors.tts_char_counter import TTSCharCounter
        processors.append(TTSCharCounter(on_tts_chars))

    processors.extend([
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=cfg.interruptions_enabled,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )
    runner = PipelineRunner()

    return PipelineComponents(
        pipeline=pipeline,
        task=task,
        runner=runner,
        stt=stt,
        tts=tts,
        llm=llm,
        context_aggregator=context_aggregator,
    )


def default_system_prompt() -> str:
    """Baseline system prompt for Phase 1 standalone voice demo."""
    return (
        "You are a helpful, concise voice assistant. "
        "Keep replies short — 1–2 sentences unless asked for detail. "
        "Speak naturally; avoid markdown, lists, and code formatting. "
        "If the user asks a question you cannot answer from general knowledge, "
        "say so briefly rather than speculating."
    )
