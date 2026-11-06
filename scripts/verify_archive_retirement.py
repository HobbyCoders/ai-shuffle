"""Real retained-data upgrade acceptance; every database belongs to --scratch."""
import argparse, asyncio, contextlib, http.client, json, os, signal, subprocess, sys, time, uuid
from pathlib import Path
from types import SimpleNamespace


def wait(label, fn, timeout=120):
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        try:
            value=fn()
            if value:
                print('PASS '+label, flush=True)
                return value
        except (ConnectionError, OSError, RuntimeError) as exc:
            last=type(exc).__name__
        time.sleep(.5)
    raise AssertionError(f'{label} timed out ({last})')

class Worker:
    def __init__(self, name, directory, controller, stack):
        from app.preview_run import preview_environment, free_port
        from docker import container_runtime as runtime
        import psycopg2, psycopg2.extras
        self.name,self.directory,self.controller=name,directory,controller
        directory.mkdir(mode=0o700)
        self.env=preview_environment(directory,free_port(),free_port(),host='127.0.0.1',browser_host='127.0.0.1',ttl_seconds=2400,
            extra={'SHUFFLE_DEV_UI':'vite','SHUFFLE_DEV_ACCESS_FILE':str(directory/'access.json')})
        self.env['SHUFFLE_DESKTOP_LISTENER_TOKEN']=uuid.uuid4().hex+uuid.uuid4().hex
        self.env['CLUSTER_ADVERTISE_URL']='http://127.0.0.1:'+self.env['SHUFFLE_PROTECTED_PORT']
        class ThrowawayDatabase(runtime.Database):
            def command(self,args,**kwargs):
                if Path(args[0]).name=='initdb' and '-D' in args:args=[*args,'--no-sync']
                return super().command(args,**kwargs)
        self.runtime=ThrowawayDatabase(SimpleNamespace(effective_data_dir=Path(self.env['DATA_DIR'])),port=free_port(),disposable=True)
        stack.callback(self.runtime.close)
        self.runtime.open()
        self.runtime.admin_script('ALTER SYSTEM SET fsync = off; SELECT pg_reload_conf();')
        self.runtime.publish_descriptor()
        descriptor=json.loads((Path(self.env['DATA_DIR'])/'managed-postgres.json').read_text())
        self.db=psycopg2.connect(host=descriptor['host'],port=descriptor['port'],user=descriptor['user'],password=descriptor['password'],dbname=descriptor['database'],cursor_factory=psycopg2.extras.RealDictCursor)
        self.db.autocommit=True
        stack.callback(self.db.close)
        self.process=None
        self.starts=0
        stack.callback(self.stop)

    def request(self, method, path, body=None):
        c=http.client.HTTPConnection('127.0.0.1',int(self.env['PORT']),timeout=15)
        headers={'X-Shuffle-Desktop-Listener':self.env['SHUFFLE_DESKTOP_LISTENER_TOKEN']}
        if body is not None: headers['Content-Type']='application/json'
        try:
            c.request(method,path,body=None if body is None else json.dumps(body),headers=headers)
            r=c.getresponse();raw=r.read()
            if r.status>=400: raise RuntimeError(f'{self.name} {method} {path} HTTP {r.status}: '+raw.decode()[:250])
            return json.loads(raw) if raw else {}
        finally:c.close()

    def sql(self, sql, params=()):
        with self.db.cursor() as c:
            c.execute(sql,params)
            return [dict(x) for x in c.fetchall()] if c.description else []

    def start(self, checkout, *, wrapper=None, wait_ready=True):
        self.stop()
        self.checkout=Path(checkout)
        assert (self.checkout/'.git').exists() or (self.checkout/'.source-commit').is_file(), 'host-provisioned source required'
        env={**self.env,'PYTHONPATH':str(self.checkout)+os.pathsep+str(self.checkout/'fluid')}
        self.starts+=1
        self.log=self.directory/f'app-{self.starts}.log'
        with self.log.open('w') as log:
            self.process=subprocess.Popen([sys.executable,str(wrapper)] if wrapper else [sys.executable,'-m','app.main'],cwd=self.checkout,env=env,stdout=log,stderr=log,start_new_session=True)
        if not wait_ready:
            return
        def ready():
            if self.process.poll() is not None: raise AssertionError(f'{self.name} boot exited {self.process.returncode}; inspect {self.log}')
            try:return self.request('GET','/api/v1/network/status')
            except (OSError,RuntimeError):return None
        status=wait(self.name+' boot '+str(self.checkout),ready)
        self.device=status['device']['device_id']
        return status

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGTERM)
            try:self.process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=5)
        self.process=None

    def run(self, code, *, timeout=100):
        env={**self.env,'PYTHONPATH':str(self.checkout)+os.pathsep+str(self.checkout/'fluid')}
        prefix='from app.db.base import init_pool\ninit_pool()\n'
        p=subprocess.run([sys.executable,'-c',prefix+code],cwd=self.checkout,env=env,capture_output=True,text=True,timeout=timeout)
        if p.returncode:
            # Keep private runtime logs off the chat/tool output.
            file=self.directory/f'probe-failure-{uuid.uuid4().hex[:6]}.log'
            file.write_text(p.stdout+'\n'+p.stderr)
            raise AssertionError(f'{self.name} domain probe failed; inspect {file}')
        lines=[line for line in p.stdout.splitlines() if line.startswith('RESULT ')]
        return json.loads(lines[-1][7:]) if lines else None

    def snapshot(self, peer):
        wait(peer.name+' transcript backfill before serving full snapshot',lambda:not peer.sql(
            "SELECT 1 FROM network_transcript_backfill WHERE NOT done LIMIT 1"))
        return self.run(f"""import asyncio,json,httpx,time
from app.network import engine,transport,peer_snapshot
ctx=engine.require_context()
link=transport.PeerLink({peer.device!r},[{peer.env['CLUSTER_ADVERTISE_URL']!r}])
async def copy():
    async with httpx.AsyncClient(timeout=90) as client:
        deadline=time.monotonic()+75
        while time.monotonic()<deadline:
            manifest=await link.call(client,ctx,'POST','/api/v1/network/peer/state',
                json_body={{}},params={{'prepare':'true','format':'1'}},timeout=60)
            if manifest.get('status')!='preparing':
                peer_snapshot.verify_manifest(manifest,link.device_id)
                return await link._download_state(client,ctx,manifest)
            await asyncio.sleep(.3)
        raise AssertionError('full snapshot preparation timed out')
print('RESULT '+json.dumps(asyncio.run(copy())))
""")

    def row(self,sid):
        rows=self.sql('SELECT id,status,is_active,owner_device_id,updated_at FROM sessions WHERE id=%s',(sid,))
        return rows[0] if rows else None

    def publish(self):
        return self.run("from app.network import engine,catalog\nengine.require_context()\nengine.scan_all(strict=True)\ncatalog.publish_owned()")


