import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent, within } from '@testing-library/svelte';
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
import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
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
const trigger = () => screen.getByRole('button', {name: /^Private access/});
const openAccess = () => fireEvent.click(trigger());
const panel = () => screen.queryByRole('dialog', {name: 'Private access'});
describe('private access header entry point', () => {
 it('renders nothing in the header while the chat has no access, request or saved connection', async () => {
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(screen.queryByRole('button', {name: /^Private access/})).toBeNull();
  expect(panel()).toBeNull();
 });
 it('is a quiet icon with no indicator when only saved connections exist', async () => {
  snapshot = {...empty(), saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: ''}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Private access');
  expect(trigger().textContent?.trim()).toBe('');
  expect(trigger().querySelector('.pa-count, .pa-dot')).toBeNull();
 });
 it('marks active access with a dot and names the count', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Private access: 1 active');
  expect(trigger().querySelector('.pa-dot.active')).toBeTruthy();
  expect(trigger().querySelector('.pa-count')).toBeNull();
 });
 it('counts requests and approvals waiting, ahead of active access', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'NAS', kind: 'machine', target: 'a@b:22', expires_at: 9999999999, writable: true}],
   requests: [{id: 'r1', kind: 'folder', purpose: 'Read logs', expires_at: 9999999999}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Private access: 2 waiting for you');
  expect(trigger().querySelector('.pa-count')?.textContent).toBe('2');
  expect(trigger().querySelector('.pa-dot')).toBeNull();
 });
 it('marks an unavailable snapshot with an error dot', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return {mode: 'code'};
   throw {status: 503, detail: 'Offline'};
  });
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Private access: unavailable');
  expect(trigger().querySelector('.pa-dot.error')).toBeTruthy();
  await openAccess();
  expect(screen.getByRole('alert').textContent).toBe('Offline');
 });
});

