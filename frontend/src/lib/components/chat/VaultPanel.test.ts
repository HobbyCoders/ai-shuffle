import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent, within } from '@testing-library/svelte';
import { tick } from 'svelte';
const m = vi.hoisted(() => {
 const api = { get: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn(),
  // The session check reads /live metadata; it answers with the row the test set up.
  getShared: vi.fn(async (url: string) => ({ session_meta: { is_remote: false, device_name: null, ...(await api.get(url)) } })) };
 return api;
});
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
const settingsNav = vi.hoisted(() => ({openSettings: vi.fn()}));
vi.mock('$lib/settings/navigation.svelte', () => settingsNav);
import VaultPanel from './VaultPanel.svelte';
import VaultChip from './VaultChip.svelte';
import { get } from 'svelte/store';
import { vaultPending, vaultSummary } from '$lib/stores/vault';
import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
import { resourceSync } from '$lib/services/resource-sync';
const empty = () => ({ requests: [], actions: [], grants: [], saved_connections: [], vault_available: false });
let snapshot: ReturnType<typeof empty> | Record<string, unknown>;
const accessCalls = () => m.get.mock.calls.filter(([url]) => url.startsWith('/vault/sessions/'));
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
 m.patch.mockResolvedValue({updated: true, revoked: 0, host_key_cleared: false});
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
// The panel and the composer's chip that opens it, as ChatView mounts them.
const mount = (props: {sessionId: string}) => { render(VaultChip); return render(VaultPanel, props); };
const trigger = () => screen.getByRole('button', {name: /^Vault/});
const openAccess = () => fireEvent.click(trigger());
const panel = () => screen.queryByRole('dialog', {name: 'Vault'});
/** The waiting requests and approvals, inline above the composer. */
const lane = () => screen.queryByRole('group', {name: 'Vault: waiting for you'});
describe('Vault chip in the composer row', () => {
 it('renders nothing while the chat has no access, request or saved sign-in', async () => {
  mount({sessionId: 's1'}); await flush();
  expect(screen.queryByRole('button', {name: /^Vault/})).toBeNull();
  expect(panel()).toBeNull();
 });
 it('is a quiet icon with no indicator when only saved sign-ins exist', async () => {
  snapshot = {...empty(), saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: ''}]};
  mount({sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Vault');
  expect(trigger().textContent?.trim()).toBe('');
  expect(trigger().querySelector('.pa-count, .pa-dot')).toBeNull();
 });
 it('marks active access with a dot and names the count', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Vault: 1 active');
  expect(trigger().querySelector('.pa-dot.active')).toBeTruthy();
  expect(trigger().querySelector('.pa-count')).toBeNull();
 });
 it('counts requests and approvals waiting, ahead of active access', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'NAS', kind: 'machine', target: 'a@b:22', expires_at: 9999999999, writable: true}],
   requests: [{id: 'r1', kind: 'folder', purpose: 'Read logs', expires_at: 9999999999}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999}]};
  mount({sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Vault: 2 waiting for you');
  expect(trigger().querySelector('.pa-count')?.textContent).toBe('2');
  expect(trigger().querySelector('.pa-dot')).toBeNull();
 });
 it('marks an unavailable snapshot with an error dot', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return {mode: 'code'};
   throw {status: 503, detail: 'Offline'};
  });
  mount({sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-label')).toBe('Vault: unavailable');
  expect(trigger().querySelector('.pa-dot.error')).toBeTruthy();
  await openAccess();
  expect(screen.getByRole('alert').textContent).toBe('Offline');
 });
});

