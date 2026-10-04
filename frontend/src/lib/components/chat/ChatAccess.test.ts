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
import PrivateAccessPrompt from './PrivateAccessPrompt.svelte';
import { get } from 'svelte/store';
import { privateAccessPending } from '$lib/stores/chat-access';
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
 HTMLDialogElement.prototype.showModal = function () { this.open = true; };
 HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
 vi.useFakeTimers(); vi.clearAllMocks(); hidden(false); snapshot = empty(); accessAuth.current = {status: {preview_access: false}};
 m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? { mode: 'code' } : snapshot);
 m.post.mockResolvedValue({}); m.delete.mockResolvedValue({});
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
describe('private access header disclosure', () => {
 // #1807: a request raised while the chat is open shows IN the chat, and the
 // session is marked as needing the owner, without opening the header panel.
 it('shows a request raised in the open chat as an in-chat prompt and marks the session', async () => {
  render(ChatAccess, {sessionId: 's1', trigger: 'none'});
  render(PrivateAccessPrompt, {sessionId: 's1'});
  await flush();
  expect(screen.queryByRole('region', {name: /private access waiting/i})).toBeNull();
  snapshot = {...empty(), requests: [{id: 'r1', kind: 'machine', purpose: 'Check the container logs', expires_at: 9999999999}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  const region = screen.getByRole('region', {name: /private access waiting/i});
  expect(region.textContent).toContain('Check the container logs');
  expect(get(privateAccessPending).get('s1')).toBe(1);
  await fireEvent.click(screen.getByRole('button', {name: 'Review'}));
  expect((document.querySelector('dialog.access-dialog') as HTMLDialogElement).open).toBe(true);
  snapshot = empty();
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByRole('region', {name: /private access waiting/i})).toBeNull();
  expect(get(privateAccessPending).has('s1')).toBe(false);
 });
 it('resumes a service setup after dialog close without keeping its token or consent', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'service',purpose:'Read inventory',expires_at:9999999999,service_provider:'cloudflare',suggested_url:'https://api.cloudflare.com/client/v4'}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  await fireEvent.click(screen.getByRole('button', {name:'Continue to setup'}));
  await fireEvent.click(screen.getByRole('button', {name:'I have a token'}));
  await fireEvent.input(screen.getByLabelText('API key or token'), {target:{value:'test-only-private-token'}});
  await fireEvent.click(screen.getByRole('checkbox', {name:/I approve access/}));
  await fireEvent.click(screen.getByRole('button', {name:'Close private access'}));
  await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  expect((screen.getByLabelText('API key or token') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('checkbox', {name:/I approve access/}) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByText('https://api.cloudflare.com/client/v4')).toBeTruthy();
  expect(m.post).not.toHaveBeenCalled();
 });
 it('keeps Escape inside the dialog instead of triggering global chat shortcuts', async () => {
  snapshot = {...empty(), actions:[{id:'a1',grant_id:'g1',operation:'command',arguments:{command:'pwd'},expires_at:9999999999}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  const shortcut = vi.fn();
  window.addEventListener('keydown', shortcut);
  try {
   const dialog = screen.getByRole('dialog') as HTMLDialogElement;
   // A completed request removes its focused button; Escape then targets body.
   await fireEvent.keyDown(document.body, {key:'Escape'});
   expect(dialog.open).toBe(false);
   expect(shortcut).not.toHaveBeenCalled();
  } finally { window.removeEventListener('keydown', shortcut); }
 });
 it('never reopens the dialog or grants after streaming invalidations and fresh snapshots', async () => {
  const grant = {id:'g1',label:'Granted folder',kind:'folder',target:'/safe',expires_at:9999999999,writable:true};
  snapshot = {...empty(), grants:[grant]};
  const view = render(ChatAccess, {sessionId:'s1'}); await flush();
  const dialog = view.container.querySelector('dialog')!;
  const details = view.container.querySelector('details')!;
  expect(dialog.open).toBe(false);
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  details.open = true; await fireEvent(details, new Event('toggle')); await tick();
  details.open = false; await fireEvent(details, new Event('toggle')); await tick();
  for (let i = 0; i < 5; i++) {
   snapshot = {...empty(), grants:[{...grant}]};
   resourceSync.invalidate(['sessions', 'chat-access:s1']);
   await vi.advanceTimersByTimeAsync(100); await tick();
   await view.rerender({sessionId:'s1'});
   expect(dialog.open).toBe(true);
   expect(details.open).toBe(false);
  }
  await fireEvent.click(screen.getByRole('button', {name:'Close private access'}));
  snapshot = {...empty(), grants:[{...grant}], requests:[{id:'r1',kind:'folder',purpose:'Another request',expires_at:9999999999}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(dialog.open).toBe(false);
  expect(screen.getByText('1 pending')).toBeTruthy();
  await vi.advanceTimersByTimeAsync(6000); await tick();
  expect(dialog.open).toBe(false);
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  expect(screen.getByRole('textbox', {name:'Resource name'})).toBeTruthy();
 });
 it('preserves an explicitly expanded saved-only panel across refreshes', async () => {
  const saved = {id:'saved1',label:'Saved server',target:'server',fingerprint:''};
  snapshot = {...empty(), saved_connections:[saved]};
  const view = render(ChatAccess, {sessionId:'s1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Private access'}));
  const details = view.container.querySelector('details')!;
  details.open = true; await fireEvent(details, new Event('toggle')); await tick();
  snapshot = {...empty(), saved_connections:[{...saved}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(details.open).toBe(true);
 });
});

describe('private-access recovery and live invalidations', () => {
 it('does not poll owner-only access or render errors in a session preview', async () => {
  accessAuth.current.status.preview_access = true;
  render(ChatAccess, {sessionId: 'preview'}); await flush();
  resourceSync.invalidate(['sessions', 'chat-access:preview']);
  await vi.advanceTimersByTimeAsync(600_000); hidden(true); hidden(false); await flush();
  expect(m.get).not.toHaveBeenCalled();
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it.each(['code', 'chat', 'voice', null])('shows a remote %s session\'s owner-side requests through this worker', async (mode) => {
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode, is_remote: true, device_name: 'Owner PC'} : snapshot);
  snapshot = {...empty(), vault_available: true,
   requests: [{id: 'r1', kind: 'folder', purpose: 'Read the notes', expires_at: 9999999999}],
   saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: '', kind: 'machine'}]};
  render(ChatAccess, {sessionId: 'remote'}); await flush();
  expect(accessCalls().map(([url]) => url)).toEqual(['/chat-access/remote']);
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
  expect(screen.getByText('Folder on Owner PC')).toBeTruthy();
  expect(screen.getByText(/Encrypted on Owner PC/)).toBeTruthy();
  // The owner's change notice arrives as a scoped invalidation on this worker.
  resourceSync.invalidate(['chat-access:remote']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
 });
 it('rechecks where access lives when a session moves between devices', async () => {
  let remote = true;
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode: 'code', is_remote: remote, device_name: remote ? 'Owner PC' : null} : snapshot);
  snapshot = {...empty(), vault_available: true, saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: ''}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(accessCalls()).toHaveLength(1);
  expect(screen.getByText(/Encrypted on Owner PC/)).toBeTruthy();
  remote = false;
  snapshot = {...empty(), grants:[{id:'g1',label:'Local folder',kind:'folder',target:'folder',expires_at:9999999999,writable:false}]};
  resourceSync.invalidate(['sessions']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(2);
  expect(screen.getByText('Local folder')).toBeTruthy();
  expect(screen.queryByText(/Encrypted on Owner PC/)).toBeNull();
  remote = true;
  snapshot = empty();
  resourceSync.invalidate(['sessions']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(3);
  expect(screen.queryByText('Local folder')).toBeNull();
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
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
  await fireEvent.click(screen.getByRole('button', {name: 'Approve once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/s1/actions/a1', {allow: true});
  expect(screen.queryByText('Approve this remote command?')).toBeNull();
 });
 it('shows an API request approval with its method, path and body, and the service grant scope', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'Inventory', kind: 'service', target: 'https://api.example.com/v1', expires_at: 9999999999, writable: true, fingerprint: ''}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'http', arguments: {method: 'POST', path: '/items', body: '{"n":1}', content_type: 'application/json'}, expires_at: 9999999999}]};
  const view = render(ChatAccess, {sessionId: 's1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
  expect(screen.getByText('Approve this API request?')).toBeTruthy();
  expect(screen.getByText('Inventory · https://api.example.com/v1')).toBeTruthy();
  const pre = view.container.querySelector('pre.pa-mono')!;
  expect(pre.textContent).toBe('POST /items\napplication/json\n\n{"n":1}');
  expect(screen.getByText(/HTTPS API · changes follow session permission mode/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', {name: 'Deny'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/s1/actions/a1', {allow: false});
 });
 it.each([[true, 'It can change files in the folder.'], [false, 'The folder is read-only to it.']])(
  'labels a folder grant command as sandboxed, not remote (writable %s)', async (writable, effect) => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/home/me/notes', expires_at: 9999999999, writable}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'grep -r todo .'}, expires_at: 9999999999}]};
  const view = render(ChatAccess, {sessionId: 's1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
  expect(screen.getByText('Approve this sandboxed command?')).toBeTruthy();
  expect(screen.queryByText('Approve this remote command?')).toBeNull();
  expect(view.container.querySelector('pre.pa-mono')!.textContent).toBe('$ grep -r todo .');
  expect(screen.getByText(`Runs once in an isolated sandbox that sees only this folder and has no network. ${effect}`)).toBeTruthy();
 });
 it('keeps request form input across recovery polls and removes expired access from the next snapshot', async () => {
  snapshot = {...empty(), requests:[{id:'r1',kind:'folder',purpose:'Read the requested file',expires_at:9999999999}]};
  render(ChatAccess,{sessionId:'s1'}); await flush();
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
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
  await fireEvent.click(screen.getByRole('button', {name: 'Private access'}));
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
 it('refreshes the saved list when another session saves or deletes a saved connection', async () => {
  const saved = {id: 'c1', label: 'Build box', target: 'ops@10.0.0.5:22', fingerprint: 'SHA256:x'};
  snapshot = {...empty(), saved_connections: [saved]};
  render(ChatAccess, {sessionId: 's2'}); await flush();
  expect(screen.getByText('Build box')).toBeTruthy();
  // A delete in session s1 invalidates s1 and the owner-wide resource.
  snapshot = empty();
  resourceSync.invalidate(['chat-access:s1', 'chat-access']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(2);
  expect(screen.queryByText('Build box')).toBeNull();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(2);
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
