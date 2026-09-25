"""
Guarded access to an SDK-style client's subprocess via private attributes.

Callers pass ``adapter.sdk_client``; ``HarnessAdapter`` has no such attribute,
so they get ``None`` and never construct a handle. Kept as the one place that
reads these internals:
  - client._transport._process          (direct path)
  - client._query.transport._process    (via Query object)
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# The SDK version these internals were last verified against.
# Bump this when you verify a new version still has the same attribute layout.
_SDK_VERSION_VERIFIED = "0.1.50"


def _extract_process(client: object) -> Optional[object]:
    """Extract the underlying anyio.abc.Process from an SDK client.

    Tries two known attribute chains and returns the first that succeeds.
    Returns None (never raises) if the SDK internals have changed.
    """
    # Primary path: client._transport._process
    try:
        transport = getattr(client, '_transport', None)
        if transport is not None:
            process = getattr(transport, '_process', None)
            if process is not None:
                return process
    except Exception:
        pass

    # Fallback path: client._query.transport._process
    try:
        query = getattr(client, '_query', None)
        if query is not None:
            transport = getattr(query, 'transport', None)
            if transport is not None:
                process = getattr(transport, '_process', None)
                if process is not None:
                    return process
    except Exception:
        pass

    return None


class SdkProcessHandle:
    """Safe, version-guarded handle to an SDK client's subprocess.

    Usage:
        handle = SdkProcessHandle(client)
        if handle.is_alive():
            await handle.wait(timeout=5.0)

    All methods degrade gracefully (return None/False) if the SDK internals
    have changed. No method raises on private attribute access failure.
    """

    __slots__ = ("_process",)

    def __init__(self, client: object) -> None:
        self._process = _extract_process(client)

    @property
    def available(self) -> bool:
        """True if we could extract the subprocess from the SDK client."""
        return self._process is not None

    def is_alive(self) -> bool:
        """True if the subprocess exists and has not exited."""
        if self._process is None:
            return False
        try:
            return self._process.returncode is None
        except (AttributeError, Exception):
            return False

    @property
    def pid(self) -> Optional[int]:
        """PID of the subprocess, or None if not accessible."""
        if self._process is None:
            return None
        try:
            return self._process.pid
        except (AttributeError, Exception):
            return None

    @property
    def returncode(self) -> Optional[int]:
        """Subprocess exit code, or None if still running / not accessible."""
        if self._process is None:
            return None
        try:
            return self._process.returncode
        except (AttributeError, Exception):
            return None

    async def wait(self, timeout: float) -> Optional[int]:
        """Wait for the subprocess to exit.

        Returns the returncode on success, None if process not accessible.
        Raises asyncio.TimeoutError if the process doesn't exit in time.
        """
        if self._process is None:
            return None
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
            return self._process.returncode
        except asyncio.TimeoutError:
            raise
        except (AttributeError, Exception) as e:
            logger.warning(f"SDK process wait failed: {e}")
            return None

    def terminate(self) -> bool:
        """Send SIGTERM (Unix) / TerminateProcess (Windows).

        Returns True if signal was sent.
        """
        if self._process is None:
            return False
        try:
            self._process.terminate()
            return True
        except (AttributeError, OSError, Exception) as e:
            logger.warning(f"Failed to terminate SDK process: {e}")
            return False

    def kill(self) -> bool:
        """Send SIGKILL (Unix) / TerminateProcess (Windows).

        Returns True if signal was sent.
        """
        if self._process is None:
            return False
        try:
            self._process.kill()
            return True
        except (AttributeError, OSError, Exception) as e:
            logger.warning(f"Failed to kill SDK process: {e}")
            return False