describe('Vault panel above the composer', () => {
 // #1807: a request raised while the chat is open shows IN the chat, and the
 // session is marked as needing the owner, without opening any panel.
 it('shows a request raised in the open chat above the composer and marks the session', async () => {
  mount({sessionId: 's1'});
  await flush();
  expect(lane()).toBeNull();
  snapshot = {...empty(), requests: [{id: 'r1', kind: 'machine', purpose: 'Check the container logs', expires_at: 9999999999}]};
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  // Answered in place: the form is there without opening anything.
  expect(lane()!.textContent).toContain('Check the container logs');
  expect(within(lane()!).getByLabelText('Address')).toBeTruthy();
  expect(get(vaultPending).get('s1')).toBe(1);
  expect(panel()).toBeNull();
  // The chip's click reaches the window too; it must not close what it opened.
  await openAccess();
  expect(panel()).toBeTruthy();
  expect(trigger().getAttribute('aria-expanded')).toBe('true');
  // The panel lists access; the request stays in the lane, not copied into it.
  expect(within(panel()!).queryByLabelText('Address')).toBeNull();
  snapshot = empty();
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(lane()).toBeNull();
  expect(get(vaultPending).has('s1')).toBe(false);
  // Nothing left to show: the panel and its chip go away together.
  expect(panel()).toBeNull();
  expect(screen.queryByRole('button', {name: /^Vault/})).toBeNull();
  expect(get(openHeaderDropdown)).toBeNull();
 });
 it('opens as a labelled panel, moves focus into it and returns focus on close', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  expect(trigger().getAttribute('aria-expanded')).toBe('false');
  expect(trigger().getAttribute('aria-haspopup')).toBe('dialog');
  await openAccess(); await flush();
  const dialog = panel()!;
  expect(trigger().getAttribute('aria-controls')).toBe(dialog.id);
  expect(document.activeElement).toBe(dialog);
  expect(within(dialog).getByText('1 active')).toBeTruthy();
  await fireEvent.click(within(dialog).getByRole('button', {name: 'Close Vault'}));
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(trigger());
 });
 it('on a phone, with no chip, returns focus to the context line\'s Controls item on Escape', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  const controls = document.createElement('button');
  controls.dataset.controlsTrigger = '';
  document.body.append(controls);
  render(VaultPanel, {sessionId: 's1'}); await flush();
  // Chat controls' Vault action closes the sheet and opens the panel.
  get(vaultSummary)!.open(); await flush();
  expect(document.activeElement).toBe(panel());
  await fireEvent.keyDown(document.body, {key: 'Escape'}); await flush();
  expect(panel()).toBeNull();
  expect(document.activeElement).toBe(controls);
  controls.remove();
 });
 it('closes on an outside click but not on clicks inside it', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke Notes'}));
  expect(panel()).toBeTruthy();
  // The Keep/Revoke swap detached the clicked button; the panel still stays.
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Keep'}));
  expect(panel()).toBeTruthy();
  await fireEvent.click(document.body);
  expect(panel()).toBeNull();
 });
 it('is one panel at a time: opening a header panel closes it', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  await openAccess();
  expect(get(openHeaderDropdown)).toBe('vault');
  openDropdown('git-panel'); await tick();
  expect(panel()).toBeNull();
  closeDropdown('git-panel');
 });
 it('keeps what is typed into an inline request while the panel opens and closes', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'machine',purpose:'Check the logs',expires_at:9999999999}]};
  mount({sessionId:'s1'}); await flush();
  await fireEvent.input(screen.getByLabelText('Address'), {target:{value:'10.0.0.2'}});
  await fireEvent.input(screen.getByLabelText('Password'), {target:{value:'test-only-password'}});
  await openAccess(); await flush();
  await fireEvent.click(within(panel()!).getByRole('button', {name:'Close Vault'})); await flush();
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('test-only-password');
  expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('10.0.0.2');
  // The request ending takes the form, and what was typed, with it.
  snapshot = empty();
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByLabelText('Password')).toBeNull();
  expect(m.post).not.toHaveBeenCalled();
 });
 it('opens upward from the composer with no inline offset to keep in sync', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/notes', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  await openAccess(); await flush();
  expect(panel()!.style.right).toBe('');
  window.dispatchEvent(new Event('resize')); await tick();
  expect(panel()!.style.right).toBe('');
 });
 it('keeps Escape inside the panel instead of triggering global chat shortcuts', async () => {
  snapshot = {...empty(), actions:[{id:'a1',grant_id:'g1',operation:'command',arguments:{command:'pwd'},expires_at:9999999999}]};
  mount({sessionId:'s1'}); await flush();
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
  const view = mount({sessionId:'s1'}); await flush();
  expect(panel()).toBeNull();
  await openAccess();
  for (let i = 0; i < 5; i++) {
   snapshot = {...empty(), grants:[{...grant}]};
   resourceSync.invalidate(['sessions', 'vault:s1']);
   await vi.advanceTimersByTimeAsync(100); await tick();
   await view.rerender({sessionId:'s1'});
   expect(panel()).toBeTruthy();
  }
  await fireEvent.click(screen.getByRole('button', {name:'Close Vault'}));
  snapshot = {...empty(), grants:[{...grant}], requests:[{id:'r1',kind:'folder',purpose:'Another request',expires_at:9999999999}]};
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(panel()).toBeNull();
  expect(trigger().getAttribute('aria-label')).toBe('Vault: 1 waiting for you');
  await vi.advanceTimersByTimeAsync(6000); await tick();
  expect(panel()).toBeNull();
  // The request is in the lane above the composer, panel closed or not.
  expect(within(lane()!).getByRole('textbox', {name:'Name'})).toBeTruthy();
 });
 it('points a local chat to Settings for its saved sign-ins, with an empty state when nothing is active', async () => {
  const saved = {id:'saved1',label:'Saved server',target:'server',fingerprint:''};
  snapshot = {...empty(), saved_connections:[saved]};
  mount({sessionId:'s1'}); await flush();
  await openAccess();
  const dialog = panel()!;
  expect(within(dialog).getByText(/Nothing is shared with this chat right now/)).toBeTruthy();
  // Saved sign-ins of this worker are not listed or edited in a chat.
  expect(within(dialog).queryByRole('list', {name: 'Saved sign-ins'})).toBeNull();
  expect(dialog.textContent).not.toContain('Saved server');
  const manage = within(dialog).getByRole('button', {name: /Manage saved sign-ins in Settings › Vault/});
  expect(manage.textContent).toContain('1 saved');
  snapshot = {...empty(), saved_connections:[{...saved}]};
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(panel()).toBeTruthy();
  await fireEvent.click(manage);
  expect(panel()).toBeNull();
  expect(settingsNav.openSettings).toHaveBeenCalledWith({category: 'vault'});
 });
});

