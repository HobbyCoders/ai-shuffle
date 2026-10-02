/** Render the shared desktop/mobile bootstrap, with chrome and backend I/O isolated. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { get, type Writable } from 'svelte/store';
import type { AppShellProps } from './app-shell';
import type { ViewType } from '$lib/stores/layout.svelte';
type ActiveView = { type: ViewType; sessionId?: string | null };

const mocks = vi.hoisted(() => ({
 init: vi.fn(), newChat: vi.fn(), resolveComposerDefaultsAfterCatalog: vi.fn(), destroy: vi.fn(), reconcileSessionMode: vi.fn(), validateSelectedProfile: vi.fn(),
 load: vi.fn(), openFeaturePage: vi.fn(), openChat: vi.fn(), openSession: vi.fn(), initGlobalStt: vi.fn(),
}));
vi.mock('$lib/stores/auth.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { isAuthenticated: writable(true), isAdmin: writable(true) };
});
vi.mock('$lib/stores/active-session', async () => {
 const { writable } = await import('svelte/store');
 return { activeSession: { ...writable({ sessionId: null, draft: '', project: null }), ...mocks } };
});
vi.mock('$lib/stores/app-data.svelte', async () => {
 const { writable } = await import('svelte/store');
 return {
  profiles: writable([]), projects: writable([]), sessions: writable([]), sessionsTagFilter: writable(null),
  appData: { current: { profiles: [], sessions: [] }, loadProfiles: mocks.load, loadSessions: mocks.load,
   loadProjects: mocks.load, loadClusterProjects: mocks.load, loadApiUserSessions: mocks.load },
 };
});
vi.mock('$lib/stores/layout.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { activeView: writable(null), sidebarCollapsed: writable(false), sidebarOverlay: writable(false),
  layout: { openFeaturePage: mocks.openFeaturePage, openChat: mocks.openChat, setSidebarCollapsed: vi.fn() } };
});
vi.mock('$lib/stores/sessionMode.svelte', () => ({ appMode: { current: 'code', set: vi.fn() } }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession }));
vi.mock('$lib/services/keyboard', () => ({ registerShortcut: vi.fn(() => ({ unregister: vi.fn() })) }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/services/resource-sync', () => ({ resourceSync: { invalidate: vi.fn() } }));
vi.mock('$lib/services/globalStt', () => ({ initGlobalStt: mocks.initGlobalStt }));
vi.mock('$lib/api/client', () => ({ getTags: vi.fn().mockResolvedValue([]) }));
// These are unrelated shell peripherals. The production AppPage lifecycle and
// its actual shell callback wiring below remain mounted and exercised.
vi.mock('$lib/components/voice/ActiveVoiceCallBar.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/voice/GlobalVoiceInput.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/KeyboardShortcutsModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/ImportSessionModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/TagManager.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/SpotlightSearch.svelte', () => ({ default: () => ({}) }));
import AppPage from './AppPage.svelte';
import { activeView } from '$lib/stores/layout.svelte';
import { activeSession } from '$lib/stores/active-session';
import { isAdmin } from '$lib/stores/auth.svelte';
const active = activeSession as unknown as Writable<{ sessionId: string | null; draft: string; project: null }>;
const admin = isAdmin as unknown as Writable<boolean>;
const views = activeView as unknown as Writable<ActiveView | null>;
const shell = createRawSnippet<[AppShellProps]>(props => ({
 render: () => '<div><button>New conversation</button></div>',
 setup: element => { (element.querySelector('button') as HTMLButtonElement).onclick = () => props().onNewSession(); },
}));
const boot = async () => { render(AppPage, { shell }); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce()); };
const emptyView: ActiveView = { type: 'session', sessionId: null };
beforeEach(() => {
 vi.clearAllMocks(); window.history.replaceState({}, '', '/');
 admin.set(true); active.set({ sessionId: null, draft: '', project: null }); views.set(emptyView);
 mocks.init.mockResolvedValue(undefined); mocks.load.mockResolvedValue(undefined);
 mocks.initGlobalStt.mockReturnValue(vi.fn()); mocks.reconcileSessionMode.mockReturnValue('code');
 mocks.newChat.mockImplementation(() => active.set({ sessionId: null, draft: '', project: null }));
 mocks.openChat.mockImplementation(id => views.set({ type: 'session', sessionId: id }));
 mocks.openFeaturePage.mockImplementation(type => views.set({ type }));
 mocks.openSession.mockImplementation(id => { views.set({ type: 'session', sessionId: id }); active.set({ sessionId: id, draft: '', project: null }); });
});
afterEach(cleanup);

describe('AppPage initial Assistant landing', () => {
 it.each([null, emptyView])('lands an owner with no selected session on Assistant (%j)', async view => {
  views.set(view); await boot();
  expect(mocks.openFeaturePage).toHaveBeenCalledWith('assistant');
  expect(mocks.newChat).not.toHaveBeenCalled();
 });
 it('treats serialized empty Tiptap content as empty, not a meaningful draft', async () => {
  active.set({ sessionId: null, draft: JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] }), project: null });
  await boot(); expect(mocks.openFeaturePage).toHaveBeenCalledWith('assistant');
 });
 it.each(['Finish my question', JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'fileChip', attrs: { name: 'notes.txt' } }] }] })])('preserves meaningful composer content', async draft => {
  active.set({ sessionId: null, draft, project: null }); await boot();
  expect(mocks.openFeaturePage).not.toHaveBeenCalled(); expect(mocks.newChat).not.toHaveBeenCalled();
  expect(get(active).draft).toBe(draft);
 });
 it('preserves a draft even when the layout snapshot is missing', async () => {
  views.set(null); active.set({ sessionId: null, draft: 'Keep this thought', project: null });
  await boot(); expect(mocks.openChat).toHaveBeenCalledWith(null);
  expect(mocks.newChat).not.toHaveBeenCalled(); expect(get(active).draft).toBe('Keep this thought');
 });
 it('preserves an already selected work session', async () => {
  views.set({ type: 'session', sessionId: 'work' }); active.set({ sessionId: 'work', draft: '', project: null });
  await boot(); expect(mocks.openFeaturePage).not.toHaveBeenCalled(); expect(mocks.openChat).not.toHaveBeenCalled();
  expect(get(activeView)).toEqual({ type: 'session', sessionId: 'work' });
 });
 it('restores work when no layout view is saved', async () => {
  views.set(null); active.set({ sessionId: 'work', draft: '', project: null }); await boot();
  expect(mocks.openChat).toHaveBeenCalledWith('work'); expect(mocks.openFeaturePage).not.toHaveBeenCalled();
 });
 it('retains explicit new-chat intent over an older backend session snapshot', async () => {
  active.set({ sessionId: 'old-work', draft: '', project: null }); await boot();
  expect(mocks.newChat).toHaveBeenCalledOnce(); expect(mocks.openFeaturePage).not.toHaveBeenCalled();
 });
 // #1636: until this settles, the composer shows New Chat while a send goes to
 // the old session, then the late newChat wipes it from view.
 it('settles explicit new-chat intent before the startup loads finish', async () => {
  let release!: () => void; mocks.load.mockReturnValue(new Promise<void>(resolve => { release = resolve; }));
  active.set({ sessionId: 'old-work', draft: '', project: null }); render(AppPage, { shell });
  await waitFor(() => expect(mocks.newChat).toHaveBeenCalledOnce());
  expect(mocks.load).toHaveBeenCalled(); expect(mocks.initGlobalStt).not.toHaveBeenCalled();
  // A send during the loads creates a session; finishing startup keeps it.
  active.set({ sessionId: 'sent-during-load', draft: '', project: null });
  release(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.newChat).toHaveBeenCalledOnce(); expect(mocks.openFeaturePage).not.toHaveBeenCalled();
  expect(get(active).sessionId).toBe('sent-during-load');
  // The remembered project and branch resolve against the loaded catalog.
  expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce();
 });
 // #1636 review: the composer holds a send until the catalog answers, so the
 // catalog starts loading alongside init instead of after it.
 it('starts the project catalog load alongside init', async () => {
  let finishInit!: () => void; mocks.init.mockReturnValue(new Promise<void>(resolve => { finishInit = resolve; }));
  render(AppPage, { shell });
  await waitFor(() => expect(mocks.load).toHaveBeenCalledTimes(2));
  expect(mocks.resolveComposerDefaultsAfterCatalog).not.toHaveBeenCalled();
  finishInit(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce();
 });
 it('does not replace another feature destination', async () => {
  views.set({ type: 'profile' }); await boot();
  expect(mocks.openFeaturePage).not.toHaveBeenCalled(); expect(get(activeView)?.type).toBe('profile');
 });
 it('does not send non-owners to owner-only Assistant configuration', async () => {
  admin.set(false); await boot(); expect(mocks.openFeaturePage).not.toHaveBeenCalled();
 });
 it('still permits New conversation after the initial landing', async () => {
  await boot(); mocks.openFeaturePage.mockClear();
  await fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
  expect(mocks.openChat).toHaveBeenCalledWith(null); expect(mocks.newChat).toHaveBeenCalledOnce();
  expect(mocks.openFeaturePage).not.toHaveBeenCalled(); expect(get(activeView)?.type).toBe('session');
 });
 it('does not override New conversation clicked while startup data is loading', async () => {
  let release!: () => void; mocks.init.mockReturnValue(new Promise<void>(resolve => { release = resolve; }));
  render(AppPage, { shell }); await fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
  release(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.openFeaturePage).not.toHaveBeenCalled(); expect(get(activeView)?.type).toBe('session');
 });
 it('lets explicit session deep-links take priority', async () => {
  window.history.replaceState({}, '', '/?session=notification-task'); await boot();
  expect(mocks.openSession).toHaveBeenCalledWith('notification-task');
  // Assistant is never mounted first (its requests would race the deep link).
  expect(mocks.openFeaturePage).not.toHaveBeenCalled();
  expect(get(activeView)).toEqual({ type: 'session', sessionId: 'notification-task' });
  expect(window.location.search).toBe('');
 });
});