def canonical(row):return row and row['status']!='archived' and row['is_active'] is False

def paired(a,b):
    status=a.request('GET','/api/v1/network/status')
    if not status.get('network'):
        a.request('POST','/api/v1/network/create',{'name':'Disposable archive upgrade','device_name':'A'})
    invitation=a.request('POST','/api/v1/pairing/grants',{'purpose':'network:join','endpoint_url':a.env['CLUSTER_ADVERTISE_URL']})
    b.request('POST','/api/v1/network/enroll',{'endpoint_url':a.env['CLUSTER_ADVERTISE_URL'],'code':invitation['code'],'device_name':'B'})
    def online():
        statuses=[w.request('GET','/api/v1/network/status') for w in (a,b)]
        for i,s in enumerate(statuses):
            peer=(b,a)[i].device
            if not any(m['device_id']==peer and m.get('online') for m in s.get('members',[])):return False
        return statuses
    wait('real paired workers online',online,180)


def seed(worker,label):
    sid=str(uuid.uuid4())
    worker.run(f"""from app.db import database
from app.db.base import get_db
from scripts.devloop.seed import Transcript
profiles=database.get_all_profiles()
pid=profiles[0]['id']
database.create_session({sid!r},pid,title={('Archive upgrade '+label)!r},is_active=True)
t=Transcript({sid!r},pid,'disposable-model')
t.turn('retained user evidence '+{label!r},reply='retained assistant evidence',tools=[('Bash',{{'command':'printf retained-task'}},'retained-task',False)])
t.save()
with get_db() as conn:
    conn.cursor().execute("UPDATE sessions SET updated_at='2020-01-02T03:04:05+00:00' WHERE id=%s",({sid!r},))
""")
    worker.request('POST',f'/api/v1/sessions/{sid}/archive')
    assert worker.row(sid)['status']=='archived', 'old producing path must actually archive'
    worker.publish()
    return sid