describe('the phone shell, where requests open in the sheet', () => {
 beforeEach(() => { vi.stubEnv('VITE_SHUFFLE_SHELL', 'mobile'); });
 afterEach(() => { vi.unstubAllEnvs(); });
 it('shows nothing inline: the prompt dock asks and the sheet holds the form', async () => {
  snapshot = {...empty(), requests: [{id:'r1',kind:'folder',purpose:'Read logs',expires_at:9999999999}]};
  mount({sessionId:'s1'}); await flush();
  expect(lane()).toBeNull();
  expect(panel()).toBeNull();
  get(vaultSummary)!.open(); await flush();
  expect(within(panel()!).getByText('Waiting for you')).toBeTruthy();
  expect(within(panel()!).getByRole('textbox', {name:'Name'})).toBeTruthy();
  // Asked to open while already open (the dock's Review), it stays open.
  get(vaultSummary)!.open(); await flush();
  expect(panel()).toBeTruthy();
 });
 it('resumes a service setup after the sheet closes without keeping its token or consent', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'service',purpose:'Read inventory',expires_at:9999999999,service_provider:'cloudflare',suggested_url:'https://api.cloudflare.com/client/v4'}]};
  mount({sessionId:'s1'}); await flush();
  await openAccess();
  await fireEvent.click(screen.getByRole('button', {name:'Continue to setup'}));
  await fireEvent.click(screen.getByRole('button', {name:'I have a token'}));
  await fireEvent.input(screen.getByLabelText('API key or token'), {target:{value:'test-only-private-token'}});
  await fireEvent.click(screen.getByRole('checkbox', {name:/I approve access/}));
  await fireEvent.click(screen.getByRole('button', {name:'Close Vault'}));
  await flush();
  await openAccess();
  expect((screen.getByLabelText('API key or token') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('checkbox', {name:/I approve access/}) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByText('https://api.cloudflare.com/client/v4')).toBeTruthy();
  expect(m.post).not.toHaveBeenCalled();
 });
 it('clears a typed token when an outside tap closes the sheet', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'service',purpose:'Read inventory',expires_at:9999999999,service_provider:'cloudflare',suggested_url:'https://api.cloudflare.com/client/v4'}]};
  mount({sessionId:'s1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.click(screen.getByRole('button', {name:'Continue to setup'}));
  await fireEvent.click(screen.getByRole('button', {name:'I have a token'}));
  await fireEvent.input(screen.getByLabelText('API key or token'), {target:{value:'test-only-private-token'}});
  await fireEvent.click(document.body); await flush();
  expect(panel()).toBeNull();
  await openAccess();
  expect((screen.getByLabelText('API key or token') as HTMLInputElement).value).toBe('');
 });
 it('clears a typed SSH password, private key and passphrase whenever the sheet closes', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), requests: [{id:'r1',kind:'machine',purpose:'Check the logs',expires_at:9999999999}]};
  mount({sessionId:'s1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.input(screen.getByLabelText('Address'), {target:{value:'10.0.0.2'}});
  await fireEvent.input(screen.getByLabelText('Password'), {target:{value:'test-only-password'}});
  await fireEvent.click(document.body); await flush();
  expect(panel()).toBeNull();
  await openAccess(); await flush();
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  // Non-secret connection details stay so the owner can carry on.
  expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('10.0.0.2');
  await fireEvent.click(screen.getByRole('button', {name:'Use a private key or check the host key'}));
  await fireEvent.click(screen.getByRole('checkbox', {name:/Sign in with a private key/}));
  await fireEvent.input(screen.getByLabelText('Private key'), {target:{value:'test-only-key'}});
  await fireEvent.input(screen.getByLabelText('Key passphrase'), {target:{value:'test-only-passphrase'}});
  await fireEvent.keyDown(document.body, {key:'Escape'}); await flush();
  await openAccess(); await flush();
  expect((screen.getByLabelText('Private key') as HTMLTextAreaElement).value).toBe('');
  expect((screen.getByLabelText('Key passphrase') as HTMLInputElement).value).toBe('');
  expect(m.post).not.toHaveBeenCalled();
 });
});

