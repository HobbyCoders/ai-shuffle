/** Render the shared desktop/mobile bootstrap, with chrome and backend I/O isolated. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import { get, type Writable } from 'svelte/store';
import type { AppShellProps } from './app-shell';
import { _resetProseQuestions, proseQuestionSessions, rememberLeavingProseQuestion } from '$lib/stores/session/prose-questions';
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
 return { activeSession: { ...writable({ sessionId: null, draft: '', project: null }), ...mocks }, backgroundPermissions: writable(new Map()), streamingSessions: writable(new Map()) };
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
import { sessions as sessionList } from '$lib/stores/app-data.svelte';
import { backgroundPermissions } from '$lib/stores/active-session';
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
afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

// Optional wall-clock correlation rig. The CI ratchet is the deterministic
// pre-init request count below, never a shared-host timing threshold.
it.runIf(process.env.PERF_BOOTSTRAP_LAB === '1')('bootstrap wall-clock lab', async () => {
 const report: Record<string, number[]> = {};
 for (const flag of ['0', '1']) {
  vi.stubEnv('VITE_PERF_PARALLEL_BOOTSTRAP', flag);
  const samples: number[] = [];
  for (let i = 0; i < 21; i++) {
   mocks.init.mockImplementation(() => new Promise(resolve => setTimeout(resolve, 25)));
   mocks.load.mockImplementation(() => new Promise(resolve => setTimeout(resolve, 40)));
   let done!: () => void;
   const ready = new Promise<void>(resolve => { done = resolve; });
   mocks.initGlobalStt.mockImplementation(() => { done(); return () => {}; });
   const start = performance.now();
   const view = render(AppPage, { shell });
   await ready;
   if (i) samples.push(performance.now() - start); // warm-up excluded
   view.unmount();
  }
  report[flag === '0' ? 'serial' : 'parallel'] = samples;
 }
 console.log('BOOTSTRAP_LAB ' + JSON.stringify(Object.fromEntries(Object.entries(report).map(([mode, values]) => {
  const sorted = [...values].sort((a, b) => a - b);
  return [mode, { n: sorted.length, p75_ms: sorted[Math.ceil(sorted.length * 0.75) - 1], samples_ms: values }];
 }))));
});

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
 it('starts all independent bootstrap reads alongside init without reconciling early', async () => {
  let finishInit!: () => void; mocks.init.mockReturnValue(new Promise<void>(resolve => { finishInit = resolve; }));
  render(AppPage, { shell });
  await waitFor(() => expect(mocks.load).toHaveBeenCalledTimes(5));
  expect(mocks.validateSelectedProfile).not.toHaveBeenCalled();
  expect(mocks.resolveComposerDefaultsAfterCatalog).not.toHaveBeenCalled();
  finishInit(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce();
 });
 it('retains the serial fallback behind the build-time kill switch', async () => {
  vi.stubEnv('VITE_PERF_PARALLEL_BOOTSTRAP', '0');
  let finishInit!: () => void; mocks.init.mockReturnValue(new Promise<void>(resolve => { finishInit = resolve; }));
  render(AppPage, { shell });
  await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
  expect(mocks.load).toHaveBeenCalledTimes(2);
  finishInit(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.load).toHaveBeenCalledTimes(5);
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
  mocks.init.mockImplementation(async (opts?: { deepLinkSessionId?: string }) => {
   if (opts?.deepLinkSessionId) active.set({ sessionId: opts.deepLinkSessionId, draft: '', project: null });
  });
  window.history.replaceState({}, '', '/?session=notification-task'); await boot();
  expect(mocks.init).toHaveBeenCalledWith({ deepLinkSessionId: 'notification-task' });
  expect(mocks.openSession).toHaveBeenCalledWith('notification-task');
  // Assistant is never mounted first (its requests would race the deep link).
  expect(mocks.openFeaturePage).not.toHaveBeenCalled();
  expect(get(activeView)).toEqual({ type: 'session', sessionId: 'notification-task' });
  expect(window.location.search).toBe('');
 });
 it('shows the linked chat from the first frame, never the chat the tab had open (#1923)', async () => {
  // The tab was last on chat A; the link names chat B.
  views.set({ type: 'session', sessionId: 'chat-a' });
  let release!: () => void;
  mocks.init.mockImplementation((opts?: { deepLinkSessionId?: string }) => {
   if (opts?.deepLinkSessionId) active.set({ sessionId: opts.deepLinkSessionId, draft: '', project: null });
   return new Promise<void>(resolve => { release = resolve; });
  });
  window.history.replaceState({}, '', '/?session=chat-b');
  render(AppPage, { shell });
  // Before init (and the first loads) settle, the view and the store are on B.
  await waitFor(() => expect(mocks.init).toHaveBeenCalledWith({ deepLinkSessionId: 'chat-b' }));
  expect(get(activeView)).toEqual({ type: 'session', sessionId: 'chat-b' });
  expect(get(active).sessionId).toBe('chat-b');
  release(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.openSession).toHaveBeenCalledWith('chat-b');
  expect(mocks.openSession).not.toHaveBeenCalledWith('chat-a');
 });
 it('does not pull the user back to the link once they left it while it loaded', async () => {
  let release!: () => void;
  mocks.init.mockImplementation((opts?: { deepLinkSessionId?: string }) => {
   if (opts?.deepLinkSessionId) active.set({ sessionId: opts.deepLinkSessionId, draft: '', project: null });
   return new Promise<void>(resolve => { release = resolve; });
  });
  window.history.replaceState({}, '', '/?session=gone-id');
  render(AppPage, { shell });
  await waitFor(() => expect(mocks.init).toHaveBeenCalled());
  // The link answered 404 (store fell back to a new chat), or the user moved on.
  active.set({ sessionId: null, draft: '', project: null });
  release(); await waitFor(() => expect(mocks.initGlobalStt).toHaveBeenCalledOnce());
  expect(mocks.openSession).not.toHaveBeenCalled();
  expect(mocks.openFeaturePage).not.toHaveBeenCalled();
  expect(window.location.search).toBe('');
 });
});

// Spec §5: the tab title carries the one "Needs you" count, the open
// session's own prompts and prose question included.
describe('AppPage tab title', () => {
 const list = sessionList as unknown as Writable<unknown[]>;
 const attention = backgroundPermissions as unknown as Writable<Map<string, { count: number; kind: string }>>;
 const asked = [{ id: 'u1', role: 'user', content: 'Deploy it' }, { id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?' }];
 afterEach(() => { list.set([]); attention.set(new Map()); _resetProseQuestions(); });
 it('prefixes the count of everything waiting on you, and drops it on the next user message', async () => {
  list.set([{ id: 'work', status: 'active' }, { id: 'other', status: 'active' }]);
  attention.set(new Map([['other', { count: 1, kind: 'permission' }]]));
  views.set({ type: 'session', sessionId: 'work' });
  const open = (state: Record<string, unknown>) => active.set({ sessionId: 'work', draft: '', project: null, pendingPermissions: [], ...state } as never);
  open({ messages: asked, pendingQuestions: [{ request_id: 'q1' }] });
  await boot();
  await waitFor(() => expect(document.title).toBe('(3) AI Shuffle'));
  open({ messages: [...asked, { id: 'u2', role: 'user', content: 'Prod only' }], pendingQuestions: [] });
  await waitFor(() => expect(document.title).toBe('(1) AI Shuffle'));
  attention.set(new Map());
  await waitFor(() => expect(document.title).toBe('AI Shuffle'));
 });

 it('keeps an unanswered prose question counting across A -> B -> A until a loaded transcript answers it', async () => {
  list.set([{ id: 'work', status: 'active', work_started_at: null }, { id: 'other', status: 'active', work_started_at: null }]);
  views.set({ type: 'session', sessionId: 'work' });
  const open = (state: Record<string, unknown>) => active.set({ draft: '', project: null, pendingPermissions: [], pendingQuestions: [], ...state } as never);
  open({ sessionId: 'work', messages: asked });
  await boot();
  await waitFor(() => expect(document.title).toBe('(1) AI Shuffle'));
  // What the switcher does on leaving: remember, then show B.
  rememberLeavingProseQuestion('work', asked as never);
  open({ sessionId: 'other', messages: [] });
  await tick();
  expect(document.title).toBe('(1) AI Shuffle');
  // Back on A while its history loads, then loaded.
  open({ sessionId: 'work', messages: [], isLoadingSession: true });
  await tick();
  expect(document.title).toBe('(1) AI Shuffle');
  open({ sessionId: 'work', messages: asked });
  await tick();
  expect(document.title).toBe('(1) AI Shuffle');
  // Leave again. A permission resume moves the turn's working time: not an answer.
  open({ sessionId: 'other', messages: [] });
  list.set([{ id: 'work', status: 'active', work_started_at: '2026-10-11T00:00:00Z' }, { id: 'other', status: 'active', work_started_at: null }]);
  await tick();
  expect(document.title).toBe('(1) AI Shuffle');
  // Reopened with the answer loaded and left again: the switcher drops it.
  open({ sessionId: 'work', messages: [...asked, { id: 'u2', role: 'user', content: 'Prod only' }] });
  rememberLeavingProseQuestion('work', [...asked, { id: 'u2', role: 'user', content: 'Prod only' }] as never);
  open({ sessionId: 'other', messages: [] });
  await waitFor(() => expect(document.title).toBe('AI Shuffle'));
  expect(get(proseQuestionSessions).has('work')).toBe(false);
 });
});