def one_order(order,args,receipts):
    candidate=Path(args.candidate);old=Path(args.old)
    directory=Path(args.scratch)/('upgrade-'+order+'-'+uuid.uuid4().hex[:6])
    directory.mkdir(mode=0o700)
    with contextlib.ExitStack() as stack:
        a=Worker('A',directory/'A',candidate,stack);b=Worker('B',directory/'B',candidate,stack)
        for w in (a,b):w.start(old)
        paired(a,b)
        sa,sb=seed(a,'owned by A'),seed(b,'owned by B')
        ids=[sa,sb];owners={sa:a.device,sb:b.device}
        wait('old archived owner and mirror history',lambda:all(w.row(sid) and w.row(sid)['status']=='archived' for w in (a,b) for sid in ids))
        before={w.name:{sid:{'events':w.sql('SELECT count(*) AS n FROM session_events WHERE session_id=%s',(sid,))[0]['n'],
                      'owner':w.row(sid)['owner_device_id']} for sid in ids} for w in (a,b)}
        first,second=(a,b) if order=='AB' else (b,a)
        first.start(candidate)
        wait('first upgraded peer canonical migration',lambda:all(canonical(first.row(s)) for s in ids))
        first.snapshot(second)
        assert all(canonical(first.row(s)) for s in ids), 'old full snapshot resurrected effective archive'
        # Actual old HTTP/client path targets a mirror owned by the upgraded peer.
        sid=sa if first is a else sb
        second.request('POST',f'/api/v1/sessions/{sid}/archive')
        second.publish()
        wait('actual old legacy archive request remains settled on candidate',lambda:canonical(first.row(sid)))
        first.snapshot(second)
        assert all(canonical(first.row(s)) for s in ids), 'legacy replay resurrected archive'
        second.start(candidate)
        wait('both upgraded peers canonical',lambda:all(canonical(w.row(s)) for w in (a,b) for s in ids))
        for w,p in ((a,b),(b,a)):w.snapshot(p)
        wait('post-upgrade snapshots retain settled history',lambda:all(canonical(w.row(s)) for w in (a,b) for s in ids))
        # Incremental exchange: membership edits must not change ownership.
        b.request('PATCH',f'/api/v1/sessions/{sa}/active',{'is_active':True})
        wait('incremental reactivation converges',lambda:all(w.row(sa)['is_active'] for w in (a,b)))
        a.request('PATCH',f'/api/v1/sessions/{sa}/active',{'is_active':False})
        wait('incremental settlement converges',lambda:all(canonical(w.row(sa)) for w in (a,b)))
        for w in (a,b):w.stop()
        for w in (a,b):w.start(candidate)
        for w,p in ((a,b),(b,a)):w.snapshot(p)
        wait('retained-data restart and full state copy stay canonical',lambda:all(canonical(w.row(s)) for w in (a,b) for s in ids))
        for w in (a,b):
            for sid in ids:
                assert w.row(sid)['owner_device_id']==owners[sid], 'execution ownership changed'
                assert w.sql('SELECT count(*) AS n FROM session_events WHERE session_id=%s',(sid,))[0]['n']>=before[w.name][sid]['events']
        for w in (a,b):w.publish()
        wait('canonical session hashes converge',lambda:a.sql("SELECT record_id,content_hash FROM sync_records WHERE collection='sessions' AND record_id=ANY(%s) ORDER BY record_id",(ids,))==b.sql("SELECT record_id,content_hash FROM sync_records WHERE collection='sessions' AND record_id=ANY(%s) ORDER BY record_id",(ids,)))
        stable={w.name:w.sql("SELECT count(*) AS n FROM sync_ops WHERE collection='sessions' AND record_id=ANY(%s)",(ids,))[0]['n'] for w in (a,b)}
        for _ in range(3):
            for w in (a,b):w.publish()
        assert all(w.sql("SELECT count(*) AS n FROM sync_ops WHERE collection='sessions' AND record_id=ANY(%s)",(ids,))[0]['n']==stable[w.name] for w in (a,b)), 'scanner republish loop'
        hashes={w.name:w.sql("SELECT record_id,content_hash,order_seq FROM sync_records WHERE collection='sessions' AND record_id=ANY(%s) ORDER BY record_id",(ids,)) for w in (a,b)}
        receipts.append({'order':order,'ids':ids,'owners':owners,'before':before,'hashes':hashes,'result':'passed'})
        Path(args.scratch,'upgrade-results.json').write_text(json.dumps(receipts,indent=2,default=str))
        print('PASS upgrade '+order+' all apps and PostgreSQL cleaned on exit',flush=True)



