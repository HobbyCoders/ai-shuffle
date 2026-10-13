import { describe, it, expect, vi } from 'vitest';
import { PreferenceSync, type PreferenceTransport } from './preference-sync';
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason?: unknown) => void; const promise = new Promise<T>((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; }
const settle = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
function fixture(initial: Record<string, unknown> = { theme: 'server' }) {
 let value: unknown = 'legacy';
 const server = { ...initial };
 const transport: PreferenceTransport = {
  all: vi.fn(async () => ({ ...server })),
  get: vi.fn(async key => key in server ? { value: server[key] } : null),
  put: vi.fn(async (key, next, missing) => { if (!missing || !(key in server)) server[key] = next; return { value: server[key] }; })
 };
 const sync = new PreferenceSync(transport);
 const apply = vi.fn(next => { value = next ?? 'default'; });
 sync.register('theme', { read: () => value, apply });
 sync.start();
 return { sync, transport, server, apply, value: () => value, edit: (next: unknown) => { value=next; sync.save('theme', next); } };
}
describe('authoritative preference coordination', () => {
 it('reads retired family records only from snapshots and never recreates them or follows alias GETs', async () => {
  const family='composer.modelEffort.opus', concrete='composer.modelEffort.claude-opus-5';
  const server:Record<string,unknown>={[family]:'low',[concrete]:'max'};
  const values:Record<string,unknown>={};
  const transport:PreferenceTransport={all:vi.fn(async()=>({...server})),
   get:vi.fn(async()=>({value:'max'})),put:vi.fn(async(_key,value)=>({value}))};
  const sync=new PreferenceSync(transport);
  sync.registerPrefix('composer.modelEffort.',key=>({readOnly:key===family,read:()=>values[key],apply:v=>values[key]=v}));
  sync.start();
  // Early invalidations must not make snapshot await its own bootstrap.
  await sync.refresh(family); await sync.hydrate();
  expect(values[family]).toBe('low'); expect(values[concrete]).toBe('max');
  server[family]='medium'; await sync.refresh(family);
  expect(values[family]).toBe('medium'); expect(transport.get).not.toHaveBeenCalled();
  sync.save(family,'high'); await settle(); expect(transport.put).not.toHaveBeenCalled();
  delete server[family]; await sync.refresh(family);
  expect(values[family]).toBeUndefined(); expect(transport.put).not.toHaveBeenCalled();
  const missing=new PreferenceSync(transport), reset=vi.fn();
  missing.register(family,{readOnly:true,read:()=> 'cached legacy choice',apply:reset});
  missing.start(); await missing.hydrate();
  expect(reset).toHaveBeenCalledWith(undefined); expect(transport.put).not.toHaveBeenCalled();
 });
 it('keeps simultaneous per-model edits and clears from two clients and discovers new remote entries', async () => {
  const server:Record<string,unknown> = {'composer.modelEffort.opus':'medium','composer.modelEffort.sol':'high'};
  const versions:Record<string,string> = Object.fromEntries(Object.keys(server).map(k=>[k,'v1']));
  let revision=1;
  const transport:PreferenceTransport = {
   all:async()=>({...server}),
   allWithVersions:async()=>({preferences:{...server},versions:{...versions}}),
   get:async key=>key in server?{value:server[key],version:versions[key]}:null,
   put:vi.fn(async(key,value,missing,expected)=>{
    if (missing && key in server) return {value:server[key],version:versions[key]};
    if (expected !== undefined && expected !== (versions[key]??null)) throw {status:409};
    server[key]=value; versions[key]=`v${++revision}`; return {value,version:versions[key]};
   })
  };
  const client=()=>{
   const values:Record<string,unknown>={}; const sync=new PreferenceSync(transport);
   sync.registerPrefix('composer.modelEffort.',key=>({read:()=>values[key]??null,apply:v=>values[key]=v}));
   sync.start(); return {sync,values};
  };
  const a=client(),b=client(); await Promise.all([a.sync.hydrate(),b.sync.hydrate()]);
  a.sync.save('composer.modelEffort.opus','low'); b.sync.save('composer.modelEffort.sol','xhigh');
  await settle(); await Promise.all([a.sync.reconnect(),b.sync.reconnect()]);
  for (const c of [a,b]) expect(c.values).toMatchObject({'composer.modelEffort.opus':'low','composer.modelEffort.sol':'xhigh'});
  a.sync.save('composer.modelEffort.opus',null); b.sync.save('composer.modelEffort.sol','max');
  await settle(); await Promise.all([a.sync.reconnect(),b.sync.reconnect()]);
  for (const c of [a,b]) expect(c.values).toMatchObject({'composer.modelEffort.opus':null,'composer.modelEffort.sol':'max'});
  server['composer.modelEffort.terra']='ultra'; versions['composer.modelEffort.terra']='v99';
  await b.sync.refresh('composer.modelEffort.terra');
  expect(b.values['composer.modelEffort.terra']).toBe('ultra');
 });
 it('hydrate joins the snapshot in flight and reads nothing once ready; reconnect asks again', async () => {
  const f=fixture();
  const first=f.sync.reconnect();
  await f.sync.hydrate(); await first;
  expect(f.transport.all).toHaveBeenCalledTimes(1);
  await f.sync.hydrate();
  expect(f.transport.all).toHaveBeenCalledTimes(1);
  await f.sync.reconnect();
  expect(f.transport.all).toHaveBeenCalledTimes(2);
 });
 it('hydrate reads the snapshot when none has been taken', async () => {
  const f=fixture();
  await f.sync.hydrate();
  expect(f.transport.all).toHaveBeenCalledTimes(1); expect(f.value()).toBe('server');
 });
 it('serializes a versioned migration before the user edit made during its response', async () => {
  const f=fixture({});
  f.transport.allWithVersions=async()=>({preferences:{},versions:{}});
  const migration=deferred<{value:unknown;version:string}>();
  f.transport.put=vi.fn().mockReturnValueOnce(migration.promise).mockResolvedValue({value:'new-pick',version:'v2'});
  f.transport.get=async()=>({value:'new-pick',version:'v2'});
  const ready=f.sync.reconnect(); await settle();
  f.edit('new-pick');
  expect(f.transport.put).toHaveBeenCalledTimes(1);
  migration.resolve({value:'legacy',version:'v1'}); await ready;
  expect(f.transport.put).toHaveBeenLastCalledWith('theme','new-pick',false,'v1');
  expect(f.value()).toBe('new-pick');
 });
 it('reconciles a stale offline edit instead of overwriting another device', async () => {
  const f=fixture(); let version='v1';
  f.transport.allWithVersions=async()=>({preferences:{...f.server},versions:{theme:version}});
  f.transport.get=async()=>({value:f.server.theme,version:version});
  f.transport.put=vi.fn(async (_key,value,_missing,expected)=>{
   if (expected !== version) throw {status:409};
   f.server.theme=value; version='v2'; return {value,version:version};
  });
  await f.sync.reconnect();
  // Other device commits, before this device observes its invalidation.
  f.server.theme='other-device'; version='other-version';
  f.edit('stale-local'); await settle();
  expect(f.transport.put).toHaveBeenCalledWith('theme','stale-local',false,'v1');
  expect(f.server.theme).toBe('other-device'); expect(f.value()).toBe('other-device');
 });
 it('applies remote values and deletions without echo saves', async () => {
  const f=fixture(); await f.sync.reconnect(); expect(f.value()).toBe('server');
  f.server.theme='remote'; await f.sync.refresh('theme'); expect(f.value()).toBe('remote');
  delete f.server.theme; await f.sync.refresh('theme'); expect(f.value()).toBe('default');
  await f.sync.reconnect(); expect(f.transport.put).not.toHaveBeenCalled();
 });
 it('migrates only if missing, accepting the concurrent winner', async () => {
  const f=fixture({}); const migration=deferred<{value: unknown}>();
  vi.mocked(f.transport.put).mockReturnValueOnce(migration.promise);
  const ready=f.sync.reconnect(); await settle();
  expect(f.transport.put).toHaveBeenCalledWith('theme','legacy',true);
  migration.resolve({value:'other device won'}); await ready; expect(f.value()).toBe('other device won');
 });
 it('does not mistake a failed startup GET for absent preferences', async () => {
  const f=fixture(); vi.mocked(f.transport.all).mockRejectedValueOnce(new Error('offline'));
  await f.sync.reconnect(); expect(f.transport.put).not.toHaveBeenCalled(); expect(f.value()).toBe('legacy');
 });
 it('serializes per-key writes and keeps edits made during a save', async () => {
  const f=fixture(); await f.sync.reconnect(); const first=deferred<{value:unknown}>();
  vi.mocked(f.transport.put).mockImplementationOnce(() => first.promise);
  f.edit('one'); f.edit('two'); f.edit('three'); expect(f.transport.put).toHaveBeenCalledTimes(1);
  first.resolve({value:'one'}); await settle();
  expect(vi.mocked(f.transport.put).mock.calls.map(c=>c[1])).toEqual(['one','three']);
  expect(f.value()).toBe('three');
 });
 it('late fetches cannot override an edit even when the save already completed', async () => {
  const f=fixture(); await f.sync.reconnect(); const old=deferred<{value:unknown}|null>();
  vi.mocked(f.transport.get).mockReturnValueOnce(old.promise);
  const refresh=f.sync.refresh('theme'); f.edit('new'); await settle();
  old.resolve({value:'old'}); await refresh; expect(f.value()).toBe('new');
 });
 it('startup snapshot cannot override edits made while it was in flight', async () => {
  const f=fixture(); const all=deferred<Record<string,unknown>>(); vi.mocked(f.transport.all).mockReturnValueOnce(all.promise);
  const ready=f.sync.reconnect(); f.edit('typed during bootstrap'); all.resolve({theme:'old'}); await ready;
  expect(f.value()).toBe('typed during bootstrap'); expect(f.server.theme).toBe('typed during bootstrap');
 });
 it('retains genuinely failed saves and retries on reconnect before applying a stale snapshot', async () => {
  const f=fixture(); await f.sync.reconnect(); vi.mocked(f.transport.put).mockRejectedValueOnce(new Error('offline'));
  f.edit('unsaved'); await settle(); expect(f.value()).toBe('unsaved');
  await f.sync.reconnect(); expect(f.server.theme).toBe('unsaved'); expect(f.value()).toBe('unsaved');
 });
 it('reconciles invalidations racing the PUT response using a fresh authoritative GET', async () => {
  const f=fixture(); await f.sync.reconnect(); const put=deferred<{value:unknown}>(); vi.mocked(f.transport.put).mockReturnValueOnce(put.promise);
  f.edit('mine'); f.server.theme='another device after mine'; await f.sync.refresh('theme');
  put.resolve({value:'mine'}); await settle(); expect(f.value()).toBe('another device after mine');
 });
 it('logout retires in-flight work and pending saves; next account starts from defaults', async () => {
  const f=fixture(); await f.sync.reconnect(); const put=deferred<{value:unknown}>(); vi.mocked(f.transport.put).mockReturnValueOnce(put.promise);
  f.edit('private account A'); f.edit('pending A'); f.sync.stop(); expect(f.value()).toBe('default');
  f.sync.start(true); f.server.theme='account B'; await f.sync.reconnect();
  put.resolve({value:'private account A'}); await settle(); expect(f.value()).toBe('account B');
  expect(vi.mocked(f.transport.put).mock.calls.some(c=>c[1]==='pending A')).toBe(false);
 });
 it('refetches invalidations received before the first snapshot resolves', async () => {
  const f=fixture(); const all=deferred<Record<string,unknown>>();
  vi.mocked(f.transport.all).mockReturnValueOnce(all.promise);
  const ready=f.sync.reconnect();
  f.server.theme='remote update during bootstrap';
  await f.sync.refresh('theme');
  all.resolve({theme:'stale snapshot'}); await ready;
  expect(f.value()).toBe('remote update during bootstrap');
  expect(f.transport.put).not.toHaveBeenCalled();
 });
 it('does not resurrect a deleted server preference from an offline device cache after reload', async () => {
  localStorage.clear();
  const first=fixture(); first.sync.start(false,'alice'); await first.sync.reconnect(); first.sync.stop(false);
  const returning=fixture({}); returning.sync.start(false,'alice'); await returning.sync.reconnect();
  expect(returning.value()).toBe('default'); expect(returning.transport.put).not.toHaveBeenCalled();
  const other=fixture({}); other.sync.start(false,'bob'); await other.sync.reconnect();
  expect(other.transport.put).toHaveBeenCalledWith('theme','legacy',true);
 });
 it('migration responses cannot overwrite a newer user edit', async () => {
  const f=fixture({}); const migration=deferred<{value:unknown}>(); vi.mocked(f.transport.put).mockReturnValueOnce(migration.promise);
  const ready=f.sync.reconnect(); await settle(); f.edit('new'); await settle(); migration.resolve({value:'legacy'}); await ready;
  expect(f.value()).toBe('new');
 });
});
