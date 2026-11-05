"""Disposable full-boot fault/visibility matrix for archive retirement.

Uses the retained-data runner; no installed worker or credential is accessed.
"""
import contextlib,json,os,signal,sys,uuid
from pathlib import Path
from verify_archive_retirement import Worker,wait


def run(args):
    directory=Path(args.scratch)/('boot-matrix-'+uuid.uuid4().hex[:6]);directory.mkdir(mode=0o700)
    with contextlib.ExitStack() as stack:
        w=Worker('matrix',directory/'worker',Path(args.candidate),stack)
        w.start(args.old)
        bypass=str(uuid.uuid4())
        w.run(f"from app.db import database\npid=database.get_all_profiles()[0]['id']\ndatabase.create_session({bypass!r},pid,title='Matrix generic bypass',is_active=True)")
        w.request('PATCH',f'/api/v1/sessions/{bypass}?status=archived')
        assert w.row(bypass)['status']=='archived'
        w.stop()
        ids={name:str(uuid.uuid4()) for name in ('waits','background','stopped','queued','voice','api','hidden','child','mirror','active','settled','failed','null')}
        ids['bypass']=bypass
        fixture=w.run(f"""import json,uuid
from app.db import database,interactions,turns,session_events
from app.db.base import get_db
pid=database.get_all_profiles()[0]['id']
ids={ids!r}
for name,sid in ids.items():
    if name=='bypass':continue
    database.create_session(sid,pid,title='Matrix '+name,is_active=name!='settled',
        mode='voice' if name=='voice' else None,hidden=name in ('hidden','child'),
        is_worker=name=='child',parent_session_id=ids['hidden'] if name=='child' else None)
interactions.create_interaction(ids['waits'],'permission',{{'request_id':'legacy-permission','tool_name':'Bash','tool_input':{{'command':'true'}}}},None)
interactions.create_interaction(ids['waits'],'question',{{'request_id':'legacy-question','questions':[{{'question':'Retain evidence?','header':'Confirm'}}]}},None)
session_events.append_events(ids['waits'],[{{'type':'message.user','payload':{{'content':'Keep original transcript'}}}},{{'type':'message.assistant','payload':{{'content':'Proceed?'}}}}])
queued=turns.enqueue_turn(ids['queued'],'agent_message',prompt='Never execute old queue',profile_id=pid)
parked=turns.enqueue_turn(ids['waits'],'user',prompt='Old waiting turn',profile_id=pid)
turns.start_turn(parked['id']);turns.park_turn(parked['id'])
historical=turns.enqueue_turn(ids['stopped'],'user',prompt='Historical stop evidence',profile_id=pid)
api_id='matrix-api-user'
with get_db() as conn:
    c=conn.cursor()
    c.execute("INSERT INTO api_users (id,name,api_key_hash) VALUES (%s,%s,%s)",(api_id,'Disposable matrix API user','not-a-real-credential'))
    c.execute("UPDATE sessions SET api_user_id=%s WHERE id=%s",(api_id,ids['api']))
    c.execute("UPDATE turns SET state='aborted',stop_reason='archived' WHERE id=%s",(historical['id'],))
    c.execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s",('disposable-mirror-owner',ids['mirror']))
    for name in ('waits','background','stopped','queued','voice','api','hidden','child','mirror','bypass'):
        c.execute("UPDATE sessions SET status='archived',created_at='2020-01-01',updated_at='2020-01-02',total_cost_usd=1.25,total_tokens_in=42,worktree_branch='retained-evidence' WHERE id=%s",(ids[name],))
    c.execute("UPDATE sessions SET status='failed' WHERE id=%s",(ids['failed'],))
    c.execute("UPDATE sessions SET status=NULL WHERE id=%s",(ids['null'],))
    for i in range(1000):
        c.execute("INSERT INTO sessions (id,profile_id,title,status,is_active,created_at,updated_at,owner_device_id) VALUES (%s,%s,%s,'archived',TRUE,'2020-01-01','2020-01-02',%s)",(str(uuid.uuid4()),pid,'Matrix history %04d'%i,{w.device!r}))
    for wid,sid,source in [('matrix-old-wake',ids['queued'],'schedule_wakeup'),('matrix-held-wake',ids['queued'],'held_peer'),('matrix-notice',ids['queued'],'profile_cancelled'),('matrix-unrelated',ids['active'],'schedule_wakeup')]:
        c.execute("INSERT INTO pending_wakeups (id,session_id,prompt,fire_at,source) VALUES (%s,%s,'retained wake evidence',2000000000,%s)",(wid,sid,source))
    c.execute('UPDATE sessions SET owner_device_id=%s WHERE owner_device_id IS NULL',({w.device!r},))
    c.execute('SELECT COUNT(*) AS n FROM sessions');total=c.fetchone()['n']
    c.execute("SELECT COUNT(*) AS n FROM sessions WHERE status='archived'");archives=c.fetchone()['n']
print('RESULT '+json.dumps({{'total':total,'archives':archives,'queued':queued['id'],'parked':parked['id'],'historical':historical['id']}}))
""")
        before=w.sql('SELECT id,created_at,updated_at,profile_id,api_user_id,hidden,is_worker,parent_session_id,owner_device_id,total_cost_usd,total_tokens_in,worktree_branch FROM sessions ORDER BY id')
        wrapper=directory/'boot-wrapper.py'
        wrapper.write_text(WRAPPER.replace('__DIRECTORY__',repr(str(directory))).replace('__IDS__',repr(ids)))
        # Kill between actual init_database migration and async dismissal.
        (directory/'pause').touch()
        w.start(args.candidate,wrapper=wrapper,wait_ready=False)
        wait('real app reached post-SQL pre-dismissal fault point',lambda:(directory/'before-dismissal').exists())
        assert not w.sql("SELECT 1 FROM sessions WHERE status='archived'")
        assert w.sql('SELECT COUNT(*) AS n FROM session_archive_retirement WHERE NOT done')[0]['n']==fixture['archives']
        assert len(w.sql("SELECT 1 FROM pending_interactions WHERE state='pending'"))==2
        os.killpg(w.process.pid,signal.SIGKILL);w.process.wait(timeout=5);w.process=None
        (directory/'pause').unlink()
        w.start(args.candidate,wrapper=wrapper)
        boot=json.loads((directory/'reconciled.json').read_text())
        assert boot['stop_latched'] and boot['private_before']==1 and boot['private_after']==0
        assert boot['outbox_held'] and boot['monitor_running_after_settlement']
        wait('settled real Monitor completes normally',lambda:(directory/'monitor-completed').exists())
        assert not w.sql("SELECT 1 FROM sessions WHERE status='archived'")
        assert not w.sql('SELECT 1 FROM session_archive_retirement WHERE NOT done')
        assert not w.sql("SELECT 1 FROM pending_interactions WHERE state='pending'")
        assert w.sql('SELECT state,stop_reason FROM turns WHERE id=%s',(fixture['queued'],))[0]=={'state':'aborted','stop_reason':'removed_from_queue'}
        assert w.sql('SELECT state,stop_reason FROM turns WHERE id=%s',(fixture['historical'],))[0]=={'state':'aborted','stop_reason':'archived'}
        assert {r['id'] for r in w.sql('SELECT id FROM pending_wakeups')}=={'matrix-notice','matrix-unrelated'}
        assert w.sql('SELECT COUNT(*) AS n FROM sessions')[0]['n']==fixture['total']
        after=w.sql('SELECT id,created_at,updated_at,profile_id,api_user_id,hidden,is_worker,parent_session_id,owner_device_id,total_cost_usd,total_tokens_in,worktree_branch FROM sessions ORDER BY id')
        # The actual Monitor legitimately updates its own transcript/recency.
        excluded={ids['background'],ids['waits']}
        original={r['id']:r for r in before if r['id'] not in excluded}
        changed={r['id']:{k:(original[r['id']][k],r[k]) for k in r if original[r['id']][k]!=r[k]}
            for r in after if r['id'] in original and original[r['id']]!=r}
        assert not changed,json.dumps(changed,default=str)
        receipt_count=w.sql("SELECT COUNT(*) AS n FROM session_events WHERE payload->>'subtype'='session_settled'")[0]['n']
        revisions=w.sql('SELECT session_id,local_revision,local_revision_epoch FROM network_session_active ORDER BY session_id')
        # The marker's old epoch must not capture a later prompt, even at the
        # same frozen wall time and after the membership row is recreated.
        w.run(f"""import asyncio
from app.db import interactions
from app.db.base import get_db
from app.network import session_active
from app.core.session_settlement import dismiss_owner_waits
with get_db() as conn:
    c=conn.cursor();c.execute('SELECT membership_revision,membership_epoch FROM session_archive_retirement WHERE session_id=%s',({ids['waits']!r},));old=dict(c.fetchone())
    c.execute('DELETE FROM network_session_active WHERE session_id=%s',({ids['waits']!r},))
session_active.write_shared({ids['waits']!r},False)
fresh=interactions.create_interaction({ids['waits']!r},'question',{{'request_id':'fresh-after-epoch'}},None)
assert not asyncio.run(dismiss_owner_waits({ids['waits']!r},expected_revision=old))
assert interactions.get_interaction(fresh['id'])['state']=='pending'
interactions.cancel_interaction(fresh['id'])
""")
        # Second real boot, same data and frozen clock, no repeated migration.
        w.start(args.candidate,wrapper=wrapper)
        assert w.sql('SELECT COUNT(*) AS n FROM sessions')[0]['n']==fixture['total']
        assert w.sql("SELECT COUNT(*) AS n FROM session_events WHERE payload->>'subtype'='session_settled'")[0]['n']==receipt_count
        stable=w.sql('SELECT session_id,local_revision,local_revision_epoch FROM network_session_active ORDER BY session_id')
        assert [r for r in revisions if r['session_id']!=ids['waits']]==[r for r in stable if r['session_id']!=ids['waits']]
        result={'fixture':fixture,'ids':ids,'boot':boot,'retained_rows':fixture['total'],'deleted_sessions':0,
            'result':'real init_database + real lifespan; crash before async dismissal, two candidate boots, outbox held, frozen membership clock, recreated epoch and later prompt passed',
            'cohorts':w.sql("SELECT hidden,is_worker,mode,api_user_id,status,is_active,COUNT(*) AS n FROM sessions GROUP BY hidden,is_worker,mode,api_user_id,status,is_active ORDER BY COUNT(*) DESC"),
            'caveat':'A process restart cannot retain a live OS task or in-memory private wait. The fixture creates a real private request and Monitor at the pre-reconcile hook; their survival tests the settlement boundary, not cross-process resurrection.'}
        Path(args.scratch,'boot-matrix-results.json').write_text(json.dumps(result,indent=2,default=str))
    print('PASS full boot matrix; app and PostgreSQL stopped',flush=True)


