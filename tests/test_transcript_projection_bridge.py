from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app.core.transcript_views.bridge import (
    SAFE_ENV_KEYS,
    ReducerBridge,
    ReducerUnavailable,
    verified_manifest,
)

BUNDLE = Path(__file__).resolve().parents[1] / "app/core/transcript_views"


def copy_bundle(tmp_path: Path) -> Path:
    directory = tmp_path / "app/core/transcript_views"
    directory.mkdir(parents=True)
    manifest = verified_manifest()
    for relative in manifest["canonical_sources"]:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((BUNDLE.parents[2] / relative).read_bytes())
    for name in ("reducer.cjs", "manifest.json"):
        shutil.copyfile(BUNDLE / name, directory / name)
    return directory


def hanging_bundle(tmp_path: Path) -> Path:
    directory = copy_bundle(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    # A private fixture sends the real handshake, answers once, then hangs.
    script = (
        "const r=require('node:readline').createInterface({input:process.stdin});"
        "let n=0;console.log(JSON.stringify({type:'ready',version:1,fingerprint:"
        + json.dumps(manifest["fingerprint"]) + ",node:process.versions.node}));"
        "r.on('line',line=>{const x=JSON.parse(line);if(++n===1)"
        "console.log(JSON.stringify({id:x.id,ok:true,result:[]}));});"
    )
    (directory / "reducer.cjs").write_text(script, encoding="utf-8")
    manifest["bundle_sha256"] = hashlib.sha256(script.encode()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_manifest_matches_current_bundle_and_canonical_input():
    manifest = verified_manifest()
    assert len(manifest["fingerprint"]) == 64
    assert manifest["minimum_node_major"] == 22


def test_stale_bundle_and_preprocessor_fail_closed(tmp_path):
    directory = copy_bundle(tmp_path)
    (directory / "reducer.cjs").write_text("changed", encoding="utf-8")
    with pytest.raises(ReducerUnavailable, match="bundle_changed"):
        verified_manifest(directory)
    directory = copy_bundle(tmp_path / "second")
    (directory.parents[2] / "app/api/events.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ReducerUnavailable, match="preprocessor_changed"):
        verified_manifest(directory)


@pytest.mark.asyncio
async def test_real_node_round_trip_and_credential_free_environment(monkeypatch):
    original = asyncio.create_subprocess_exec
    child_keys = set()

    async def spawn(*args, **options):
        child_keys.update(options["env"])
        return await original(*args, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("NODE_OPTIONS", "fixture-must-not-reach-node")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-must-not-reach-node")
    bridge = ReducerBridge()
    try:
        frames = [{"id": "user", "seq": 1, "type": "message.user", "content": "Run"}]
        assert isinstance(await bridge.request("plan", carry=None, frames=frames), list)
        result = await bridge.request("fold", carry=None, frames=frames, records=[])
        assert result["preview"]["lastSeq"] == 1
        assert result["boundary"]["lastSeq"] == 1
        assert child_keys <= SAFE_ENV_KEYS
        assert child_keys.isdisjoint({"NODE_OPTIONS", "OPENAI_API_KEY", "DATABASE_URL"})
    finally:
        await bridge.close()
    assert bridge._process is None


@pytest.mark.asyncio
async def test_crashed_preparation_fails_closed_then_restarts_its_own_child():
    bridge = ReducerBridge()
    try:
        assert await bridge.request('plan', carry=None, frames=[]) == []
        node = bridge._process
        with pytest.raises(ReducerUnavailable, match='preparation_unavailable'):
            await bridge.prepare(os._exit, 1)
        assert await bridge.request('plan', carry=None, frames=[]) == []
        assert bridge._process is node
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_missing_node_is_optional_not_an_import_or_serving_failure():
    bridge = ReducerBridge(node="")
    with pytest.raises(ReducerUnavailable, match="node_missing"):
        await bridge.request("plan", carry=None, frames=[])
    await bridge.close()


@pytest.mark.asyncio
async def test_killed_process_is_restarted_without_reusing_responses():
    bridge = ReducerBridge()
    try:
        assert await bridge.request("plan", carry=None, frames=[]) == []
        previous = bridge._process
        previous.kill()
        await previous.wait()
        assert await bridge.request("plan", carry=None, frames=[]) == []
        assert bridge._process.pid != previous.pid
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_timeout_kills_owned_process_and_clears_pending_response(tmp_path):
    bridge = ReducerBridge(directory=hanging_bundle(tmp_path))
    try:
        assert await bridge.request("plan", carry=None, frames=[]) == []
        previous = bridge._process
        bridge.timeout = 0.05
        with pytest.raises(ReducerUnavailable, match="node_unavailable"):
            await bridge.request("plan", carry=None, frames=[])
        assert previous.returncode is not None
        assert bridge._process is None
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_cancellation_and_parallel_backpressure_are_bounded(tmp_path):
    bridge = ReducerBridge(directory=hanging_bundle(tmp_path))
    try:
        assert await bridge.request("plan", carry=None, frames=[]) == []
        previous = bridge._process
        pending = asyncio.create_task(bridge.request("plan", carry=None, frames=[]))
        await asyncio.sleep(0)  # deterministic event-loop handoff, not a timer.
        with pytest.raises(ReducerUnavailable, match="node_busy"):
            await bridge.request("plan", carry=None, frames=[])
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert previous.returncode is not None
        assert bridge._process is None
    finally:
        await bridge.close()
