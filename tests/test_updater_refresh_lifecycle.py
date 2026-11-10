"""Deferred live refresh never stops work; startup handover retains its ownership boundary."""
import asyncio
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="Linux supervisor ownership uses flock")
from docker import application_updates as module
from docker import container_runtime as runtime
from tests.test_application_updates import A, B, NEW_IMAGE, feed, installed


@pytest.fixture
def healthy(feed, monkeypatch):
    updater, publish, *_ = feed
    import copy
    current = installed(updater, publish, image=copy.deepcopy(NEW_IMAGE))
    # An older resident may still pass this mode and retain a queued intent.
    current.refresh_mode = "available"
    current.lifecycle = Mock(side_effect=lambda action: {"active_sessions": 0} if action == "active-sessions" else {"stopped": True})
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    monkeypatch.setattr(runtime, "load_updater", lambda: module)
    monkeypatch.delenv("SHUFFLE_SOURCE_SUPERVISOR", raising=False)
    return current


def test_refresh_is_refused_even_with_signed_healthy_source(healthy):
    assert module.handover_target(healthy.root, healthy.image, healthy.public_key)[0] == B
    with pytest.raises(runtime.RestartRejected) as caught:
        runtime.prepare_updater_refresh(healthy)
    assert "separately approved container restart" in caught.value.reason
    healthy.lifecycle.assert_not_called()
    assert healthy.state == {"active": B}, "refusal does not activate staging or migrate the database"


@pytest.mark.parametrize("gate", ["isolation", "disabled", "policy", "busy", "booting", "blocked", "pending", "trial", "signature", "capability"])
def test_refresh_preflight_fails_closed_without_quiescing(healthy, monkeypatch, gate):
    if gate == "isolation":
        monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=object()))
    elif gate == "disabled":
        monkeypatch.setenv("SHUFFLE_SOURCE_SUPERVISOR", "0")
    elif gate == "policy":
        healthy.allowed = lambda: False
    elif gate == "busy":
        healthy.lifecycle.side_effect = lambda _: {"active_sessions": 1}
    elif gate in ("booting", "blocked", "pending"):
        healthy.state[gate] = True if gate == "blocked" else B
        healthy.save()
    elif gate == "trial":
        (healthy.root / "handover.json").write_text(json.dumps({"revision": B}))
    elif gate == "signature":
        (healthy.directory(B) / "source/docker/container_runtime.py").write_text("tampered")
    elif gate == "capability":
        healthy.image["provides"] = []
    with pytest.raises(runtime.RestartRejected):
        runtime.prepare_updater_refresh(healthy)
    healthy.lifecycle.assert_not_called()


@pytest.mark.parametrize("isolated,disabled,capability,expected", [
    (False, False, True, "container_restart_required"),
    (False, True, True, "disabled"),
    (True, False, True, "image_update_required"),
    (False, False, False, "image_update_required"),
])
def test_refresh_mode_never_offers_live_action(healthy, monkeypatch, isolated, disabled, capability, expected):
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=object() if isolated else None))
    monkeypatch.setenv("SHUFFLE_SOURCE_SUPERVISOR", "0" if disabled else "1")
    if not capability:
        healthy.image["provides"] = []
    assert runtime.updater_refresh_mode(healthy.image) == expected