describe('vault recovery and live invalidations', () => {
 it('does not poll owner-only access or render errors in a session preview', async () => {
  accessAuth.current.status.preview_access = true;
  mount({sessionId: 'preview'}); await flush();
  resourceSync.invalidate(['sessions', 'vault:preview']);
  await vi.advanceTimersByTimeAsync(600_000); hidden(true); hidden(false); await flush();
  expect(m.get).not.toHaveBeenCalled();
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it.each(['code', 'chat', 'voice', null])('shows a remote %s session\'s owner-side requests through this worker', async (mode) => {
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode, is_remote: true, device_name: 'Owner PC'} : snapshot);
  snapshot = {...empty(), vault_available: true,
   requests: [{id: 'r1', kind: 'folder', purpose: 'Read the notes', expires_at: 9999999999}],
   saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: '', kind: 'machine'}]};
  mount({sessionId: 'remote'}); await flush();
  expect(accessCalls().map(([url]) => url)).toEqual(['/vault/sessions/remote']);
  await fireEvent.click(trigger());
  expect(screen.getByText('Folder on Owner PC')).toBeTruthy();
  expect(screen.getByText(/Encrypted on Owner PC/)).toBeTruthy();
  // The owner's change notice arrives as a scoped invalidation on this worker.
  resourceSync.invalidate(['vault:remote']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
 });
 it('checks the session through its /live metadata, never the whole transcript', async () => {
  mount({sessionId: 's-slim'}); await flush();
  expect(m.getShared).toHaveBeenCalledWith('/sessions/s-slim/live?include_messages=false&owner_fields=true');
  expect(m.get.mock.calls.map(([url]) => url)).not.toContain('/sessions/s-slim');
  expect(accessCalls()).toHaveLength(1);
 });
 it('reads the whole session with VITE_PERF_SLIM_SESSION_READ=0', async () => {
  vi.stubEnv('VITE_PERF_SLIM_SESSION_READ', '0');
  try {
   mount({sessionId: 's-full'}); await flush();
   expect(m.getShared).not.toHaveBeenCalled();
   expect(m.get.mock.calls.map(([url]) => url)).toContain('/sessions/s-full');
  } finally { vi.unstubAllEnvs(); }
 });
 it('rechecks where access lives when a session moves between devices', async () => {
  let remote = true;
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode: 'code', is_remote: remote, device_name: remote ? 'Owner PC' : null} : snapshot);
  snapshot = {...empty(), vault_available: true, saved_connections: [{id: 'c1', label: 'NAS', target: 'root@10.0.0.2:22', fingerprint: ''}]};
  mount({sessionId: 's1'}); await flush();
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
  mount({ sessionId: 's1' }); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
  snapshot = { ...empty(), actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999 }] };
  resourceSync.invalidate(['vault:other']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(2);
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  // The agent has no grant row here, so the card names the resource generically.
  expect(within(lane()!).getByText('Run this on this resource?')).toBeTruthy();
  expect(accessCalls()).toHaveLength(3);
  expect(sessionCalls()).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(2000);
  expect(accessCalls()).toHaveLength(4);
  snapshot = empty();
  // Answered in the lane: no panel to open first.
  await fireEvent.click(screen.getByRole('button', {name: 'Run once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/vault/sessions/s1/actions/a1', {allow: true});
  expect(lane()).toBeNull();
 });
 it('shows an API request approval with its method, path and body, and the service grant scope', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'Inventory', kind: 'service', target: 'https://api.example.com/v1', expires_at: 9999999999, writable: true, fingerprint: ''}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'http', arguments: {method: 'POST', path: '/items', body: '{"n":1}', content_type: 'application/json'}, expires_at: 9999999999}]};
  mount({sessionId: 's1'}); await flush();
  const card = within(lane()!).getByRole('region', {name: 'One-time approval'});
  expect(within(card).getByText('Send this to Inventory?')).toBeTruthy();
  expect(within(card).getByText('Once, to https://api.example.com/v1')).toBeTruthy();
  expect(within(card).getByRole('button', {name: 'Send once'})).toBeTruthy();
  const pre = card.querySelector('pre.pa-mono')!;
  expect(pre.textContent).toBe('POST /items\napplication/json\n\n{"n":1}');
  expect(within(card).getByText(/sent with the sign-in attached; the agent never sees the token or password/)).toBeTruthy();
  await fireEvent.click(trigger());
  expect(within(panel()!).getByText(/HTTPS API · changes follow session permission mode/)).toBeTruthy();
  await fireEvent.click(within(card).getByRole('button', {name: 'Deny'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/vault/sessions/s1/actions/a1', {allow: false});
 });
 it('describes commands as remote SSH execution', async () => {
  snapshot = {...empty(),
   grants: [{id: 'g1', label: 'Server', kind: 'machine', target: 'server', expires_at: 9999999999, writable: true}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'pwd'}, expires_at: 9999999999}]};
  mount({sessionId: 's1'}); await flush();
  const card = within(lane()!).getByRole('region', {name: 'One-time approval'});
  expect(within(card).getByText('Run this on Server?')).toBeTruthy();
  expect(within(card).getByText('Once, as server')).toBeTruthy();
  expect(card.querySelector('pre.pa-mono')!.textContent).toBe('$ pwd');
  expect(within(card).getByText('It runs with that account’s authority and can change files on that machine. The next command asks again.')).toBeTruthy();
 });
 it.each([true, false])('keeps folder file access (writable %s)', async (writable) => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Notes', kind: 'folder', target: '/home/me/notes', expires_at: 9999999999, writable}]};
  mount({sessionId: 's1'}); await flush();
  await fireEvent.click(trigger());
  expect(screen.getByText('Notes')).toBeTruthy();
  expect(screen.getByText(writable ? /Read\/write · session permission mode applies/ : /Read only/)).toBeTruthy();
  expect(lane()).toBeNull();
 });
 it('keeps request form input across recovery polls and removes expired access from the next snapshot', async () => {
  snapshot = {...empty(), requests:[{id:'r1',kind:'folder',purpose:'Read the requested file',expires_at:9999999999}]};
  mount({sessionId:'s1'}); await flush();
  const label = screen.getByRole('textbox',{name:'Name'}) as HTMLInputElement;
  await fireEvent.input(label,{target:{value:'My folder'}});
  await vi.advanceTimersByTimeAsync(2000); await tick();
  expect(accessCalls()).toHaveLength(2);
  expect((screen.getByRole('textbox',{name:'Name'}) as HTMLInputElement).value).toBe('My folder');
  snapshot = {...empty(),grants:[{id:'g1',label:'My folder',kind:'folder',target:'/safe',expires_at:9999999999,writable:false}]};
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByRole('textbox',{name:'Name'})).toBeNull();
  await fireEvent.click(trigger());
  expect(screen.getByText('My folder')).toBeTruthy();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(3);
  // The broker announces grant expiry; no grant polling is needed.
  snapshot = empty();
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(screen.queryByText('My folder')).toBeNull();
 });
 it('does not fetch private state for an unsupported persisted mode', async () => {
  m.get.mockResolvedValue({mode:'unsupported'});
  render(VaultPanel,{sessionId:'s1'}); await flush();
  await vi.advanceTimersByTimeAsync(300_000);
  expect(m.get).toHaveBeenCalledTimes(1); expect(accessCalls()).toHaveLength(0);
 });
 it('stops on persistent denial and retries on reconnect without bypassing authorization', async () => {
  m.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/')) return { mode: 'chat' };
   throw { status: 403, detail: 'Denied' };
  });
  mount({sessionId: 's1'}); await flush();
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
  mount({sessionId: 's1'}); await flush();
  for (let i = 0; i < 30; i++) {
   resourceSync.invalidate(['sessions', 'vault:s1']);
   await vi.advanceTimersByTimeAsync(2000);
  }
  expect(accessCalls()).toHaveLength(1);
 });
 it('does not immediately retry a denial dirtied while in flight or revive after unmount', async () => {
  let fail!: (value: unknown) => void;
  m.get.mockImplementation((url: string) => url.startsWith('/sessions/')
   ? Promise.resolve({mode: 'code'}) : new Promise((_resolve, reject) => {fail = reject;}));
  const view = mount({sessionId: 's1'}); await flush();
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100);
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
  mount({sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(5000); expect(accessCalls()).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(9999); expect(accessCalls()).toHaveLength(2);
  m.get.mockResolvedValue(empty());
  await vi.advanceTimersByTimeAsync(1); await tick();
  expect(accessCalls()).toHaveLength(3); expect(screen.queryByRole('alert')).toBeNull();
 });
 it('does no hidden polling and catches up immediately on return or online', async () => {
  hidden(true); const view = mount({sessionId: 's1'}); await flush();
  expect(m.get).not.toHaveBeenCalled();
  hidden(false); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(1);
  hidden(true); resourceSync.invalidate(['vault:s1']);
  await vi.advanceTimersByTimeAsync(300_000); expect(accessCalls()).toHaveLength(1);
  hidden(false); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(2);
  window.dispatchEvent(new Event('online')); await vi.advanceTimersByTimeAsync(100); expect(accessCalls()).toHaveLength(3);
  view.unmount(); await vi.advanceTimersByTimeAsync(300_000); expect(accessCalls()).toHaveLength(3);
 });
 it('refreshes what it can offer when another session saves or removes a sign-in', async () => {
  const saved = {id: 'c1', label: 'Build box', target: 'ops@10.0.0.5:22', fingerprint: 'SHA256:x'};
  snapshot = {...empty(), saved_connections: [saved]};
  mount({sessionId: 's2'}); await flush();
  expect(get(vaultSummary)!.saved).toBe(1);
  expect(trigger()).toBeTruthy();
  // A delete in session s1 invalidates s1 and the owner-wide resource.
  snapshot = empty();
  resourceSync.invalidate(['vault:s1', 'vault']); await vi.advanceTimersByTimeAsync(100); await tick();
  expect(accessCalls()).toHaveLength(2);
  // Nothing saved and nothing active: the chip goes too.
  expect(screen.queryByRole('button', {name: /^Vault/})).toBeNull();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(2);
 });
 it('keeps one follow-up when an access change arrives during a request', async () => {
  let finish!: (value: unknown) => void;
  m.get.mockImplementation((url: string) => url.startsWith('/sessions/') ? Promise.resolve({mode: 'code'}) : new Promise(resolve => {finish = resolve;}));
  mount({sessionId: 's1'}); await flush();
  resourceSync.invalidate(['vault:s1']); await vi.advanceTimersByTimeAsync(100);
  expect(accessCalls()).toHaveLength(1);
  m.get.mockResolvedValue(empty()); finish(empty()); await flush();
  expect(accessCalls()).toHaveLength(2);
 });
});

