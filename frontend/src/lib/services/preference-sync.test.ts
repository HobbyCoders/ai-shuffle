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
