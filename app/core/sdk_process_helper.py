"""
Wrapper for Claude Agent SDK private attribute access.

The SDK doesn't expose a public API for process management (PID, kill, etc.).
We access private attributes as a workaround. This module centralizes that
access so it's easy to update when the SDK changes.

Known fragility: These attributes may change without notice in SDK updates.
TODO: Request public API from Anthropic for process management.

Attribute chain: client._query.transport._process
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_sdk_process(client) -> Optional[object]:
    """Get the underlying subprocess from an SDK client.

    Accesses: client._query.transport._process
    Returns None if the attribute chain is broken (SDK version mismatch).
    """
    try:
        query = getattr(client, '_query', None)
        transport = getattr(query, 'transport', None) if query else None
        process = getattr(transport, '_process', None) if transport else None
        return process
    except AttributeError as e:
        logger.warning(f"SDK private attribute access failed: {e}. "
                       f"SDK version may have changed.")
        return None


def get_sdk_pid(client) -> Optional[int]:
    """Get the PID of the SDK subprocess.

    Returns None if process is not accessible or not running.
    """
    process = get_sdk_process(client)
    if process is None:
        return None
    try:
        return process.pid
    except AttributeError:
        return None


def is_sdk_process_running(client) -> bool:
    """Check if the SDK subprocess is still running.

    Returns True only if the process exists and has not exited (returncode is None).
    """
    process = get_sdk_process(client)
    if process is None:
        return False
    try:
        return process.returncode is None
    except AttributeError:
        return False


async def wait_for_sdk_process(client, timeout: float) -> Optional[int]:
    """Wait for the SDK subprocess to exit, with a timeout.

    Returns the returncode if the process exited within the timeout,
    or None if the process is not accessible.

    Raises asyncio.TimeoutError if the process doesn't exit in time.
    """
    process = get_sdk_process(client)
    if process is None:
        return None
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return process.returncode
    except AttributeError as e:
        logger.warning(f"SDK process wait failed: {e}")
        return None


def get_sdk_returncode(client) -> Optional[int]:
    """Get the return code of the SDK subprocess.

    Returns None if the process is still running, not accessible, or
    the attribute doesn't exist.
    """
    process = get_sdk_process(client)
    if process is None:
        return None
    try:
        return process.returncode
    except AttributeError:
        return None


def kill_sdk_process(client) -> bool:
    """Kill the SDK subprocess (SIGKILL on Unix, TerminateProcess on Windows).

    Returns True if process was killed, False if not accessible.
    """
    process = get_sdk_process(client)
    if process is None:
        return False
    try:
        process.kill()
        return True
    except (AttributeError, OSError) as e:
        logger.warning(f"Failed to kill SDK process: {e}")
        return False


def terminate_sdk_process(client) -> bool:
    """Terminate the SDK subprocess (SIGTERM on Unix, TerminateProcess on Windows).

    Returns True if signal was sent, False if not accessible.
    """
    process = get_sdk_process(client)
    if process is None:
        return False
    try:
        process.terminate()
        return True
    except (AttributeError, OSError) as e:
        logger.warning(f"Failed to terminate SDK process: {e}")
        return False


def send_signal_to_sdk(client, signal: int) -> bool:
    """Send a signal to the SDK subprocess.

    Returns True if signal was sent, False if not accessible.
    """
    process = get_sdk_process(client)
    if process is None:
        return False
    try:
        process.send_signal(signal)
        return True
    except (AttributeError, OSError) as e:
        logger.warning(f"Failed to send signal {signal} to SDK process: {e}")
        return False
