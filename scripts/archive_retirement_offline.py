"""Live offline-owner settlement and stale signed delivery, on private peers."""
import contextlib,json,uuid
from pathlib import Path
from verify_archive_retirement import Worker,wait,paired


def command(worker,name,**kwargs):
    token=uuid.uuid4().hex
    path=worker.directory/(token+'.command')
    path.write_text(json.dumps({'name':name,**kwargs}))
    result=worker.directory/(token+'.result')
    wait(worker.name+' '+name,lambda:result.exists())
    value=json.loads(result.read_text())
    assert 'error' not in value,value
    return value


def run(args):
    directory=Path(args.scratch)/('offline-'+uuid.uuid4().hex[:6]);directory.mkdir(mode=0o700)
    with contextlib.ExitStack() as stack:
        a=Worker('A',directory/'A',Path(args.candidate),stack);b=Worker('B',directory/'B',Path(args.candidate),stack)
        for w in (a,b):
            wrapper=w.directory/'live-wrapper.py';wrapper.write_text(WRAPPER.replace('__DIRECTORY__',repr(str(w.directory))))
            w.start(args.candidate,wrapper=wrapper)
        paired(a,b)
        sid=str(uuid.uuid4())
        a.run(f"from app.db import database\npid=database.get_all_profiles()[0]['id']\ndatabase.create_session({sid!r},pid,title='Offline settlement race',is_active=True)")
        a.publish();wait('offline fixture mirrored',lambda:b.row(sid))
        command(a,'waits',sid=sid,request='old')
        monitor=command(a,'monitor',sid=sid)
        initial=a.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id=%s AND payload->>'subtype'='session_settled'",(sid,))[0]['n']
        (a.directory/'partition').touch()
        b.request('PATCH',f'/api/v1/sessions/{sid}/active',{'is_active':False})
        assert not b.row(sid)['is_active'] and a.row(sid)['is_active']
        assert len(a.sql("SELECT id FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,)))==2
        envelope=b.run(f"import json\nfrom app.db import network as store\nfrom app.network import engine\nctx=engine.require_context()\nops=[op for op in store.ops_since({{}}) if op['origin_device_id']==ctx.device_id and op['collection']=='session_active' and op['record_id']=={sid!r}]\nprint('RESULT '+json.dumps(ops[-1]))")
        (a.directory/'partition').unlink()
        wait('reconnected owner applies offline settle',lambda:not a.row(sid)['is_active'])
        wait('owner waits dismissed',lambda:not a.sql("SELECT 1 FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,)))
        state=command(a,'status',sid=sid,task_id=monitor['task_id'])
        assert state['private']==0 and state['task_status']=='running'
        once=a.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id=%s AND payload->>'subtype'='session_settled'",(sid,))[0]['n']
        assert once==initial+1
        b.request('PATCH',f'/api/v1/sessions/{sid}/active',{'is_active':True})
        wait('other-client reopen reaches owner',lambda:a.row(sid)['is_active'])
        command(a,'question',sid=sid,request='fresh-after-reopen')
        # Actual signed control sends the old delivery AFTER reactivation and a
        # later prompt. Repeat it to exercise the durable one-time receipt.
        deliveries=b.run(f"""import asyncio,json
from app.network import engine,control
engine.require_context()
async def replay():
    return [await control.call({a.device!r},'session-settle-dismiss',{{'session_id':{sid!r},'is_active':False,'settled_membership':{envelope!r}}}) for _ in range(3)]
print('RESULT '+json.dumps(asyncio.run(replay())))
""")
        assert all(not r['dismissed'] for r in deliveries)
        assert a.sql("SELECT id FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,))
        assert a.row(sid)['is_active']
        # New real settle then a new prompt on the already-settled row. A
        # duplicate of the just-processed receipt must not sweep that prompt.
        b.request('PATCH',f'/api/v1/sessions/{sid}/active',{'is_active':False})
        wait('new settlement dismisses only fresh question',lambda:not a.sql("SELECT 1 FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,)))
        command(a,'question',sid=sid,request='fresh-after-settled-receipt')
        latest=b.run(f"import json\nfrom app.network import engine\nfrom app.db import network as store\nctx=engine.require_context()\nprint('RESULT '+json.dumps([op for op in store.ops_since({{}}) if op['origin_device_id']==ctx.device_id and op['collection']=='session_active' and op['record_id']=={sid!r}][-1]))")
        duplicate=b.run(f"""import asyncio,json
from app.network import engine,control
engine.require_context()
print('RESULT '+json.dumps(asyncio.run(control.call({a.device!r},'session-settle-dismiss',{{'session_id':{sid!r},'is_active':False,'settled_membership':{latest!r}}}))))
""")
        assert not duplicate['dismissed']
        assert a.sql("SELECT id FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,))
        command(a,'finish-monitor',sid=sid,task_id=monitor['task_id'])
        wait('actual settled Monitor terminal notification persisted',lambda:a.sql("SELECT 1 FROM background_tasks WHERE session_id=%s AND task_id=%s AND status='completed'",(sid,monitor['task_id'])))
        assert a.row(sid)['owner_device_id']==a.device and b.row(sid)['owner_device_id']==a.device
        # A separate truly idle row avoids stealing the Monitor's newly queued
        # completion report. Move it through the real leased handoff, recreate
        # membership, and replay a former-owner dismissal against a new question.
        sid=str(uuid.uuid4())
        a.run(f"from app.db import database\npid=database.get_all_profiles()[0]['id']\ndatabase.create_session({sid!r},pid,title='Idle ownership transfer',is_active=True)")
        a.publish();wait('idle transfer fixture mirrored',lambda:b.row(sid))
        a.request('POST',f'/api/v1/sessions/{sid}/archive')
        wait('idle transfer fixture settled',lambda:not b.row(sid)['is_active'])
        prior=a.run(f"import json\nfrom app.db import network as store\nprint('RESULT '+json.dumps([op for op in store.ops_since({{}}) if op['collection']=='session_active' and op['record_id']=={sid!r}][-1]))")
        moved=b.request('POST',f'/api/v1/sessions/{sid}/adopt',{})
        assert moved['adopted']
        wait('intentional leased ownership transfer',lambda:all(w.row(sid)['owner_device_id']==b.device for w in (a,b)))
        b.sql('DELETE FROM network_session_active WHERE session_id=%s',(sid,))
        b.request('PATCH',f'/api/v1/sessions/{sid}/active',{'is_active':True})
        wait('new owner recreated membership epoch',lambda:all(w.row(sid)['is_active'] for w in (a,b)))
        command(b,'question',sid=sid,request='fresh-after-ownership-transfer')
        after_transfer=a.run(f"""import asyncio,json
from app.network import engine,control
engine.require_context()
print('RESULT '+json.dumps(asyncio.run(control.call({b.device!r},'session-settle-dismiss',{{'session_id':{sid!r},'is_active':False,'settled_membership':{prior!r}}}))))
""")
        assert not after_transfer['dismissed']
        assert b.sql("SELECT 1 FROM pending_interactions WHERE session_id=%s AND state='pending'",(sid,))
        result={'result':'network-partitioned execution owner; offline peer settlement, exact one receipt, owner wait dismissal, real running Monitor survival, other-client reactivation, fresh prompt, three delayed signed deliveries and duplicate current delivery; intentional leased ownership transfer and recreated membership epoch passed',
            'delayed_deliveries':deliveries,'duplicate':duplicate,'monitor':monitor,'owner_unchanged_before_intentional_handoff':True,'intentional_transfer':moved,'post_transfer_stale_dismissal':after_transfer,
            'offline_model':'Both apps stayed alive; the owner peer transport/ASGI edge was partitioned. Its OS job did not need resurrection across process death.'}
        Path(args.scratch,'offline-results.json').write_text(json.dumps(result,indent=2))
    print('PASS offline owner and stale signed delivery; both apps and PostgreSQL stopped',flush=True)


WRAPPER=r"""import asyncio,json,runpy,shlex,sys
from pathlib import Path
from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from app.network import transport
from app.db import session_archive_migration as migration
D=Path(__DIRECTORY__)
request=transport.PeerLink._request
async def partitioned_request(self,*args,**kwargs):
    if (D/'partition').exists():raise httpx.ConnectError('disposable peer partition')
    return await request(self,*args,**kwargs)
transport.PeerLink._request=partitioned_request
asgi=FastAPI.__call__
async def partitioned_asgi(self,scope,receive,send):
    if (D/'partition').exists() and scope.get('path','').startswith('/api/v1/network/peer/'):
        await send({'type':'http.response.start','status':503,'headers':[]})
        await send({'type':'http.response.body','body':b'Disposable offline peer'})
        return
    await asgi(self,scope,receive,send)
FastAPI.__call__=partitioned_asgi
async def broadcast(*args,**kwargs):
    from app.core.sync_engine import sync_engine
    sid=kwargs.pop('session_id',None)
    # Question producer supplies the session as its first positional argument.
    if sid is None and args:sid=args[0]
    if sid:await sync_engine._broadcast_user_message(sid,args[-1] if args else kwargs)
async def controller():
    from app.core.user_question_handler import user_question_handler
    from app.core.permission_handler import permission_handler
    from app.core import chat_access
    from app.core.monitor_spawn import make_fluid_monitor_spawner
    from app.core.task_registry import task_registry
    from app.db import interactions
    async def question(sid,request):
        async def emit(message):
            from app.core.sync_engine import sync_engine
            await sync_engine._broadcast_user_message(sid,message)
        return await user_question_handler.post_question(request_id=request,tool_use_id=request+'-tool',session_id=sid,questions=[{'question':'Fresh disposable question?','header':'Confirm'}],broadcast_func=emit)
    while True:
        for p in D.glob('*.command'):
            r=p.with_suffix('.result')
            if r.exists():continue
            try:
                c=json.loads(p.read_text());sid=c['sid'];name=c['name']
                value={'ok':True}
                if name in ('waits','question'):
                    await question(sid,c['request'])
                    if name=='waits':
                        interactions.create_interaction(sid,'permission',{'request_id':'offline-permission','tool_name':'Bash','tool_input':{'command':'true'}},None)
                        await chat_access.broker.request(sid,'service','Disposable offline settlement')
                elif name=='monitor':
                    release=D/'release-monitor'
                    code="import pathlib,time; p=pathlib.Path("+repr(str(release))+"); end=time.monotonic()+600\nwhile not p.exists() and time.monotonic()<end: time.sleep(.05)\nassert p.exists(); print('settled Monitor completed normally')"
                    outcome=await make_fluid_monitor_spawner(sid)({'command':shlex.quote(sys.executable)+' -c '+shlex.quote(code)},SimpleNamespace(cwd=str(D),extra={}))
                    assert not outcome.is_error,outcome.text
                    value=dict(outcome.ui or {})
                elif name=='status':
                    task=task_registry.get_task(sid,c['task_id'])
                    value={'private':chat_access.broker.pending_count(sid),'task_status':task['status']}
                elif name=='finish-monitor':(D/'release-monitor').touch()
                else:raise AssertionError('unknown acceptance command')
                r.write_text(json.dumps(value,default=str))
            except Exception as exc:r.write_text(json.dumps({'error':type(exc).__name__+': '+str(exc)}))
        await asyncio.sleep(.1)
original=migration.reconcile
async def reconcile():
    await original()
    asyncio.create_task(controller())
migration.reconcile=reconcile
runpy.run_module('app.main',run_name='__main__')
"""