WRAPPER=r"""import asyncio,json,runpy,sys
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from app.db import session_archive_migration as migration
from app.network import engine,session_active
D=Path(__DIRECTORY__);IDS=__IDS__
class Frozen(datetime):
    @classmethod
    def now(cls,tz=None):return datetime(2026,11,5,tzinfo=timezone.utc)
session_active.datetime=Frozen
held=engine.hold_outbox
def hold(value):held(value or not (D/'release-outbox').exists())
engine.hold_outbox=hold
hold(True)
original=migration.reconcile
async def reconcile():
    (D/'before-dismissal').touch()
    while (D/'pause').exists():await asyncio.sleep(.05)
    if (D/'reconciled.json').exists():
        await original();return
    from app.core import chat_access
    from app.core.task_registry import task_registry
    from app.core.monitor_spawn import make_fluid_monitor_spawner
    task_registry.mark_user_stopped(IDS['stopped'])
    await chat_access.broker.request(IDS['waits'],'service','Disposable migration acceptance')
    private_before=chat_access.broker.pending_count(IDS['waits'])
    completion=D/'monitor-completed';release=D/'release-monitor'
    code="import pathlib,time; p=pathlib.Path("+repr(str(release))+"); end=time.monotonic()+90\nwhile not p.exists() and time.monotonic()<end: time.sleep(.05)\nassert p.exists(); pathlib.Path("+repr(str(completion))+").touch(); print('retained settled Monitor completed')"
    import shlex
    outcome=await make_fluid_monitor_spawner(IDS['background'])({'command':shlex.quote(sys.executable)+' -c '+shlex.quote(code)},SimpleNamespace(cwd=str(D),extra={}))
    assert not outcome.is_error,outcome.text
    await original()
    from app.db import database
    from app.core.session_reservations import holds_resources
    assert holds_resources(IDS['background'])
    result={'private_before':private_before,'private_after':chat_access.broker.pending_count(IDS['waits']),
        'stop_latched':task_registry.is_user_stopped(IDS['stopped']),'outbox_held':engine.outbox_held(),
        'monitor_running_after_settlement':True}
    (D/'reconciled.json').write_text(json.dumps(result));release.touch();(D/'release-outbox').touch();held(False)
migration.reconcile=reconcile
runpy.run_module('app.main',run_name='__main__')
"""
