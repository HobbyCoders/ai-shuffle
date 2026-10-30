"""Real child-boundary regressions; probe only a synthetic, credential-free env."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE_ENTRY = b"REVIEW_TRANSCRIPT_FAKE_SECRET=fixture-only"


def child_stats():
    return {"pid": os.getpid(), "secret_inherited": "REVIEW_TRANSCRIPT_FAKE_SECRET" in os.environ,
            "api_modules": [name for name in sys.modules if name.startswith("app.api")],
            "main_imported": "app.main" in sys.modules}


def blocked(marker):
    Path(marker).touch()
    time.sleep(30)


PARENT = r'''
import asyncio, importlib.util, json, os, sys, time
from multiprocessing import resource_tracker
from app.core.transcript_views.bridge import ReducerBridge
from tests.test_transcript_projection_child_boundary import child_stats, blocked

async def main():
    # Exactly the production spawn hazard: multiprocessing reconstructs this
    # entrypoint even though the preparation operation needs no application.
    sys.modules["__main__"].__spec__ = importlib.util.find_spec("app.main")
    bridge = ReducerBridge()
    start = time.perf_counter()
    stats = await bridge.prepare(child_stats)
    stats["cold_seconds"] = time.perf_counter() - start
    await bridge.request("plan", carry=None, frames=[])
    stats["node"] = bridge._process.pid
    stats["tracker"] = resource_tracker._resource_tracker._pid
    if sys.argv[1] == "blocked":
        asyncio.create_task(bridge.prepare(blocked, sys.argv[2]))
        while not os.path.exists(sys.argv[2]):
            await asyncio.sleep(.005)
    print(json.dumps(stats), flush=True)
    # The supervisor kills this parent without giving close()/finally a turn.
    sys.stdin.read(1)
    await bridge.close()

asyncio.run(main())
'''


async def parent(tmp_path, mode="idle"):
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "Path", "SystemRoot", "WINDIR", "TMP", "TEMP"}}
    environment.update(TESTING="1", AI_SHUFFLE_ENV_FILE=os.devnull,
                       PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "fluid"))),
                       DATA_DIR=str(tmp_path / "data"), REVIEW_TRANSCRIPT_FAKE_SECRET="fixture-only")
    # Files, not inherited output pipes: the pre-fix orphan helper retains
    # stdout/stderr, so awaiting their EOF would hide the regression in a hang.
    output = tmp_path / "parent-output"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", PARENT, mode, str(tmp_path / "entered"), cwd=ROOT,
            env=environment, stdin=asyncio.subprocess.PIPE, stdout=stream, stderr=stream)
    descendants = []
    try:
        async def ready():
            nonlocal descendants
            while True:
                descendants = psutil.Process(process.pid).children(recursive=True)
                text = output.read_text()
                for line in text.splitlines():
                    if line.startswith('{'):
                        return json.loads(line)
                assert process.returncode is None, text
                await asyncio.sleep(.01)
        return process, await asyncio.wait_for(ready(), 15)
    except BaseException:
        for child in descendants:
            if child.is_running():
                child.kill()
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


def alive(pid):
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def cleanup(process, stats):
    # Only the parent/children created by this fixture, never a shared worker.
    for key in ("pid", "node", "tracker"):
        pid = stats.get(key)
        if pid and alive(pid):
            psutil.Process(pid).kill()
    if process.returncode is None:
        process.kill()
    await process.wait()


@pytest.mark.asyncio
async def test_lightweight_cold_child_does_not_reconstruct_production_main(tmp_path):
    process, stats = await parent(tmp_path)
    try:
        assert not stats["main_imported"]
        assert stats["api_modules"] == []
        assert stats["tracker"] is None
        # Structural import assertions remain portable; this Linux cold budget
        # also catches accidental site/application startup outside app.api.
        if sys.platform == "linux":
            assert stats["cold_seconds"] < 2.0, stats["cold_seconds"]
        print(f"preparation cold call: {stats['cold_seconds']:.3f}s; no application imports")
    finally:
        await cleanup(process, stats)


@pytest.mark.asyncio
async def test_same_uid_sibling_cannot_find_fake_credential_in_either_child(tmp_path):
    process, stats = await parent(tmp_path)
    try:
        assert not stats["secret_inherited"]
        assert stats["tracker"] is None  # No hidden third credential boundary.
        if sys.platform == "linux":
            assert psutil.Process(process.pid).uids().real == os.getuid()
            for key in ("pid", "node"):
                assert psutil.Process(stats[key]).uids().real == os.getuid()
                try:
                    contents = Path(f"/proc/{stats[key]}/environ").read_bytes()
                except PermissionError:
                    continue
                # Never print an environment, including on assertion failure.
                visible = FAKE_ENTRY in contents.split(b"\0")
                assert not visible, f"synthetic credential readable from {key}"
    finally:
        await cleanup(process, stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idle", "blocked"])
async def test_abrupt_parent_death_stops_all_owned_children(tmp_path, mode):
    process, stats = await parent(tmp_path, mode)
    try:
        assert alive(stats["pid"]) and alive(stats["node"])
        process.kill()
        await process.wait()
        deadline = time.monotonic() + 3
        pids = [stats[key] for key in ("pid", "node", "tracker") if stats.get(key)]
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert not any(alive(pid) for pid in pids), {pid: alive(pid) for pid in pids}
    finally:
        await cleanup(process, stats)