async def test_refresh_never_stops_turn_admitted_during_signed_verification(healthy, mock_db, monkeypatch):
    """The original race now refuses at every entry while real turns keep running.

    Signed validation is driven independently: the deferred prepare operation
    must no longer reach it, or any lifecycle operation, at all.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import internal, settings_updates
    from app.api.auth import require_admin
    from app.core import application_updates, session_runtime, startup
    from app.db import runs, turns
    from app.serve import IngressApplication
    from tests.core.test_session_runtime import _runtime_env

    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "test-child-secret")
    monkeypatch.setattr(internal, "_shutdown_task", None)
    monkeypatch.setattr(startup, "is_shutting_down", False)
    monkeypatch.setattr(application_updates, "allowed", lambda: True)
    monkeypatch.setattr(application_updates, "control_root", lambda: healthy.root)
    monkeypatch.setattr(settings_updates.preview_access, "grant", lambda _: None)
    app = FastAPI()
    app.include_router(internal.router)
    app.include_router(settings_updates.updates_router, prefix="/settings")
    app.dependency_overrides[require_admin] = lambda: "owner"
    loop = asyncio.get_running_loop()
    verifying, finish = asyncio.Event(), asyncio.Event()
    began = asyncio.Queue()
    verified = threading.Event()
    signed_target = module.handover_target

    def verify(*args):
        loop.call_soon_threadsafe(verifying.set)
        assert verified.wait(5), "test did not admit work during verification"
        return signed_target(*args)

    async def pump(**kwargs):
        began.put_nowait(True)
        await finish.wait()
        yield {"type": "done"}

    monkeypatch.setattr(module, "handover_target", verify)
    existing = turns.enqueue_turn("existing-work", "user", prompt="keep working")
    arriving = turns.enqueue_turn("verification-race", "user", prompt="new work")
    async with AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)),
                           base_url="http://localhost:8000") as client:
        async def request(action):
            response = await client.request("GET" if action == "active-sessions" else "POST",
                                            f"/api/v1/internal/desktop/{action}",
                                            headers={"Authorization": "Bearer test-child-secret"})
            response.raise_for_status()
            return response.json()

        healthy.lifecycle.side_effect = lambda action: asyncio.run_coroutine_threadsafe(request(action), loop).result(5)
        with _runtime_env(pump=pump) as env:
            env["registry"].active_session_ids.return_value = []
            work = [asyncio.create_task(session_runtime.execute(existing))]
            verification = None
            try:
                await asyncio.wait_for(began.get(), 5)
                verification = asyncio.create_task(asyncio.to_thread(
                    module.handover_target, healthy.root, healthy.image, healthy.public_key))
                await asyncio.wait_for(verifying.wait(), 5)
                work.append(asyncio.create_task(session_runtime.execute(arriving)))
                await asyncio.wait_for(began.get(), 5)
                verified.set()
                assert (await asyncio.wait_for(verification, 5))[0] == B
                with pytest.raises(runtime.RestartRejected):
                    await asyncio.to_thread(runtime.prepare_updater_refresh, healthy)
                response = await client.post("/settings/application-updates",
                                             json={"action": "refresh-updater", "scope": "current"})
                assert response.status_code == 409
                assert not (healthy.root / "request.json").exists()
                for task, turn in zip(work, (existing, arriving)):
                    assert not task.done()
                    assert turns.get_turn(turn["id"])["state"] == "running"
                    assert runs.get_active_run(turn["session_id"]) is not None
                assert not startup.is_shutting_down
                healthy.lifecycle.assert_not_called()
            finally:
                verified.set()
                finish.set()
                await asyncio.gather(*work, *([verification] if verification else []), return_exceptions=True)
                startup.is_shutting_down = False
            # Refusal leaves admission open too, not merely existing work alive.
            successor = turns.enqueue_turn("after-refusal", "user", prompt="still serving")
            await session_runtime.execute(successor)
            assert turns.get_turn(successor["id"])["state"] == "completed"


def test_startup_handover_chains_only_to_reverified_target(healthy, monkeypatch, tmp_path):
    image = tmp_path / "image"
    image.mkdir()
    (image / "image.json").write_text(json.dumps(healthy.image))
    (image / "application-update-key.pem").write_bytes(healthy.public_key)
    monkeypatch.setattr(runtime, "image_dir", lambda: image)
    monkeypatch.setattr(runtime, "safe_path", lambda _: healthy.root)
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", A)
    runtime.STOP.clear()
    settings = SimpleNamespace(effective_data_dir=healthy.root.parent)
    execute = Mock()
    assert runtime.handover(settings, execute) is False
    execute.assert_not_called()
    # Only a new container process, not an in-app refresh, can hand over again.
    monkeypatch.delenv("SHUFFLE_HANDED_OVER")
    assert runtime.handover(settings, execute) is True
    executable, args, env = execute.call_args.args
    assert args == [executable, str(healthy.directory(B) / "source/docker/container_runtime.py"), "run"]
    assert env["SHUFFLE_HANDED_OVER"] == B and env["SHUFFLE_IMAGE_DIR"] == str(image)
    assert json.loads((healthy.root / "handover.json").read_text()) == {"revision": B}
    # An exec which never reaches health cannot get a second trial.
    assert runtime.handover(settings, Mock()) is False


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("publish_fails", [False, True])
def test_watch_refuses_stale_refresh_intent_without_restarting(healthy, monkeypatch, pending, publish_fails):
    counter = itertools.count(step=5)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(counter))
    prepare, restart, relaunch, stop, shutdown = (Mock() for _ in range(5))
    monkeypatch.setattr(runtime, "prepare_updater_refresh", prepare)
    monkeypatch.setattr(runtime, "stop_group", stop)
    monkeypatch.setattr(runtime, "shutdown", shutdown)
    runtime.STOP.clear()
    healthy.refresh_requested.set()
    if pending:
        healthy.state["pending"] = B
    original_publish = healthy.publish

    def refuse(phase, **kwargs):
        runtime.STOP.set()  # bound the test watch, not an application stop
        if publish_fails:
            raise OSError("synthetic status write failure")
        return original_publish(phase, **kwargs)

    monkeypatch.setattr(healthy, "publish", Mock(side_effect=refuse))
    app = SimpleNamespace(poll=lambda: None)
    monitor = SimpleNamespace(ever_ready=True, failures=0, live_failures=0, check=lambda: False)
    try:
        assert runtime.watch(app, SimpleNamespace(process=None), None, monitor=monitor, updates=healthy,
                             restart=restart, relaunch=relaunch) == 0
        assert not healthy.refresh_requested.is_set()
        healthy.publish.assert_called_once_with("ready" if pending else "idle", error=runtime.UPDATER_REFRESH_UNAVAILABLE)
        for operation in (prepare, restart, relaunch, stop, shutdown, healthy.lifecycle):
            operation.assert_not_called()
        assert healthy.state == {"active": B, **({"pending": B} if pending else {})}
    finally:
        runtime.STOP.clear()


def test_real_subprocess_app_restart_keeps_supervisor_and_startup_handover_releases_lock(tmp_path):
    project = Path(__file__).resolve().parents[1]
    target = tmp_path / "target.py"
    target.write_text("""import fcntl,json,os,pathlib,sys
