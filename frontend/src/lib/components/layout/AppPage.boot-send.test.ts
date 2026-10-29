/** #821: exercise the actual sender, init, adoption and tab persistence through AppPage. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { get, type Writable, writable } from 'svelte/store';
import type { ActiveSessionState } from '$lib/stores/active-session';
import type { AppShellProps } from './app-shell';
import type { InternalMutableState } from '$lib/stores/session/ws-message-handlers';

const mocks = vi.hoisted(() => ({
	init: vi.fn(), newChat: vi.fn(), destroy: vi.fn(), openChat: vi.fn(), ready: vi.fn(),
	load: vi.fn(async () => {}), get: vi.fn(),
	resolveComposerDefaultsAfterCatalog: vi.fn(), validateSelectedProfile: vi.fn(), reconcileSessionMode: vi.fn()
}));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAuthenticated: writable(true), isAdmin: writable(true) };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: { ...writable({}), ...mocks }, backgroundPermissions: writable(new Map()), streamingSessions: writable(new Map()) };
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { profiles: writable([]), projects: writable([]), sessions: writable([]), sessionsTagFilter: writable(null),
		appData: { current: { profiles: [], sessions: [], projects: [], clusterProjects: [] }, loadProfiles: mocks.load,
			loadSessions: mocks.load, loadProjects: mocks.load, loadClusterProjects: mocks.load, loadApiUserSessions: mocks.load } };
});
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { activeView: writable(null), sidebarCollapsed: writable(false), sidebarOverlay: writable(false),
		layout: { openChat: mocks.openChat, openFeaturePage: vi.fn(), setSidebarCollapsed: vi.fn() } };
});
vi.mock('$lib/stores/sessionMode.svelte', () => ({ appMode: { current: 'chat', set: vi.fn() }, normalizeSessionMode: (mode: string) => mode || 'code' }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: vi.fn() }));
vi.mock('$lib/services/keyboard', () => ({ registerShortcut: vi.fn(() => ({ unregister: vi.fn() })) }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/services/resource-sync', () => ({ resourceSync: { invalidate: vi.fn() } }));
vi.mock('$lib/services/globalStt', () => ({ initGlobalStt: mocks.ready }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: vi.fn(), put: vi.fn() }, getTags: vi.fn(async () => []) }));
vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: () => false, sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(), voiceContextFor: vi.fn() }));
vi.mock('$lib/stores/session/v2/feed-controller', () => ({
	feedController: { start: vi.fn(), stop: vi.fn(), isFeeding: () => false, isReducerStreaming: () => false, clearOptimisticSend: vi.fn() },
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true }
}));
vi.mock('$lib/components/voice/ActiveVoiceCallBar.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/voice/GlobalVoiceInput.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/KeyboardShortcutsModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/ImportSessionModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/TagManager.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/SpotlightSearch.svelte', () => ({ default: () => ({}) }));

import AppPage from './AppPage.svelte';
import { activeSession } from '$lib/stores/active-session';
import { activeView } from '$lib/stores/layout.svelte';
import { createSessionInit, getInitGeneration, hasBootSendClaim } from '$lib/stores/session/session-init';
import { createMessageSender } from '$lib/stores/session/message-sender';
import { createSessionSwitcher } from '$lib/stores/session/session-switcher';
import { createMessageHandlers } from '$lib/stores/session/ws-message-handlers';
import { feedController } from '$lib/stores/session/v2/feed-controller';
import { TAB_ACTIVE_SESSION_KEY } from '$lib/stores/device-id';

const state = activeSession as unknown as Writable<ActiveSessionState>;
const views = activeView as unknown as Writable<{ type: 'session'; sessionId: string | null } | null>;
const shell = createRawSnippet<[AppShellProps]>(() => ({ render: () => '<div>shell</div>' }));
const base = () => ({ sessionId: null, title: 'New Chat', mode: 'chat', draft: '', profile: 'dev-agent', project: '', projectV2Id: null,
	messages: [], queuedMessages: [], pendingPeerMessages: [], activeTasks: [], worktreeIntent: 'local', worktreeBranch: '',
	worktreeBaseBranch: 'main', worktreeMode: 'new', worktreeId: null, contextWindowChoice: 'standard', bindingUnhydrated: {},
	isStreaming: false, isRestoringSession: false, error: null } as unknown as ActiveSessionState);
function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>(done => { resolve = done; });
	return { promise, resolve };
}
function setup() {
	const update = state.update, getState = () => get(state);
	const internal: InternalMutableState = { pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null };
	const send = vi.fn(() => true);
	const wsConn = { send, isConnected: () => true, resetReconnectAttempts: vi.fn(), activate: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), destroy: vi.fn() };
	const cache = { save: vi.fn(), restore: () => null, remove: vi.fn() };
	const switcher = createSessionSwitcher({ update, getState, internal, wsSend: send, sessionEphemeralCache: cache, onPersistTimerChanged: vi.fn() });
	const preferences = deferred<null>();
	const init = createSessionInit({ update, getState, internal, wsConn, defaultState: base, setState: state.set, notifySubscribers: vi.fn(),
		switcher: { ...switcher, loadSelectionFromBackend: () => preferences.promise, refreshSessionMetadata: vi.fn(async () => {}) },
		appData: { loadSessions: vi.fn() }, disconnect: vi.fn(), persistSession: switcher.persistSession,
		persistTimer: { get: () => null, clear: vi.fn() } });
	mocks.init.mockImplementation(init.init);
	mocks.destroy.mockImplementation(() => { init.destroy(); switcher.destroy(); });
	mocks.newChat.mockImplementation(() => { switcher.newChat(); });
	const sender = createMessageSender({ update, getState, internal, wsConn, sessionEphemeralCache: cache,
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() }, persistSession: switcher.persistSession,
		stopTimeout: { get: () => null, set: vi.fn() }, getActiveSession: vi.fn() });
	const handlers = createMessageHandlers({ update, getState, internal, wsSend: send,
		streamingSessions: writable(new Map()), backgroundPermissions: writable(new Map()),
		gitRefreshStatus: vi.fn(), gitRefreshDiffStats: vi.fn(), gitGetProjectId: () => null, gitGetLoading: () => false,
		appLoadSessions: vi.fn(), appLoadApiUserSessions: vi.fn(), mergeSessionPatch: () => false,
		onNewChat: mocks.newChat, onPersistSession: switcher.persistSession, onNotifySessionComplete: vi.fn(),
		onNotifyWaitingForResponse: vi.fn(), wsIsConnected: () => true, clearStopTimeout: vi.fn() });
	return { sender, init, preferences, send, adopt: () => handlers.handleMessage({ type: 'session_created', session_id: 'created', request_id: internal.pendingRequestId }) };
}

beforeEach(() => {
	vi.clearAllMocks(); sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/');
	state.set(base()); views.set({ type: 'session', sessionId: null });
	mocks.openChat.mockImplementation(sessionId => views.set({ type: 'session', sessionId }));
	mocks.ready.mockReturnValue(vi.fn()); mocks.reconcileSessionMode.mockReturnValue('chat');
	vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

for (const view of ['absent', 'new-chat'] as const) {
	for (const snapshot of ['absent', 'null', 'old'] as const) {
		it.each([false, true])(`keeps a first send through init (view=${view}, snapshot=${snapshot}, pendingId=%s)`, async pendingId => {
			const saved = snapshot === 'absent' ? null : { sessionId: snapshot === 'old' ? 'stale' : null, title: 'Stale', profile: 'stale-agent' };
			mocks.get.mockResolvedValue(saved ? { value: saved } : null);
			if (view === 'absent') views.set(null);
			const ctx = setup();
			const page = render(AppPage, { shell });
			await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
			const generation = getInitGeneration();
			expect(await ctx.sender.sendMessage('first message')).toBe(true);
			expect(getInitGeneration()).toBe(generation); // send/adoption is not navigation
			expect(hasBootSendClaim(generation)).toBe(true);
			if (!pendingId) {
				ctx.adopt();
				// Even a completed first turn is owned; this is not an isStreaming guard.
				state.update(s => ({ ...s, isStreaming: false }));
			}
			ctx.preferences.resolve(null);
			await waitFor(() => expect(mocks.ready).toHaveBeenCalledOnce());
			expect(mocks.newChat).not.toHaveBeenCalled();
			expect(ctx.send).not.toHaveBeenCalledWith({ type: 'close_session' });
			expect(get(state).sessionId).toBe(pendingId ? null : 'created');
			expect(get(state).messages.map(message => message.content)).toEqual(['first message']);
			expect(hasBootSendClaim(generation)).toBe(false); // reconcile retires the claim
			if (pendingId) ctx.adopt();
			await waitFor(() => expect(get(activeView)).toEqual({ type: 'session', sessionId: 'created' }));
			// Drive the real persistence writer, then init's real tab-local reader.
			await waitFor(() => expect(JSON.parse(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY)!)).toMatchObject({ sessionId: 'created' }));
			page.unmount(); state.set(base());
			await ctx.init.init();
			expect(get(state).sessionId).toBe('created');
			expect(feedController.start).toHaveBeenLastCalledWith('created');
		});
	}
}