// Issue #1281: request counts over simulated time. Previously an empty snapshot
// polled every 30 s, any grant/request/action every 2 s, and a denial every 300 s.
describe('vault request rate', () => {
 const deny = () => m.get.mockImplementation(async (url: string) => {
  if (url.startsWith('/sessions/')) return {mode: 'code'};
  throw {status: 403, detail: 'Denied'};
 });
 const pending = () => ({...empty(), requests: [{id: 'r1', kind: 'folder', purpose: 'Read a file', expires_at: 9999999999}]});
 it('empty: one request on mount, none over the next ten minutes', async () => {
  mount({sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
  expect(sessionCalls()).toHaveLength(1);
 });
 it('grant only: no polling while access is active', async () => {
  snapshot = {...empty(), grants: [{id: 'g1', label: 'Folder', kind: 'folder', target: '/x', expires_at: 9999999999, writable: false}]};
  mount({sessionId: 's1'}); await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  expect(accessCalls()).toHaveLength(1);
 });
 it('denied (403): stops despite a two-second event stream until the sign-in changes', async () => {
  deny();
  mount({sessionId: 's1'}); await flush();
  for (let i = 0; i < 300; i++) {
   resourceSync.invalidate(['sessions', 'vault', 'vault:s1']);
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
  mount({sessionId: 's1'}); await flush();
  const at = (ms: number) => vi.advanceTimersByTimeAsync(ms).then(() => accessCalls().length);
  // Mount, then +2, +4, +8, +16, +30 s safety polls.
  expect(await at(60_000)).toBe(6);
  // Steady state: two per minute.
  expect(await at(60_000)).toBe(8);
  resourceSync.invalidate(['vault:s1']);
  expect(await at(100)).toBe(9);
  expect(await at(2000)).toBe(10);
  snapshot = empty();
  expect(await at(4000)).toBe(11);
  expect(await at(600_000)).toBe(11);
 });
 it('reconnect: a full invalidation checks the session and snapshot once each', async () => {
  mount({sessionId: 's1'}); await flush();
  expect([sessionCalls().length, accessCalls().length]).toEqual([1, 1]);
  // live-sync invalidates every resource on sync_ready and on return to the tab.
  resourceSync.invalidate(); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([2, 2]);
  resourceSync.invalidate(); hidden(true); hidden(false); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([3, 3]);
 });
 it('hidden tab: no requests while hidden, even for pending approvals or events', async () => {
  snapshot = pending();
  mount({sessionId: 's1'}); await flush();
  hidden(true);
  for (let i = 0; i < 60; i++) {
   resourceSync.invalidate(['sessions', 'vault:s1']);
   await vi.advanceTimersByTimeAsync(10_000);
  }
  expect(accessCalls()).toHaveLength(1);
  hidden(false); await vi.advanceTimersByTimeAsync(100);
  expect([sessionCalls().length, accessCalls().length]).toEqual([2, 2]);
 });
});

describe('a subagent’s Vault in its main chat', () => {
 const fromChild = {session_id: 'child', subagent: 'Review PR #1837'};
 it('labels who asks and sends every answer to the subagent’s session', async () => {
  snapshot = {...empty(),
   requests: [{id: 'r1', kind: 'machine', purpose: 'Check the CI runner', expires_at: 9999999999, ...fromChild}],
   grants: [{id: 'g1', label: 'Runner', kind: 'machine', target: 'ci@10.0.0.9:22', expires_at: 9999999999, writable: true, ...fromChild}],
   actions: [{id: 'a1', grant_id: 'g1', operation: 'command', arguments: {command: 'uptime'}, expires_at: 9999999999, ...fromChild}]};
  mount({sessionId: 'main'});
  await flush();
  // Above the composer: the subagent is named on each waiting item.
  const waiting = lane()!;
  expect(within(waiting).getAllByText((_, el) => el?.classList.contains('pa-from') ?? false)).toHaveLength(2);
  expect(waiting.textContent).toContain('Asked by subagent Review PR #1837');
  expect(get(vaultPending).get('main')).toBe(2);
  expect(get(vaultSummary)!.items!.map(item => item.subagent)).toEqual(['Review PR #1837', 'Review PR #1837']);
  await fireEvent.click(within(waiting).getByRole('button', {name: 'Run once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/vault/sessions/child/actions/a1', {allow: true});
  await fireEvent.click(within(lane()!).getByRole('button', {name: 'Decline'})); await flush();
  expect(m.delete).toHaveBeenCalledWith('/vault/sessions/child/requests/r1');
  await openAccess(); await flush();
  expect(panel()!.textContent).toContain('Subagent Review PR #1837 · Remote commands');
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke Runner'}));
  await fireEvent.click(within(panel()!).getByRole('button', {name: 'Revoke'})); await flush();
  expect(m.delete).toHaveBeenCalledWith('/vault/sessions/child/grants/g1');
  // Nothing was sent to the main chat's own endpoints.
  expect([...m.post.mock.calls, ...m.delete.mock.calls].some(([url]) => String(url).startsWith('/vault/sessions/main/'))).toBe(false);
 });
 it('never fetches a subagent’s own snapshot, even when mounted before /live marks the chat', async () => {
  const { subagentChats } = await import('$lib/stores/subagent-chats');
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? { mode: 'code', is_worker: true }
   : {...empty(), requests: [{id: 'r1', kind: 'machine', purpose: 'Hidden here', expires_at: 9999999999}]});
  mount({sessionId: 'child'});
  await flush();
  expect(accessCalls()).toEqual([]);
  expect(screen.queryByRole('button', {name: /^Vault/})).toBeNull();
  expect(lane()).toBeNull();
  expect(get(subagentChats).has('child')).toBe(true);
  subagentChats.mark('child', false);
 });
 it('keeps the main chat’s own items unlabelled and addressed to itself', async () => {
  snapshot = {...empty(), actions: [{id: 'a2', grant_id: 'g2', operation: 'command', arguments: {command: 'ls'}, expires_at: 9999999999}],
   grants: [{id: 'g2', label: 'NAS', kind: 'machine', target: 'a@b:22', expires_at: 9999999999, writable: true}]};
  mount({sessionId: 'main'});
  await flush();
  expect(lane()!.querySelector('.pa-from')).toBeNull();
  expect(within(lane()!).getByText('Run this on NAS?')).toBeTruthy();
  await fireEvent.click(within(lane()!).getByRole('button', {name: 'Run once'})); await flush();
  expect(m.post).toHaveBeenCalledWith('/vault/sessions/main/actions/a2', {allow: true});
 });
});

// Only a mirrored chat edits saved sign-ins in the panel: they live on the
// device it runs on, which this worker's Settings cannot reach. A local chat
// links to Settings › Vault instead (covered above).
describe('editing a saved sign-in of a mirrored chat inline', () => {
 const machine = {id: 'c'.repeat(32), label: 'NAS', kind: 'machine', target: 'root@10.0.0.2:22', fingerprint: 'SHA256:' + 'a'.repeat(43),
  host: '10.0.0.2', port: 22, username: 'root', uses_key: false};
 const service = {id: 'd'.repeat(32), label: 'Home API', kind: 'service', target: 'https://10.0.0.5/api', fingerprint: '',
  writable: false, auth_header: 'Authorization', auth_scheme: 'Bearer'};
 const endpoint = (id: string) => `/vault/sessions/s1/saved/${id}`;
 const row = (label: string) => screen.getByRole('list', {name: 'Saved sign-ins'}).querySelectorAll('li')[
  [...screen.getByRole('list', {name: 'Saved sign-ins'}).querySelectorAll('li')].findIndex(li => li.textContent?.includes(label))] as HTMLElement;
 async function openEditor(label: string) {
  mount({sessionId: 's1'}); await flush();
  await openAccess(); await flush();
  await fireEvent.click(screen.getByRole('button', {name: `Edit ${label}`})); await flush();
  return within(row(label));
 }
 // What each PATCH carried when it was sent: the component blanks typed
 // secrets in the request body as soon as the request settles.
 let sent: Record<string, unknown>[] = [];
 let reply: () => Promise<unknown> = async () => ({updated: true, revoked: 0, host_key_cleared: false});
 const lastSent = () => sent[sent.length - 1];
 beforeEach(() => {
  sent = [];
  reply = async () => ({updated: true, revoked: 0, host_key_cleared: false});
  m.patch.mockImplementation(async (_path: string, body: Record<string, unknown>) => { sent.push({...body}); return reply(); });
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  snapshot = {...empty(), vault_available: true, saved_connections: [machine, service]};
  m.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode: 'code', is_remote: true, device_name: 'Owner PC'} : snapshot);
 });
 it('expands the row into a form with the details but never a secret, and sends only what changed', async () => {
  const form = await openEditor('NAS');
  // Inline in the same row of the same panel, not a nested dialog.
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  expect(within(row('NAS')).getByRole('form', {name: 'Edit NAS'})).toBeTruthy();
  expect(within(row('NAS')).queryByRole('button', {name: 'Remove NAS'})).toBeNull();
  expect(screen.getByText('Saved sign-ins on Owner PC')).toBeTruthy();
  // This worker's Settings manages them as well: saved sign-ins sync.
  expect(screen.getByRole('button', {name: 'Manage saved sign-ins in Settings › Vault'})).toBeTruthy();
  expect((form.getByLabelText('Name') as HTMLInputElement).value).toBe('NAS');
  expect((form.getByLabelText('Address') as HTMLInputElement).value).toBe('10.0.0.2');
  expect((form.getByLabelText('Port') as HTMLInputElement).value).toBe('22');
  expect((form.getByLabelText('Username') as HTMLInputElement).value).toBe('root');
  const password = form.getByLabelText('Password') as HTMLInputElement;
  expect(password.value).toBe('');
  expect(password.placeholder).toBe('Unchanged');
  expect(password.required).toBe(false);
  expect((form.getByLabelText('Host-key fingerprint') as HTMLInputElement).value).toBe('');
  // The other saved row keeps its own actions.
  expect(screen.getByRole('button', {name: 'Edit Home API'})).toBeTruthy();
  await fireEvent.input(form.getByLabelText('Port'), {target: {value: '2222'}});
  expect(form.getByText(/clears the pinned host key/)).toBeTruthy();
  reply = async () => ({updated: true, revoked: 1, host_key_cleared: true});
  await fireEvent.submit(form.getByRole('form', {name: 'Edit NAS'})); await flush();
  // A blank password keeps the stored one: it is not sent at all.
  expect(m.patch.mock.calls[0][0]).toBe(endpoint(machine.id));
  expect(lastSent()).toEqual({port: 2222});
  expect(screen.queryByRole('form', {name: 'Edit NAS'})).toBeNull();
  const notice = screen.getByRole('status');
  expect(notice.textContent).toContain('1 active grant using it was revoked');
  expect(notice.textContent).toContain('host key is checked and saved again');
 });
 it('sends a typed password once, clears it, and shows a refusal inline', async () => {
  const form = await openEditor('NAS');
  reply = async () => { throw {status: 400, detail: 'Enter a POSIX username: letters, digits, dot, dash or underscore.'}; };
  await fireEvent.input(form.getByLabelText('Username'), {target: {value: 'bad name'}});
  await fireEvent.input(form.getByLabelText('Password'), {target: {value: 'test-only-new-password'}});
  await fireEvent.submit(form.getByRole('form', {name: 'Edit NAS'})); await flush();
  expect(lastSent()).toEqual({username: 'bad name', password: 'test-only-new-password'});
  // The request body itself no longer holds the password once it settled.
  expect(m.patch.mock.calls[0][1]).toEqual({username: 'bad name', password: ''});
  // Still open with the error in the row; the typed secret is gone.
  expect(form.getByRole('alert').textContent).toContain('POSIX username');
  expect((form.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  expect((form.getByLabelText('Username') as HTMLInputElement).value).toBe('bad name');
  expect(JSON.stringify(sent)).not.toContain('Unchanged');
 });
 it('switching to a private key requires the key and drops the password', async () => {
  const form = await openEditor('NAS');
  await fireEvent.click(form.getByRole('button', {name: /Use a private key instead/}));
  expect(form.queryByLabelText('Password')).toBeNull();
  expect((form.getByLabelText('Private key') as HTMLTextAreaElement).required).toBe(true);
  await fireEvent.input(form.getByLabelText('Private key'), {target: {value: 'test-only-key'}});
  await fireEvent.submit(form.getByRole('form', {name: 'Edit NAS'})); await flush();
  expect(lastSent()).toEqual({private_key: 'test-only-key'});
 });
 it('cancel and closing the panel send nothing and drop typed secrets', async () => {
  let form = await openEditor('NAS');
  await fireEvent.input(form.getByLabelText('Password'), {target: {value: 'test-only-password'}});
  await fireEvent.click(form.getByRole('button', {name: 'Cancel'})); await flush();
  expect(screen.queryByRole('form', {name: 'Edit NAS'})).toBeNull();
  await fireEvent.click(screen.getByRole('button', {name: 'Edit NAS'})); await flush();
  form = within(row('NAS'));
  expect((form.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  await fireEvent.input(form.getByLabelText('Password'), {target: {value: 'test-only-password'}});
  await fireEvent.keyDown(document.body, {key: 'Escape'}); await flush();
  await openAccess(); await flush();
  expect(screen.queryByRole('form', {name: 'Edit NAS'})).toBeNull();
  await fireEvent.click(screen.getByRole('button', {name: 'Edit NAS'})); await flush();
  expect((within(row('NAS')).getByLabelText('Password') as HTMLInputElement).value).toBe('');
  // Saving with nothing changed closes without a request.
  await fireEvent.submit(screen.getByRole('form', {name: 'Edit NAS'})); await flush();
  expect(screen.queryByRole('form', {name: 'Edit NAS'})).toBeNull();
  expect(m.patch).not.toHaveBeenCalled();
 });
 it('edits a service: a blank token keeps the stored one, a typed one replaces it', async () => {
  const form = await openEditor('Home API');
  expect((form.getByLabelText('Service address') as HTMLInputElement).value).toBe('https://10.0.0.5/api');
  expect((form.getByLabelText('Credential header') as HTMLInputElement).value).toBe('Authorization');
  const token = form.getByLabelText('API key or token') as HTMLInputElement;
  expect(token.value).toBe('');
  expect(token.placeholder).toBe('Unchanged');
  await fireEvent.click(form.getByRole('checkbox', {name: /Allow changes/}));
  await fireEvent.submit(form.getByRole('form', {name: 'Edit Home API'})); await flush();
  expect(m.patch.mock.calls[0][0]).toBe(endpoint(service.id));
  expect(lastSent()).toEqual({writable: true});
  await fireEvent.click(screen.getByRole('button', {name: 'Edit Home API'})); await flush();
  const again = within(row('Home API'));
  await fireEvent.input(again.getByLabelText('API key or token'), {target: {value: 'test-only-token'}});
  await fireEvent.submit(again.getByRole('form', {name: 'Edit Home API'})); await flush();
  expect(lastSent()).toEqual({secret: 'test-only-token'});
 });
 it('edits a username and password service as an account, never through the token header settings', async () => {
  snapshot = {...snapshot, saved_connections: [machine, {...service, auth_scheme: 'Basic', username: 'admin'}]};
  const form = await openEditor('Home API');
  expect(form.queryByLabelText('Credential header')).toBeNull();
  expect(form.queryByLabelText('Scheme')).toBeNull();
  expect(form.queryByLabelText('API key or token')).toBeNull();
  expect((form.getByLabelText('Username') as HTMLInputElement).value).toBe('admin');
  const password = form.getByLabelText('Password') as HTMLInputElement;
  expect(password.value).toBe('');
  expect(password.placeholder).toBe('Unchanged');
  await fireEvent.input(form.getByLabelText('Username'), {target: {value: 'operator'}});
  await fireEvent.input(password, {target: {value: 'test only pass'}});
  await fireEvent.submit(form.getByRole('form', {name: 'Edit Home API'})); await flush();
  // The scheme is not resent: the worker keeps Basic and encodes the pair itself.
  expect(lastSent()).toEqual({username: 'operator', secret: 'test only pass'});
  expect(m.patch.mock.calls[0][1].secret).toBe('');
 });
 it('refuses to take private details outside a secure context', async () => {
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
  const form = await openEditor('NAS');
  expect(form.getByRole('alert').textContent).toContain('HTTPS');
  expect((form.getByLabelText('Password') as HTMLInputElement).disabled).toBe(true);
  expect((form.getByRole('button', {name: 'Save changes'}) as HTMLButtonElement).disabled).toBe(true);
 });
});