describe('private access header panel', () => {
 // #1807: a request raised while the chat is open shows IN the chat, and the
 // session is marked as needing the owner, without opening the header panel.
 it('shows a request raised in the open chat as an in-chat prompt and marks the session', async () => {
  render(ChatAccess, {sessionId: 's1'});
  render(PrivateAccessPrompt, {sessionId: 's1'});
  await flush();
  expect(screen.queryByRole('region', {name: /private access waiting/i})).toBeNull();
  snapshot = {...empty(), requests: [{id: 'r1', kind: 'machine', purpose: 'Check the container logs', expires_at: 9999999999}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  const region = screen.getByRole('region', {name: /private access waiting/i});
  expect(region.textContent).toContain('Check the container logs');
  expect(get(privateAccessPending).get('s1')).toBe(1);
  expect(panel()).toBeNull();
  // The Review click reaches the window too; it must not close what it opened.
  await fireEvent.click(screen.getByRole('button', {name: 'Review'}));
  expect(panel()).toBeTruthy();
  expect(trigger().getAttribute('aria-expanded')).toBe('true');
  snapshot = empty();
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByRole('region', {name: /private access waiting/i})).toBeNull();
  expect(get(privateAccessPending).has('s1')).toBe(false);
  // Nothing left to show: the panel and its icon go away together.
  expect(panel()).toBeNull();
  expect(get(openHeaderDropdown)).toBeNull();
 });
 it('opens as a labelled panel, moves focus into it and returns focus on close', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-expanded')).toBe('false');
  expect(trigger().getAttribute('aria-haspopup')).toBe('dialog');
  await openAccess(); await flush();
  const dialog = panel()!;
  expect(trigger().getAttribute('aria-controls')).toBe(dialog.id);
  expect(document.activeElement).toBe(dialog);
  expect(within(dialog).getByText('1 active')).toBeTruthy();
  await fireEvent.click(within(dialog).getByRole('button', {name: 'Close private access'}));
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(trigger());
 });
 it('closes on an outside click but not on clicks inside it', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke Notes'}));
  expect(panel()).toBeTruthy();
  // The Keep/Revoke swap detached the clicked button; the panel still stays.
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Keep'}));
  expect(panel()).toBeTruthy();
  await fireEvent.click(document.body);
  expect(panel()).toBeNull();
 });
 it('is one of the header panels: opening another closes it', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  await openAccess();
  expect(get(openHeaderDropdown)).toBe('private-access');
  openDropdown('git-panel'); await tick();
  expect(panel()).toBeNull();
  closeDropdown('git-panel');
 });
 it('resumes a service setup after the panel closes without keeping its token or consent', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'service',purpose:'Read inventory',expires_at:9999999999,service_provider:'cloudflare',suggested_url:'https://api.cloudflare.com/client/v4'}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await openAccess();
  await fireEvent.click(screen.getByRole('button', {name:'Continue to setup'}));
  await fireEvent.click(screen.getByRole('button', {name:'I have a token'}));
  await fireEvent.input(screen.getByLabelText('API key or token'), {target:{value:'test-only-private-token'}});
  await fireEvent.click(screen.getByRole('checkbox', {name:/I approve access/}));
  await fireEvent.click(screen.getByRole('button', {name:'Close private access'}));
  await flush();
  await openAccess();
  expect((screen.getByLabelText('API key or token') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('checkbox', {name:/I approve access/}) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByText('https://api.cloudflare.com/client/v4')).toBeTruthy();
  expect(m.post).not.toHaveBeenCalled();
 });
 it('clears a typed token when an outside click closes the panel', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'service',purpose:'Read inventory',expires_at:9999999999,service_provider:'cloudflare',suggested_url:'https://api.cloudflare.com/client/v4'}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Continue to setup'}));
  await fireEvent.click(screen.getByRole('button', {name:'I have a token'}));
  await fireEvent.input(screen.getByLabelText('API key or token'), {target:{value:'test-only-private-token'}});
  await fireEvent.click(document.body); await flush();
  expect(panel()).toBeNull();
  await openAccess();
  expect((screen.getByLabelText('API key or token') as HTMLInputElement).value).toBe('');
 });
 it('clears a typed SSH password, private key and passphrase whenever the panel closes', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'machine',purpose:'Check the logs',expires_at:9999999999}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.input(screen.getByLabelText('Server IP address'), {target:{value:'10.0.0.2'}});
  await fireEvent.input(screen.getByLabelText('Password'), {target:{value:'test-only-password'}});
  await fireEvent.click(document.body); await flush();
  expect(panel()).toBeNull();
  await openAccess(); await flush();
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  // Non-secret connection details stay so the owner can carry on.
  expect((screen.getByLabelText('Server IP address') as HTMLInputElement).value).toBe('10.0.0.2');
  await fireEvent.click(screen.getByRole('button', {name:/Use a private key instead/}));
  await fireEvent.input(screen.getByLabelText('Private key'), {target:{value:'test-only-key'}});
  await fireEvent.input(screen.getByLabelText('Key passphrase'), {target:{value:'test-only-passphrase'}});
  await fireEvent.keyDown(document.body, {key:'Escape'}); await flush();
  await openAccess(); await flush();
  expect((screen.getByLabelText('Private key') as HTMLTextAreaElement).value).toBe('');
  expect((screen.getByLabelText('Key passphrase') as HTMLInputElement).value).toBe('');
  expect(m.post).not.toHaveBeenCalled();
 });
 it('keeps the panel open when the chat prompt’s Review is pressed while it is already open', async () => {
  snapshot = {...empty(), requests: [{id:'r1',kind:'folder',purpose:'Read logs',expires_at:9999999999}]};
  render(ChatAccess, {sessionId:'s1'});
  render(PrivateAccessPrompt, {sessionId:'s1'});
  await flush();
  await openAccess(); await flush();
  expect(panel()).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', {name:'Review'}));
  expect(panel()).toBeTruthy();
 });
 it('shifts the anchored panel back inside a narrow desktop window', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  render(ChatAccess, {sessionId: 's1'}); await flush();
  const anchor = trigger().parentElement!;
  let right = 300;
  vi.spyOn(anchor, 'getBoundingClientRect').mockImplementation(() => ({right} as DOMRect));
  const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(420);
  try {
   await openAccess(); await flush();
   // Right-aligned under the icon it would start at 300 - 420 = -120.
   expect(panel()!.style.right).toBe('-132px');
   right = 900;
   window.dispatchEvent(new Event('resize')); await tick();
   expect(panel()!.style.right).toBe('');
  } finally { width.mockRestore(); }
 });
 it('keeps Escape inside the panel instead of triggering global chat shortcuts', async () => {
  snapshot = {...empty(), actions:[{id:'a1',grant_id:'g1',operation:'command',arguments:{command:'pwd'},expires_at:9999999999}]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await openAccess();
  const shortcut = vi.fn();
  window.addEventListener('keydown', shortcut);
  try {
   expect(panel()).toBeTruthy();
   // A completed request removes its focused button; Escape then targets body.
   await fireEvent.keyDown(document.body, {key:'Escape'});
   expect(panel()).toBeNull();
   expect(shortcut).not.toHaveBeenCalled();
   expect(document.activeElement).toBe(trigger());
   // Closed, Escape belongs to the chat again.
   await fireEvent.keyDown(document.body, {key:'Escape'});
   expect(shortcut).toHaveBeenCalledTimes(1);
  } finally { window.removeEventListener('keydown', shortcut); }
 });
 it('never opens or closes itself on streaming invalidations and fresh snapshots', async () => {
  const grant = {id:'g1',label:'Granted folder',kind:'folder',target:'/safe',expires_at:9999999999,writable:true};
  snapshot = {...empty(), grants:[grant]};
  const view = render(ChatAccess, {sessionId:'s1'}); await flush();
  expect(panel()).toBeNull();
  await openAccess();
  for (let i = 0; i < 5; i++) {
   snapshot = {...empty(), grants:[{...grant}]};
   resourceSync.invalidate(['sessions', 'chat-access:s1']);
   await vi.advanceTimersByTimeAsync(100); await tick();
   await view.rerender({sessionId:'s1'});
   expect(panel()).toBeTruthy();
  }
  await fireEvent.click(screen.getByRole('button', {name:'Close private access'}));
  snapshot = {...empty(), grants:[{...grant}], requests:[{id:'r1',kind:'folder',purpose:'Another request',expires_at:9999999999}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(panel()).toBeNull();
  expect(trigger().getAttribute('aria-label')).toBe('Private access: 1 waiting for you');
  await vi.advanceTimersByTimeAsync(6000); await tick();
  expect(panel()).toBeNull();
  await openAccess();
  expect(within(panel()!).getByText('Waiting for you')).toBeTruthy();
  expect(screen.getByRole('textbox', {name:'Resource name'})).toBeTruthy();
 });
 it('lists saved connections with an empty state when nothing is active, across refreshes', async () => {
  const saved = {id:'saved1',label:'Saved server',target:'server',fingerprint:''};
  snapshot = {...empty(), saved_connections:[saved]};
  render(ChatAccess, {sessionId:'s1'}); await flush();
  await openAccess();
  const dialog = panel()!;
  expect(within(dialog).getByText(/Nothing is shared with this chat right now/)).toBeTruthy();
  expect(within(dialog).getByRole('list', {name: 'Saved connections'}).textContent).toContain('Saved server');
  snapshot = {...empty(), saved_connections:[{...saved}]};
  resourceSync.invalidate(['chat-access:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(panel()).toBeTruthy();
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
  await fireEvent.click(trigger());
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
  await fireEvent.click(trigger());
  await fireEvent.click(screen.getByRole('button', {name: 'Approve once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/s1/actions/a1', {allow: true});
  expect(screen.queryByText('Approve this remote command?')).toBeNull();
 });
 it('shows an API request approval with its method, path and body, and the service grant scope', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'Inventory', kind: 'service', target: 'https://api.example.com/v1', expires_at: 9999999999, writable: true, fingerprint: ''}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'http', arguments: {method: 'POST', path: '/items', body: '{"n":1}', content_type: 'application/json'}, expires_at: 9999999999}]};
  const view = render(ChatAccess, {sessionId: 's1'}); await flush();
  await fireEvent.click(trigger());
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
  await fireEvent.click(trigger());
  expect(screen.getByText('Approve this sandboxed command?')).toBeTruthy();
  expect(screen.queryByText('Approve this remote command?')).toBeNull();
  expect(view.container.querySelector('pre.pa-mono')!.textContent).toBe('$ grep -r todo .');
  expect(screen.getByText(`Runs once in an isolated sandbox that sees only this folder and has no network. ${effect}`)).toBeTruthy();
 });
 it('keeps request form input across recovery polls and removes expired access from the next snapshot', async () => {
  snapshot = {...empty(), requests:[{id:'r1',kind:'folder',purpose:'Read the requested file',expires_at:9999999999}]};
  render(ChatAccess,{sessionId:'s1'}); await flush();
  await fireEvent.click(trigger());
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
  await fireEvent.click(trigger());
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

describe('a subagent’s private access in its main chat', () => {
 const fromChild = {session_id: 'child', subagent: 'Review PR #1837'};
 it('labels who asks and sends every answer to the subagent’s session', async () => {
  snapshot = {...empty(),
   requests: [{id: 'r1', kind: 'machine', purpose: 'Check the CI runner', expires_at: 9999999999, ...fromChild}],
   grants: [{id: 'g1', label: 'Runner', kind: 'machine', target: 'ci@10.0.0.9:22', expires_at: 9999999999, writable: true, ...fromChild}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'uptime'}, expires_at: 9999999999, ...fromChild}]};
  render(ChatAccess, {sessionId: 'main'});
  render(PrivateAccessPrompt, {sessionId: 'main'});
  await flush();
  // Above the composer: the subagent is named on each waiting item.
  const region = screen.getByRole('region', {name: /private access waiting/i});
  expect(region.textContent).toContain('A subagent waits until you answer.');
  expect(within(region).getAllByText('Review PR #1837')).toHaveLength(2);
  expect(get(privateAccessPending).get('main')).toBe(2);
  await openAccess(); await flush();
  const dialog = panel()!;
  expect(within(dialog).getAllByText((_, el) => el?.classList.contains('pa-from') ?? false)).toHaveLength(2);
  expect(dialog.textContent).toContain('Asked by subagent Review PR #1837');
  expect(dialog.textContent).toContain('Subagent Review PR #1837 · Remote commands');
  await fireEvent.click(within(dialog).getByRole('button', {name: 'Approve once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/child/actions/a1', {allow: true});
  const ensureOpen = async () => { if (!panel() || panel()!.hidden) { await openAccess(); await flush(); } };
  await ensureOpen();
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Decline'})); await flush();
  expect(m.delete).toHaveBeenCalledWith('/chat-access/child/requests/r1');
  await ensureOpen();
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke Runner'}));
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke'})); await flush();
  expect(m.delete).toHaveBeenCalledWith('/chat-access/child/grants/g1');
  // Nothing was sent to the main chat's own endpoints.
  expect([...m.post.mock.calls, ...m.delete.mock.calls].some(([url]) => String(url).startsWith('/chat-access/main/'))).toBe(false);
 });
 it('never fetches a subagent’s own snapshot, even when mounted before /live marks the chat', async () => {
  const { subagentChats } = await import('$lib/stores/subagent-chats');
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? { mode: 'code', is_worker: true }
   : {...empty(), requests: [{id: 'r1', kind: 'machine', purpose: 'Hidden here', expires_at: 9999999999}]});
  render(ChatAccess, {sessionId: 'child'});
  render(PrivateAccessPrompt, {sessionId: 'child'});
  await flush();
  expect(accessCalls()).toEqual([]);
  expect(screen.queryByRole('button', {name: /^Private access/})).toBeNull();
  expect(screen.queryByRole('region', {name: /private access waiting/i})).toBeNull();
  expect(get(subagentChats).has('child')).toBe(true);
  subagentChats.mark('child', false);
 });
 it('keeps the main chat’s own items unlabelled and addressed to itself', async () => {
  snapshot = {...empty(), actions: [{id: 'a2', grant_id: 'g2', operation: 'command', arguments: {command: 'ls'}, expires_at: 9999999999}],
   grants: [{id: 'g2', label: 'NAS', kind: 'machine', target: 'a@b:22', expires_at: 9999999999, writable: true}]};
  render(ChatAccess, {sessionId: 'main'});
  render(PrivateAccessPrompt, {sessionId: 'main'});
  await flush();
  expect(screen.getByRole('region', {name: /private access waiting/i}).textContent).toContain('The agent waits until you answer.');
  await openAccess(); await flush();
  expect(panel()!.querySelector('.pa-from')).toBeNull();
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Approve once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/chat-access/main/actions/a2', {allow: true});
 });
});
