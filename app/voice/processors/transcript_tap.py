"""
TranscriptTap — read-only Pipecat processor that forwards
Transcription frames to an external callback without modifying
the pipeline's frame flow.

Used to push transcripts to the frontend via the voice session
WebSocket for live captioning and persistence.
"""

from __future__ import annotations

import logging
from typing import Callable

try:
    from pipecat.frames.frames import (
        Frame,
        InterimTranscriptionFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError:  # pragma: no cover — deferred dep
    Frame = object  # type: ignore
    InterimTranscriptionFrame = object  # type: ignore
    TranscriptionFrame = object  # type: ignore
    FrameDirection = object  # type: ignore
    FrameProcessor = object  # type: ignore


logger = logging.getLogger(__name__)


class TranscriptTap(FrameProcessor):
    """Non-mutating tap: forwards frames unchanged, fires callback on transcripts."""

    def __init__(self, on_transcript: Callable[[str, bool], None]):
        super().__init__()
        self._on_transcript = on_transcript

    async def process_frame(self, frame, direction):  # type: ignore[override]
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, TranscriptionFrame):
                self._safe_callback(frame.text, True)
            elif isinstance(frame, InterimTranscriptionFrame):
                self._safe_callback(frame.text, False)
        except Exception as e:
            logger.debug("TranscriptTap callback error: %s", e)

        # Always forward — never swallow.
        await self.push_frame(frame, direction)

    def _safe_callback(self, text: str, is_final: bool) -> None:
        try:
            self._on_transcript(text, is_final)
        except Exception as e:
            logger.debug("TranscriptTap user callback raised: %s", e)
