"""Refresh keeps the supervisor boundary; subprocess checks are controlled, not a container update."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
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
    current.refresh_mode = "available"
    current.lifecycle = Mock(side_effect=lambda action: {"active_sessions": 0} if action == "active-sessions" else {"stopped": True})
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    monkeypatch.setattr(runtime, "load_updater", lambda: module)
    monkeypatch.delenv("SHUFFLE_SOURCE_SUPERVISOR", raising=False)
    return current


def test_refresh_rechecks_signed_healthy_source_before_quiescing(healthy):
    assert runtime.prepare_updater_refresh(healthy) == B
    assert [call.args[0] for call in healthy.lifecycle.call_args_list] == ["active-sessions", "stop-sessions"]
    assert healthy.state == {"active": B}, "refresh does not activate staging or migrate the database"


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
    assert all(call.args[0] != "stop-sessions" for call in healthy.lifecycle.call_args_list)


def test_policy_change_during_verification_is_refused(healthy):
    healthy.allowed = Mock(side_effect=[True, False])
    with pytest.raises(runtime.RestartRejected):
        runtime.prepare_updater_refresh(healthy)
    assert healthy.lifecycle.call_args_list == [(('active-sessions',),)]


def test_explicit_handover_chains_only_to_reverified_target(healthy, monkeypatch, tmp_path):
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
    assert runtime.handover(settings, execute, refresh_revision=A) is False
    execute.assert_not_called()
    assert runtime.handover(settings, execute, refresh_revision=B) is True
    executable, args, env = execute.call_args.args
    assert args == [executable, str(healthy.directory(B) / "source/docker/container_runtime.py"), "run"]
    assert env["SHUFFLE_HANDED_OVER"] == B and env["SHUFFLE_IMAGE_DIR"] == str(image)
    assert json.loads((healthy.root / "handover.json").read_text()) == {"revision": B}
    # An exec which never reaches health cannot get a second trial.
    assert runtime.handover(settings, Mock(), refresh_revision=B) is False


def test_watch_handoff_is_an_explicit_supervisor_event(healthy, monkeypatch):
    monkeypatch.setattr(runtime, "prepare_updater_refresh", lambda _: B)
    counter = itertools.count(step=5)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(counter))
    runtime.STOP.clear()
    healthy.refresh_requested.set()
    app = SimpleNamespace(poll=lambda: None)
    monitor = SimpleNamespace(ever_ready=True, failures=0, live_failures=0, check=lambda: False)
    with pytest.raises(runtime.SupervisorRefresh) as caught:
        runtime.watch(app, SimpleNamespace(process=None), None, monitor=monitor, updates=healthy)
    assert caught.value.revision == B
    assert not healthy.refresh_requested.is_set()


def test_real_subprocess_app_restart_keeps_resident_supervisor_and_refresh_releases_lock(tmp_path):
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
  self.updater_revision='a'*40; self.refresh_mode='available'; self.stop=types.SimpleNamespace(is_set=lambda:False)
 def publish(self,*a,**kw): pass
 def allowed(self): return True
 def updater_status(self): return {'refresh':'available'}
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
 raise rt.SupervisorRefresh(rt.prepare_updater_refresh(updates))
rt.watch=watch
try: rt.run(settings,'run')
except rt.SupervisorRefresh as refresh:
 assert events['closed'] and events['updates_closed'] and events['app_restart_kept_supervisor']
 rt.handover(settings,refresh_revision=refresh.revision)
raise AssertionError('exec did not occur')
"""
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(project) + os.pathsep + str(project / "fluid"),
           "DATA_DIR": str(tmp_path), "AI_SHUFFLE_ENV_FILE": os.devnull, "TESTING": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["revision"] == B and report["lock"] == "released"