root=pathlib.Path(sys.argv[0]).parent
with (root/'lock').open('a') as lock:
 fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert json.loads((root/'events.json').read_text())['closed'] is True
 print(json.dumps({'pid':os.getpid(),'revision':os.environ['SHUFFLE_HANDED_OVER'],'lock':'released'}))
""")
    script = r"""
import contextlib,fcntl,json,os,pathlib,sys,types
from docker import container_runtime as rt
root=pathlib.Path(sys.argv[1]); initial=os.getpid(); events={'closed':False}
image={'revision':'a'*40,'sequence':1,'provides':['source-supervisor']}
(root/'image.json').write_text(json.dumps(image)); (root/'application-update-key.pem').write_bytes(b'fixture')
lock=(root/'lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
rt.isolation=lambda:types.SimpleNamespace(ACTIVE=None,agent_files=contextlib.nullcontext)
rt.image_dir=lambda:root
rt.safe_path=lambda p:root
rt.STOP.clear()
class DB:
 process=None
 def __init__(self,settings): pass
 def open(self,**kw): pass
 def publish_descriptor(self): pass
 def close(self):
  events['closed']=True; (root/'events.json').write_text(json.dumps(events)); lock.close()
rt.Database=DB
class Updates:
 def __init__(self,*args,**kw):
  self.root=root; self.state={'active':'b'*40}; self.image=image; self.public_key=b'fixture'
  self.updater_revision='a'*40; self.stop=types.SimpleNamespace(is_set=lambda:False)
 def publish(self,*a,**kw): pass
 def allowed(self): return True
 def lifecycle(self,action): return {'active_sessions':0,'stopped':True}
 def launch(self,env,**kw): return [sys.executable,'-c','import signal; signal.pause()'],None,dict(env,GIT_SHA='b'*40)
 def snapshot_room(self): pass
 def activating(self): pass
 def close(self): events['updates_closed']=True
updates_module=types.SimpleNamespace(ContainerUpdates=Updates,UpdateError=RuntimeError,
 handover_target=lambda *a:('b'*40,pathlib.Path(sys.executable),root/'target.py'))
rt.load_updater=lambda:updates_module
settings=types.SimpleNamespace(effective_data_dir=root,port=12345)
os.environ['START_CHROME']='0'
def watch(app,db,chrome,monitor,updates,restart,relaunch,**kw):
 old=app.pid
 new=restart(app)
 assert new.pid!=old and app.poll() is not None and os.getpid()==initial
 assert updates.updater_revision=='a'*40 and updates.state['active']=='b'*40
 events['app_restart_kept_supervisor']=True
 try: rt.prepare_updater_refresh(updates)
 except rt.RestartRejected: pass
 else: raise AssertionError('live refresh was accepted')
 assert new.poll() is None and not events['closed'] and not events.get('updates_closed')
 rt.STOP.set()  # a separate process stop, not a refresh request
 return 0
rt.watch=watch
assert rt.run(settings,'run')==0
assert events['closed'] and events['updates_closed'] and events['app_restart_kept_supervisor']
rt.STOP.clear()
rt.handover(settings)
raise AssertionError('startup exec did not occur')
"""
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(project) + os.pathsep + str(project / "fluid"),
           "DATA_DIR": str(tmp_path), "AI_SHUFFLE_ENV_FILE": os.devnull, "TESTING": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["revision"] == B and report["lock"] == "released"


# --- #1121: an idle restart holds an admission fence through verification ---

def _idle_restart_harness(healthy, monkeypatch, loop, client, launch):
    from app.api import internal
    from app.core import startup

    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "test-child-secret")
    monkeypatch.setattr(internal, "_shutdown_task", None)
    monkeypatch.setattr(startup, "is_shutting_down", False)
    monkeypatch.setattr(healthy, "launch", launch)
    monkeypatch.setattr(healthy, "snapshot_room", lambda: None)
    monkeypatch.setattr(healthy, "activating", lambda: None)
    monkeypatch.setattr(runtime, "stop_group", lambda *args: None)
    monkeypatch.setattr(runtime, "start_app", lambda *args: "new-app")
    actions = []

    async def request(action):
        actions.append(action.split("?", 1)[0])
        response = await client.request("GET" if action == "active-sessions" else "POST",
                                        f"/api/v1/internal/desktop/{action}",
                                        headers={"Authorization": "Bearer test-child-secret"})
        response.raise_for_status()
        return response.json()

    healthy.lifecycle.side_effect = lambda action: asyncio.run_coroutine_threadsafe(request(action), loop).result(10)
    return actions


def _internal_client():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import internal
    from app.serve import IngressApplication

    app = FastAPI()
    app.include_router(internal.router)
    return AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)), base_url="http://localhost:8000")


async def _wait_state(turns, turn_id, state):
    for _ in range(200):
        if turns.get_turn(turn_id)["state"] == state:
            return
        await asyncio.sleep(0.02)
    assert turns.get_turn(turn_id)["state"] == state


@pytest.mark.parametrize("outcome", ["preflight-fails", "commits"])
async def test_idle_restart_never_stops_a_turn_admitted_during_verification(healthy, mock_db, monkeypatch, outcome):
    """A turn that arrives while the supervisor verifies stays queued (never
    stopped). A failed preflight reopens admission and the queued turn runs."""
    from app.core import restart_fence, session_runtime, startup, turn_scheduler
    from app.db import turns
    from tests.core.test_session_runtime import _runtime_env

    loop = asyncio.get_running_loop()
    verifying, admitted = asyncio.Event(), threading.Event()
    began, finish = asyncio.Queue(), asyncio.Event()

    def launch(env, activate=False):
        loop.call_soon_threadsafe(verifying.set)
        assert admitted.wait(10), "test did not admit work during verification"
        if outcome == "preflight-fails":
            raise module.UpdateError("synthetic verification failure")
        return ["true"], None, env

    async def pump(**kwargs):
        began.put_nowait(True)
        await finish.wait()
        yield {"type": "done"}

    arriving = turns.enqueue_turn("verification-race", "user", prompt="new work")
    async with _internal_client() as client:
        actions = _idle_restart_harness(healthy, monkeypatch, loop, client, launch)
        with _runtime_env(pump=pump) as env:
            env["registry"].active_session_ids.return_value = []
            env["registry"].get_active.return_value = []
            restart = asyncio.create_task(asyncio.to_thread(runtime.restart_into_update, "old-app", healthy, {}))
            try:
                await asyncio.wait_for(verifying.wait(), 10)
                assert restart_fence.is_closed()
                # Both real turn producers hold the turn back without starting it.
                assert await session_runtime.execute(arriving) is None
                turn_scheduler.kick("verification-race")
                await asyncio.sleep(0.05)
                assert began.empty()
                assert turns.get_turn(arriving["id"])["state"] == "queued"
                assert not session_runtime.active_executions()
            finally:
                admitted.set()
            if outcome == "preflight-fails":
                with pytest.raises(runtime.RestartRejected):
                    await asyncio.wait_for(restart, 10)
                assert actions == ["restart-reserve", "restart-release"]
                assert not restart_fence.is_closed() and not startup.is_shutting_down
                # Release re-kicked the held session: the turn runs now.
                await asyncio.wait_for(began.get(), 10)
                assert turns.get_turn(arriving["id"])["state"] == "running"
                finish.set()
                await _wait_state(turns, arriving["id"], "completed")
            else:
                assert await asyncio.wait_for(restart, 10) == "new-app"
                assert actions == ["restart-reserve", "restart-reserve", "stop-sessions"]
                # Nothing was running to stop; the turn is durable for the next boot.
                assert began.empty()
                assert turns.get_turn(arriving["id"])["state"] == "queued"
                finish.set()


async def test_idle_restart_refuses_a_turn_registered_at_the_fence_without_stopping_it(healthy, mock_db, monkeypatch):
    from app.core import restart_fence, session_runtime, startup
    from app.db import turns
    from tests.core.test_session_runtime import _runtime_env

    loop = asyncio.get_running_loop()
    began, finish = asyncio.Queue(), asyncio.Event()
    launch = Mock(side_effect=AssertionError("busy restart must not verify"))

    async def pump(**kwargs):
        began.put_nowait(True)
        await finish.wait()
        yield {"type": "done"}

    existing = turns.enqueue_turn("at-the-fence", "user", prompt="keep working")
    assert existing is not None
    async with _internal_client() as client:
        actions = _idle_restart_harness(healthy, monkeypatch, loop, client, launch)
        with _runtime_env(pump=pump) as env:
            env["registry"].active_session_ids.return_value = []
            env["registry"].get_active.return_value = []
            # Reservation first: the turn arriving in the next loop step is
            # held back as a queued row, then started by the release.
            assert (await restart_fence.reserve("c" * 32))["reserved"] is True
            assert await session_runtime.execute(existing) is None
            assert turns.get_turn(existing["id"])["state"] == "queued"
            restart_fence.release("c" * 32)
            await asyncio.wait_for(began.get(), 10)
            # Turn first: registered in the same step as its check, so the
            # supervisor's reservation sees it and refuses without stopping it.
            assert (await restart_fence.reserve("d" * 32))["reserved"] is False
            with pytest.raises(runtime.RestartRejected):
                await asyncio.to_thread(runtime.restart_into_update, "old-app", healthy, {})
            launch.assert_not_called()
            assert actions == ["restart-reserve"]
            assert not restart_fence.is_closed() and not startup.is_shutting_down
            assert turns.get_turn(existing["id"])["state"] == "running"
            finish.set()
            await _wait_state(turns, existing["id"], "completed")


async def test_lost_reserve_answer_is_released_and_admission_reopens(healthy, mock_db, monkeypatch):
    """The reservation landed but its answer was lost: the supervisor releases
    it by the token it chose, so the worker is not left fenced."""
    import httpx
    from app.core import restart_fence

    loop = asyncio.get_running_loop()
    async with _internal_client() as client:
        actions = _idle_restart_harness(healthy, monkeypatch, loop, client, Mock())
        routed = healthy.lifecycle.side_effect

        def lossy(action):
            answer = routed(action)
            if action.startswith("restart-reserve"):
                assert restart_fence.is_closed()
                raise httpx.ReadTimeout("synthetic lost answer")
            return answer

        healthy.lifecycle.side_effect = lossy
        with pytest.raises(runtime.RestartRejected):
            await asyncio.to_thread(runtime.restart_into_update, "old-app", healthy, {})
    assert actions == ["restart-reserve", "restart-release"]
    assert not restart_fence.is_closed()
