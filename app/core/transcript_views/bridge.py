"""Bounded private IPC for the shipped authoritative TypeScript reducer.

No credentials, provider configuration or inherited NODE_OPTIONS reach Node.
Failure, timeout, stale code or absence of Node makes projections unavailable;
the existing full-replay serving path is deliberately untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .child_process import SAFE_ENV_KEYS as SAFE_ENV_KEYS
from .child_process import launch, stop
from .preparation import PreparationBusy, PreparationUnavailable, PreparationWorker

MAX_PACKET_BYTES = 24 * 1024 * 1024


class ReducerUnavailable(RuntimeError):
    """A fixed diagnostic code, never a Node exception or a transcript payload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def verified_manifest(directory: Path | None = None) -> dict[str, Any]:
    directory = directory or Path(__file__).parent
    root = directory.resolve().parents[2]
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or manifest.get("minimum_node_major") != 22:
            raise ReducerUnavailable("bundle_version")
        actual = hashlib.sha256((directory / "reducer.cjs").read_bytes()).hexdigest()
        if actual != manifest["bundle_sha256"]:
            raise ReducerUnavailable("bundle_changed")
        sources = manifest["canonical_sources"]
        if not isinstance(sources, dict) or not sources:
            raise ReducerUnavailable("bundle_version")
        for relative, expected in sources.items():
            source = root / relative
            # Only checked-in app modules, never paths from an RPC request.
            if not relative.startswith("app/") or ".." in Path(relative).parts:
                raise ReducerUnavailable("bundle_version")
            data = source.read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(data).hexdigest() != expected:
                raise ReducerUnavailable("preprocessor_changed")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ReducerUnavailable("bundle_unavailable") from error


def _encode_packet(sequence: int, method: str, params: dict[str, Any]) -> bytes:
    return json.dumps({"id": sequence, "method": method, "params": params},
                      ensure_ascii=True, allow_nan=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"


class ReducerBridge:
    """One sequential process, bounded packets/deadlines, no hidden request queue.

    A timeout/cancellation kills this process before another request can start,
    so a late response can never be mistaken for a newer fold.
    """

    def __init__(self, *, node: str | None = None, directory: Path | None = None,
                 timeout: float = 3.0) -> None:
        self.directory = directory or Path(__file__).parent
        self.node = node if node is not None else shutil.which("node")
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self.fingerprint: str | None = None
        self._preparation = PreparationWorker()

    async def prepare(self, call, *args, **kwargs):
        try:
            return await self._preparation.run(call, *args, **kwargs)
        except PreparationBusy as error:
            raise ReducerUnavailable("preparation_busy") from error
        except PreparationUnavailable as error:
            raise ReducerUnavailable("preparation_unavailable") from error

    async def _stop(self) -> None:
        process, self._process = self._process, None
        await stop(process)

    async def close(self) -> None:
        async with self._lock:
            try:
                await self._stop()
            finally:
                await self._preparation.close()

    async def _start(self) -> None:
        if self._process is not None:
            if self._process.returncode is None:
                return
            await self._stop()
        if not self.node:
            raise ReducerUnavailable("node_missing")
        manifest = await self.prepare(verified_manifest, self.directory)
        try:
            self._process = await launch(
                self.node, "--max-old-space-size=192", str(self.directory / "reducer.cjs"),
                limit=MAX_PACKET_BYTES + 1, runtime="node")
            assert self._process.stdout is not None
            line = await asyncio.wait_for(self._process.stdout.readline(), self.timeout)
            ready = json.loads(line)
            if (not isinstance(ready, dict) or not isinstance(ready.get("node"), str) or
                    ready.get("type") != "ready" or ready.get("version") != 1 or
                    ready.get("fingerprint") != manifest["fingerprint"] or
                    int(ready["node"].split(".")[0]) < 22):
                raise ReducerUnavailable("node_protocol")
            self.fingerprint = manifest["fingerprint"]
            self._sequence = 0
        except asyncio.CancelledError:
            await self._stop()
            raise
        except (OSError, ValueError, TypeError, asyncio.TimeoutError) as error:
            await self._stop()
            raise ReducerUnavailable("node_unavailable") from error
        except ReducerUnavailable:
            await self._stop()
            raise

    async def request(self, method: str, **params: Any) -> Any:
        # Callers are one bounded producer. Refuse parallel use rather than
        # growing an asyncio waiter queue outside that producer's back-pressure.
        if self._lock.locked():
            raise ReducerUnavailable("node_busy")
        async with self._lock:
            await self._start()
            self._sequence += 1
            try:
                packet = await self.prepare(_encode_packet, self._sequence, method, params)
                if len(packet) > MAX_PACKET_BYTES:
                    raise ReducerUnavailable("node_input_limit")
                assert self._process is not None and self._process.stdin is not None
                assert self._process.stdout is not None

                async def exchange() -> dict[str, Any]:
                    assert self._process is not None and self._process.stdin is not None
                    assert self._process.stdout is not None
                    self._process.stdin.write(packet)
                    await self._process.stdin.drain()
                    line = await self._process.stdout.readline()
                    if len(line) > MAX_PACKET_BYTES:
                        raise ReducerUnavailable("node_output_limit")
                    response = await self.prepare(json.loads, line)
                    if not isinstance(response, dict) or response.get("id") != self._sequence:
                        raise ReducerUnavailable("node_protocol")
                    return response

                response = await asyncio.wait_for(exchange(), self.timeout)
                if response.get("ok") is not True:
                    # Never forward raw exception text or payload-derived codes.
                    raise ReducerUnavailable("fold_rejected")
                return response["result"]
            except asyncio.CancelledError:
                await self._stop()
                raise
            except ReducerUnavailable:
                await self._stop()
                raise
            except (OSError, ValueError, TypeError, KeyError, asyncio.TimeoutError) as error:
                await self._stop()
                raise ReducerUnavailable("node_unavailable") from error
