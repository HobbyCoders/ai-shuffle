"""
Persistent Node.js Worker Manager.

Manages a long-running Node.js subprocess that handles AI tool execution.
Replaces the fragile pattern of creating temp .mjs files for each tool call.

The worker process is started lazily on first tool call and communicates
via newline-delimited JSON over stdin/stdout. Multiple concurrent tool calls
are multiplexed via unique request IDs.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class NodeWorker:
    """Manages a persistent Node.js worker process for AI tool execution."""

    def __init__(
        self,
        worker_script: str,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ):
        self._worker_script = worker_script
        self._env = env or {}
        self._cwd = cwd
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._started = False

    async def _start(self) -> None:
        """Start the worker subprocess and wait for ready signal."""
        node_env = os.environ.copy()
        node_env.update(self._env)

        self._process = await asyncio.create_subprocess_exec(
            'node', self._worker_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=node_env,
            cwd=self._cwd,
        )

        # Start response reader and stderr forwarder
        self._reader_task = asyncio.create_task(self._read_responses())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Wait for ready signal (first message from worker)
        ready_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._ready_future = ready_future

        try:
            await asyncio.wait_for(ready_future, timeout=15)
        except asyncio.TimeoutError:
            logger.error("Node worker failed to start within 15s")
            await self._kill()
            raise RuntimeError("Node worker failed to start")

        self._started = True
        logger.info("Node.js AI tools worker started (PID %s)", self._process.pid)

    async def _read_responses(self) -> None:
        """Read JSON responses from worker stdout and dispatch to pending futures."""
        assert self._process and self._process.stdout

        while True:
            try:
                line = await self._process.stdout.readline()
                if not line:
                    break  # Process exited

                text = line.decode().strip()
                if not text:
                    continue

                try:
                    response = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Worker sent non-JSON: %s", text[:200])
                    continue

                # Handle ready signal
                if response.get("ready"):
                    if hasattr(self, '_ready_future') and not self._ready_future.done():
                        self._ready_future.set_result(True)
                    continue

                # Match response to pending request by ID
                req_id = response.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending.pop(req_id)
                    if not future.done():
                        if "error" in response:
                            future.set_result({"error": response["error"]})
                        else:
                            future.set_result(response.get("result", {}))
                else:
                    logger.warning("Worker response for unknown request: %s", req_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error reading worker response: %s", e)
                break

        # Worker died — fail all pending requests
        for req_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result({"error": "Worker process exited unexpectedly"})
        self._pending.clear()
        self._started = False

    async def _read_stderr(self) -> None:
        """Forward worker stderr (tool debug messages) to Python logger."""
        assert self._process and self._process.stderr

        while True:
            try:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if text:
                    logger.debug("[node-worker] %s", text)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def call(self, tool_name: str, args: dict, timeout: float = 300) -> dict:
        """
        Call a tool function on the worker and return the result.

        Starts the worker lazily on first call. Restarts if the worker has crashed.
        Multiple concurrent calls are supported via unique request IDs.

        Args:
            tool_name: Name of the tool function (e.g., "generateImage")
            args: Arguments dict to pass to the tool
            timeout: Max seconds to wait for response (default 5 min for 3D tools)

        Returns:
            Parsed result dict from the tool
        """
        # Ensure worker is running (lock protects startup race)
        async with self._lock:
            if not self._started or self._process is None or self._process.returncode is not None:
                await self._start()

        assert self._process and self._process.stdin

        # Create request with unique ID
        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Send request
        request = json.dumps({"id": req_id, "tool": tool_name, "args": args})
        try:
            self._process.stdin.write((request + "\n").encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._pending.pop(req_id, None)
            self._started = False
            return {"error": "Worker process died before request could be sent"}

        # Wait for response
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            logger.error("Tool %s timed out after %ss", tool_name, timeout)
            return {"error": f"Tool {tool_name} timed out after {int(timeout)} seconds"}

    async def shutdown(self) -> None:
        """Shut down the worker process gracefully."""
        if self._process and self._process.returncode is None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            logger.info("Node worker shut down (PID %s)", self._process.pid)

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        self._started = False

    async def _kill(self) -> None:
        """Force-kill the worker process."""
        if self._process and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._started = False
