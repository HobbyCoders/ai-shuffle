import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
const m = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: m, getErrorMessage: (e: {detail?: string}) => e.detail ?? 'Unavailable' }));
const accessAuth = vi.hoisted(() => {
 type Status = Record<string, unknown>;
 const listeners = new Set<(state: {status: Status}) => void>();
 const store = {
  current: {status: {preview_access: false} as Status},
  subscribe(fn: (state: {status: Status}) => void) { listeners.add(fn); fn(store.current); return () => { listeners.delete(fn); }; },
  set(status: Status) { store.current = {status}; listeners.forEach(fn => fn(store.current)); },
 };
 return store;
});
vi.mock('$lib/stores/auth.svelte', () => ({auth: accessAuth}));
import ChatAccess from './ChatAccess.svelte';
import { resourceSync } from '$lib/services/resource-sync';
const empty = () => ({ requests: [], actions: [], grants: [], saved_connections: [], vault_available: false });
let snapshot: ReturnType<typeof empty> | Record<string, unknown>;
const accessCalls = () => m.get.mock.calls.filter(([url]) => url.startsWith('/chat-access/'));
const sessionCalls = () => m.get.mock.calls.filter(([url]) => url.startsWith('/sessions/'));
const flush = async () => { await vi.advanceTimersByTimeAsync(0); await tick(); };
function hidden(value: boolean) {
 Object.defineProperty(document, 'hidden', { configurable: true, value });
 document.dispatchEvent(new Event('visibilitychange'));
}
beforeEach(() => {
 vi.useFakeTimers(); vi.clearAllMocks(); hidden(false); snapshot = empty(); accessAuth.current = {status: {preview_access: false}};
 m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? { mode: 'code' } : snapshot);
 m.post.mockResolvedValue({}); m.delete.mockResolvedValue({});
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
describe('private-access recovery and live invalidations', () => {
 it('does not poll owner-only access or render errors in a session preview', async () => {
  accessAuth.current.status.preview_access = true;
  render(ChatAccess, {sessionId: 'preview'}); await flush();
  resourceSync.invalidate(['sessions', 'chat-access:preview']);
  await vi.advanceTimersByTimeAsync(600_000); hidden(true); hidden(false); await flush();
  expect(m.get).not.toHaveBeenCalled();
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it.each(['code', 'chat', 'voice', null])('does not request local grants for a remote %s session', async (mode) => {
  m.get.mockResolvedValue({mode, is_remote: true});
  render(ChatAccess, {sessionId: 'remote'}); await flush();
  await vi.advanceTimersByTimeAsync(300_000);
  resourceSync.invalidate(['chat-access:remote']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(0);
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.queryByLabelText('Temporary access in this conversation')).toBeNull();
 });
 it('rechecks ownership when a remote session is adopted and clears private state when moved away', async () => {
  let remote = true;
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode: 'code', is_remote: remote} : snapshot);
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(accessCalls()).toHaveLength(0);
  remote = false;
  snapshot = {...empty(), grants:[{id:'g1',label:'Local folder',kind:'folder',target:'folder',expires_at:9999999999,writable:false}]};
  resourceSync.invalidate(['sessions']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(1);
  expect(screen.getByText('Local folder')).toBeTruthy();
  remote = true;
  resourceSync.invalidate(['sessions']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByText('Local folder')).toBeNull();
  await vi.advanceTimersByTimeAsync(300_000);
  expect(accessCalls()).toHaveLength(1);
 });

 it('does not poll an empty snapshot, wakes on scoped events, and polls pending approvals promptly', async () => {
  render(ChatAccess, { sessionId: 's1' }); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
  snapshot = { ...empty(), actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999 }] };
  resourceSync.invalidate(['chat-access:other']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.getByText('Approve this remote command?')).toBeTruthy();
  expect(accessCalls()).toHaveLength(3);
  expect(sessionCalls()).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(2000);
  expect(accessCalls()).toHaveLength(4);
  snapshot = empty();
  await fireEvent.click(screen.getByRole('button', {name: 'Approve once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/s1/actions/a1', {allow: true});
  expect(screen.queryByText('Approve this remote command?')).toBeNull();
 });
 it('keeps request form input across recovery polls and removes expired access from the next snapshot', async () => {
  snapshot = {...empty(), requests:[{id:'r1',kind:'folder',purpose:'Read the requested file',expires_at:9999999999}]};
  render(ChatAccess,{sessionId:'s1'}); await flush();
  const label = screen.getByRole('textbox',{name:'Resource name'}) as HTMLInputElement;
  await fireEvent.input(label,{target:{value:'My folder'}});
  await vi.advanceTimersByTimeAsync(2000); await tick();
  expect(accessCalls()).toHaveLength(2);
  expect((screen.getByRole('textbox',{name:'Resource name'}) as HTMLInputElement).value).toBe('My folder');
  snapshot = {...empty(),grants:[{id:'g1',label:'My folder',kind:'folder',target:'/safe',expires_at:9999999999,writable:false}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByRole('textbox',{name:'Resource name'})).toBeNull();
  expect(screen.getByText('My folder')).toBeTruthy();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(3);
  // The broker announces grant expiry; no grant polling is needed.
  snapshot = empty();
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByText('My folder')).toBeNull();
 });
 it('does not fetch private state for an unsupported persisted mode', async () => {
  m.get.mockResolvedValue({mode:'unsupported'});
  render(ChatAccess,{sessionId:'s1'}); await flush();
  await vi.advanceTimersByTimeAsync(300_000);
  expect(m.get).toHaveBeenCalledTimes(1); expect(accessCalls()).toHaveLength(0);
 });
 it('stops on persistent denial and retries on reconnect without bypassing authorization', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return { mode: 'chat' };
   throw { status: 403, detail: 'Denied' };
  });
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(screen.getByRole('alert').textContent).toBe('Denied');
  await vi.advanceTimersByTimeAsync(3_600_000); expect(accessCalls()).toHaveLength(1);
  m.get.mockResolvedValue(empty());
  window.dispatchEvent(new Event('online')); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(2);
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it('keeps denial backoff across broad and scoped resource events', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return {mode: 'chat'};
   throw {status: 403, detail: 'Denied'};
  });
  render(ChatAccess, {sessionId: 's1'}); await flush();
  for (let i = 0; i < 30; i++) {
   resourceSync.invalidate(['sessions', 'chat-access:s1']);
   await vi.advanceTimersByTimeAsync(2000);
  }
  expect(accessCalls()).toHaveLength(1);
 });
 it('does not immediately retry a denial dirtied while in flight or revive after unmount', async () => {
  let fail!: (value: unknown) => void;
  m.get.mockImplementation((url: string) => url.startsWith('/sessions/')
   ? Promise.resolve({mode: 'code'}) : new Promise((_resolve, reject) => {fail = reject;}));
  const view = render(ChatAccess, {sessionId: 's1'}); await flush();
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100);
  fail({status:403}); await flush();
  expect(accessCalls()).toHaveLength(1);
  view.unmount();
  window.dispatchEvent(new Event('online'));
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
 });
 it('backs off transient failures and clears the error after recovery', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return {mode: 'voice'};
   throw {status: 503, detail: 'Offline'};
  });
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(5000); expect(accessCalls()).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(9999); expect(accessCalls()).toHaveLength(2);
  m.get.mockResolvedValue(empty());
  await vi.advanceTimersByTimeAsync(1); await tick();
  expect(accessCalls()).toHaveLength(3); expect(screen.queryByRole('alert')).toBeNull();
 });
 it('does no hidden polling and catches up immediately on return or online', async () => {
  hidden(true); const view = render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(m.get).not.toHaveBeenCalled();
  hidden(false); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(1);
  hidden(true); resourceSync.invalidate(['chat-access:s1']);
  await vi.advanceTimersByTimeAsync(300_000); expect(accessCalls()).toHaveLength(1);
  hidden(false); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(2);
  window.dispatchEvent(new Event('online')); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(3);
  view.unmount(); await vi.advanceTimersByTimeAsync(300_000); expect(accessCalls()).toHaveLength(3);
 });
 it('keeps one follow-up when an access change arrives during a request', async () => {
  let finish!: (value: unknown) => void;
  m.get.mockImplementation((url: string) => url.startsWith('/sessions/') ? Promise.resolve({mode: 'code'}) : new Promise(resolve => {finish = resolve;}));
  render(ChatAccess, {sessionId: 's1'}); await flush();
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(1);
  m.get.mockResolvedValue(empty()); finish(empty()); await flush();
  expect(accessCalls()).toHaveLength(2);
 });
});