def legacy_inputs(args):
    directory=Path(args.scratch)/('legacy-'+uuid.uuid4().hex[:6]);directory.mkdir(mode=0o700)
    receipt={'inputs':[]}
    with contextlib.ExitStack() as stack:
        a=Worker('A',directory/'A',Path(args.candidate),stack);b=Worker('B',directory/'B',Path(args.candidate),stack)
        for w in (a,b):w.start(args.old)
        paired(a,b)
        sa,sb=seed(a,'legacy owner A'),seed(b,'legacy owner B')
        wait('legacy catalogs mirrored',lambda:a.row(sb) and b.row(sa))
        original={sid:(a if sid==sa else b).sql('SELECT id,event_type,payload FROM session_events WHERE session_id=%s ORDER BY id',(sid,)) for sid in (sa,sb)}
        a.start(args.candidate)
        caller=str(uuid.uuid4())
        b.run(f"from app.db import database\npid=database.get_all_profiles()[0]['id']\ndatabase.create_session({caller!r},pid,title='Old tool caller',is_active=True)")
        for name in ('archive-http','active-flag','generic-status-patch','ArchiveSession','session-active'):
            a.request('PATCH',f'/api/v1/sessions/{sa}/active',{'is_active':True})
            wait(name+' active before legacy input',lambda:b.row(sa)['is_active'])
            if name=='archive-http':b.request('POST',f'/api/v1/sessions/{sa}/archive')
            elif name=='active-flag':b.request('PATCH',f'/api/v1/sessions/{sa}/active',{'is_active':True,'archived':True})
            elif name=='generic-status-patch':
                # An actual legacy HTTP shape against the upgraded server.
                # The old server's generic bypass edits only its local catalog,
                # not a signed intent to a different execution owner.
                a.request('PATCH',f'/api/v1/sessions/{sa}?status=archived')
                b.request('PATCH',f'/api/v1/sessions/{sb}?status=archived')
            elif name=='ArchiveSession':
                b.run(f"""import asyncio
from fluid.tools.registry import ToolCtx
from fluid.tools.builtins.session_orchestration import SPECS
from app.core import session_orchestration as orch
ctx=ToolCtx(extra={{'session_orchestration':orch.make_handler(lambda:{caller!r})}})
async def invoke():
    _,handler=next((spec,fn) for spec,fn in SPECS if spec.name=='ArchiveSession')
    result=await handler({{'session_id':{sa!r}}},ctx)
    assert not result.is_error,result.text
asyncio.run(invoke())
""")
            else:
                b.run(f"""import asyncio
from app.network import control
asyncio.run(control.call({a.device!r},'session-active',{{'session_id':{sa!r},'is_active':True,'archived':True}}))
""")
            wait(name+' canonical settlement from old producer',lambda:canonical(a.row(sa)))
            b.publish();a.snapshot(b)
            assert canonical(a.row(sa)),name+' resurrected archive after old snapshot'
            receipt['inputs'].append({'name':name,'result':'passed','producer':'old app/tool/control; generic cached HTTP shape against new app'})
        # Catalog-only legacy producer: no session_active record exists for it.
        catalog_id=str(uuid.uuid4())
        b.run(f"""from app.db import database
from app.db.base import get_db
pid=database.get_all_profiles()[0]['id']
database.create_session({catalog_id!r},pid,title='Catalog-only legacy archive',is_active=False)
with get_db() as conn:
    c=conn.cursor();c.execute("DELETE FROM network_session_active WHERE session_id=%s",({catalog_id!r},))
    c.execute("UPDATE sessions SET status='archived' WHERE id=%s",({catalog_id!r},))
""")
        b.publish();a.snapshot(b)
        assert canonical(a.row(catalog_id)),'catalog-only archived insertion was not normalized'
        a.request('PATCH',f'/api/v1/sessions/{catalog_id}/active',{'is_active':True})
        wait('newer active membership visible on old catalog owner',lambda:b.row(catalog_id)['is_active'])
        assert b.row(catalog_id)['status']=='archived','must actually replay archived old catalog'
        # A new archived catalog revision, arriving behind the newer membership.
        b.sql('UPDATE sessions SET title=title||%s WHERE id=%s',(' delayed',catalog_id))
        b.publish();a.snapshot(b)
        assert a.row(catalog_id)['status']=='active' and a.row(catalog_id)['is_active']
        receipt['catalog_only']={'result':'canonical insertion and stale archived catalog behind newer active membership passed',
            'old_owner_status':b.row(catalog_id)['status'],
            'policy':'3764 owner still renders its own archived status; complete owner UX requires its upgrade. This proves catalog-only input, not an untested pre-membership binary.'}
        b.start(args.candidate)
        # Migration is intentional settlement; let that new intent converge.
        wait('old owner upgraded catalog canonical',lambda:b.row(catalog_id)['status']=='active')
        for w in (a,b):w.stop()
        for w in (a,b):w.start(args.candidate)
        for w,p in ((a,b),(b,a)):w.snapshot(p)
        for w in (a,b):
            for sid,events in original.items():
                actual={e['id']:e for e in w.sql('SELECT id,event_type,payload FROM session_events WHERE session_id=%s',(sid,))}
                assert all(actual.get(e['id'])==e for e in events),'original transcript/task event lost or changed'
                assert w.row(sid)['status']=='active'
                assert w.row(sid)['owner_device_id']==(a.device if sid==sa else b.device)
        for w in (a,b):w.publish()
        ids=[sa,sb,catalog_id]
        wait('legacy restart hashes converge',lambda:a.sql("SELECT record_id,content_hash FROM sync_records WHERE collection='sessions' AND record_id=ANY(%s) ORDER BY record_id",(ids,))==b.sql("SELECT record_id,content_hash FROM sync_records WHERE collection='sessions' AND record_id=ANY(%s) ORDER BY record_id",(ids,)))
        receipt['restart']='both candidates restarted; full snapshots; exact original transcript/tool-event retention and ownership passed'
        Path(args.scratch,'legacy-results.json').write_text(json.dumps(receipt,indent=2,default=str))
    print('PASS legacy input stage; both apps and PostgreSQL stopped',flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--old',required=True);p.add_argument('--candidate',required=True);p.add_argument('--scratch',required=True);p.add_argument('--smoke',action='store_true');p.add_argument('--stage',choices=('upgrade','legacy','matrix','offline'),default='upgrade')
    args=p.parse_args();receipts=[]
    scratch=Path(args.scratch).resolve()
    assert scratch.is_dir() and scratch.stat().st_uid==os.getuid(), 'scratch must be an existing caller-owned directory'
    for checkout in (args.old,args.candidate):
        assert not scratch.is_relative_to(Path(checkout).resolve()), 'scratch must be outside both source checkouts'
    stamp=Path(args.old)/'.source-commit'
    old_sha=stamp.read_text().strip() if stamp.is_file() else subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.old,text=True).strip()
    assert args.smoke or old_sha.startswith('3764f2f81'), 'old checkout must be pinned at the requested compatibility baseline'
    if args.smoke:
        directory=Path(args.scratch)/('startup-smoke-'+uuid.uuid4().hex[:6]);directory.mkdir(mode=0o700)
        with contextlib.ExitStack() as stack:
            a=Worker('A',directory/'A',Path(args.candidate),stack);b=Worker('B',directory/'B',Path(args.candidate),stack)
            for w in (a,b):w.start(args.candidate)
            paired(a,b)
        print('PASS startup smoke stopped both apps and PostgreSQL',flush=True)
    elif args.stage=='legacy':
        legacy_inputs(args)
    elif args.stage=='matrix':
        from archive_retirement_matrix import run
        run(args)
    elif args.stage=='offline':
        from archive_retirement_offline import run
        run(args)
    else:
        for order in ('AB','BA'):one_order(order,args,receipts)

if __name__=='__main__':main()
