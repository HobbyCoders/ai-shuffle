"""
TTSCharCounter — non-mutating processor that counts characters flowing into
TTS so the session can track cost at hangup.

Place immediately before the TTS service in the pipeline. Counts:
  - TextFrame (assistant → TTS path)
  - LLMTextFrame (streaming LLM tokens)

Does not modify frames.
"""

from __future__ import annotations

import logging
from typing import Callable

try:
    from pipecat.frames.frames import Frame, LLMTextFrame, TextFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError:  # pragma: no cover
    Frame = object  # type: ignore
    LLMTextFrame = object  # type: ignore
    TextFrame = object  # type: ignore
    FrameDirection = object  # type: ignore
    FrameProcessor = object  # type: ignore


logger = logging.getLogger(__name__)


class TTSCharCounter(FrameProcessor):
    """Counts characters that reach the TTS stage."""

    def __init__(self, on_chars: Callable[[int], None]):
        super().__init__()
        self._on_chars = on_chars

    async def process_frame(self, frame, direction):  # type: ignore[override]
        await super().process_frame(frame, direction)
        try:
            text = None
            if isinstance(frame, LLMTextFrame):
                text = getattr(frame, "text", None)
            elif isinstance(frame, TextFrame):
                text = getattr(frame, "text", None)
            if text:
                try:
                    self._on_chars(len(text))
                except Exception:
                    logger.debug("TTSCharCounter callback raised", exc_info=True)
        finally:
            await self.push_frame(frame, direction)
