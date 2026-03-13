"""
Structured logging context for streaming operations.

Provides consistent correlation IDs (session, device, message, request)
across the entire streaming pipeline. Each log line includes a prefix
that makes it trivial to grep for all events in a single streaming session.

Usage:
    ctx = StreamingContext(session_id="abc", device_id="def", message_id="ghi")
    ctx.debug(logger, "Started streaming")
    # Output: [s=abc d=def m=ghi r=jkl012] Started streaming
"""

import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamingContext:
    """Correlation context for a single streaming request."""

    session_id: str
    device_id: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    user_id: Optional[str] = None

    @property
    def log_prefix(self) -> str:
        """Short prefix for structured log lines."""
        s = self.session_id[:12] if self.session_id else "?"
        d = self.device_id[:12] if self.device_id else "?"
        m = self.message_id[:12] if self.message_id else "?"
        return f"[s={s} d={d} m={m} r={self.request_id}]"

    def debug(self, logger: logging.Logger, msg: str) -> None:
        """Log at DEBUG level with correlation prefix."""
        logger.debug(f"{self.log_prefix} {msg}")

    def info(self, logger: logging.Logger, msg: str) -> None:
        """Log at INFO level with correlation prefix."""
        logger.info(f"{self.log_prefix} {msg}")

    def warning(self, logger: logging.Logger, msg: str) -> None:
        """Log at WARNING level with correlation prefix."""
        logger.warning(f"{self.log_prefix} {msg}")

    def error(self, logger: logging.Logger, msg: str, exc_info: bool = False) -> None:
        """Log at ERROR level with correlation prefix."""
        logger.error(f"{self.log_prefix} {msg}", exc_info=exc_info)
