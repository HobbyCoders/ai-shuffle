import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
const m = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: m, getErrorMessage: (e: {detail?: string}) => e.detail ?? 'Unavailable' }));
import ChatAccess from './ChatAccess.svelte';
import { resourceSync } from '$lib/services/resource-sync';
const empty = () => ({ requests: [], actions: [], grants: [], saved_connections: [], vault_available: false });
let snapshot: ReturnType<typeof empty> | Record<string, unknown>;
const accessCalls = () => m.get.mock.calls.filter(([url]) => url.startsWith('/chat-access/'));
const flush = async () => { await vi.advanceTimersByTimeAsync(0); await tick(); };
function hidden(value: boolean) {
 Object.defineProperty(document, 'hidden', { configurable: true, value });
 document.dispatchEvent(new Event('visibilitychange'));
}
beforeEach(() => {
 vi.useFakeTimers(); vi.clearAllMocks(); hidden(false); snapshot = empty();
 m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? { mode: 'code' } : snapshot);
 m.post.mockResolvedValue({}); m.delete.mockResolvedValue({});
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
describe('private-access recovery and live invalidations', () => {
 it('uses slow empty recovery, wakes on scoped events, and polls pending approvals promptly', async () => {
  render(ChatAccess, { sessionId: 's1' }); await flush();
  await vi.advanceTimersByTimeAsync(29_999);
  expect(accessCalls()).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(accessCalls()).toHaveLength(2);
  snapshot = { ...empty(), actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999 }] };
  resourceSync.invalidate(['chat-access:other']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.getByText('Approve this remote command?')).toBeTruthy();
  expect(accessCalls()).toHaveLength(3);
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
  snapshot = empty();
  await vi.advanceTimersByTimeAsync(2000); await tick();
  expect(screen.queryByText('My folder')).toBeNull();
 });
 it('does not fetch private state for an unsupported persisted mode', async () => {
  m.get.mockResolvedValue({mode:'unsupported'});
  render(ChatAccess,{sessionId:'s1'}); await flush();
  await vi.advanceTimersByTimeAsync(300_000);
  expect(m.get).toHaveBeenCalledTimes(1); expect(accessCalls()).toHaveLength(0);
 });
 it('backs off persistent denial and retries on reconnect without bypassing authorization', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return { mode: 'chat' };
   throw { status: 403, detail: 'Denied' };
  });
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(299_999); expect(accessCalls()).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1); expect(accessCalls()).toHaveLength(2);
  m.get.mockResolvedValue(empty());
  resourceSync.invalidate(); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(3);
  expect(screen.queryByRole('alert')).toBeNull();
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
  hidden(false); await flush(); expect(accessCalls()).toHaveLength(1);
  hidden(true); resourceSync.invalidate(['chat-access:s1']);
  await vi.advanceTimersByTimeAsync(300_000); expect(accessCalls()).toHaveLength(1);
  hidden(false); await flush(); expect(accessCalls()).toHaveLength(2);
  window.dispatchEvent(new Event('online')); await flush(); expect(accessCalls()).toHaveLength(3);
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