// Issue #1281: request counts over simulated time. Previously an empty snapshot
// polled every 30 s, any grant/request/action every 2 s, and a denial every 300 s.
describe('private-access request rate', () => {
 const deny = () => m.get.mockImplementation(async (url: string) => {
  if (url.startsWith('/sessions/')) return {mode: 'code'};
  throw {status: 403, detail: 'Denied'};
 });
 const pending = () => ({...empty(), requests: [{id: 'r1', kind: 'folder', purpose: 'Read a file', expires_at: 9999999999}]});
 it('empty: one request on mount, none over the next ten minutes', async () => {
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
  expect(sessionCalls()).toHaveLength(1);
 });
 it('grant only: no polling while access is active', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Folder', kind: 'folder', target: '/x', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
 });
 it('denied (403): stops despite a two-second event stream until the sign-in changes', async () => {
  deny();
  render(ChatAccess, {sessionId: 's1'}); await flush();
  for (let i = 0; i < 300; i++) {
   resourceSync.invalidate(['sessions', 'chat-access', 'chat-access:s1']);
   await vi.advanceTimersByTimeAsync(2000);
  }
  hidden(true); hidden(false); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(1);
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode: 'code'} : snapshot);
  accessAuth.set({preview_access: false, authenticated: true, is_admin: true, username: 'owner'});
  await flush();
  expect(accessCalls()).toHaveLength(2);
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it('pending: polls 2 s doubling to a 30 s cap, and an event resets the interval', async () => {
  snapshot = pending();
  render(ChatAccess, {sessionId: 's1'}); await flush();
  const at = (ms: number) => vi.advanceTimersByTimeAsync(ms).then(() => accessCalls().length);
  // Mount, then +2, +4, +8, +16, +30 s safety polls.
  expect(await at(60_000)).toBe(6);
  // Steady state: two per minute.
  expect(await at(60_000)).toBe(8);
  resourceSync.invalidate(['chat-access:s1']);
  expect(await at(100)).toBe(9);
  expect(await at(2000)).toBe(10);
  snapshot = empty();
  expect(await at(4000)).toBe(11);
  expect(await at(600_000)).toBe(11);
 });
 it('reconnect: a full invalidation checks the session and snapshot once each', async () => {
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect([sessionCalls().length, accessCalls().length]).toEqual([1, 1]);
  // live-sync invalidates every resource on sync_ready and on return to the tab.
  resourceSync.invalidate(); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([2, 2]);
  resourceSync.invalidate(); hidden(true); hidden(false); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([3, 3]);
 });
 it('hidden tab: no requests while hidden, even for pending approvals or events', async () => {
  snapshot = pending();
  render(ChatAccess, {sessionId: 's1'}); await flush();
  hidden(true);
  for (let i = 0; i < 60; i++) {
   resourceSync.invalidate(['sessions', 'chat-access:s1']);
   await vi.advanceTimersByTimeAsync(10_000);
  }
  expect(accessCalls()).toHaveLength(1);
  hidden(false); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([2, 2]);
 });
});
