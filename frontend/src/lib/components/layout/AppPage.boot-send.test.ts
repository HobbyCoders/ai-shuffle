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
	feedController: { start: vi.fn(), stop: vi.fn(), isFeeding: () => false, isReducerStreaming: () => false, clearOptimisticSend: vi.fn(), cachedPatch: () => null, reconcileRunningTasks: () => false, hasAppliedUsage: () => false },
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true }
}));
vi.mock('$lib/components/voice/ActiveVoiceCallBar.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/voice/GlobalVoiceInput.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/KeyboardShortcutsModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/ImportSessionModal.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/TagManager.svelte', () => ({ default: () => ({}) }));
vi.mock('$lib/components/SpotlightSearch.svelte', () => ({ default: () => ({}) }));

import AppPage from './AppPage.svelte';
import { api } from '$lib/api/client';
import { activeSession } from '$lib/stores/active-session';
import { activeView } from '$lib/stores/layout.svelte';
import { bumpInitGeneration, createSessionInit, getInitGeneration, hasBootProjectPick, hasBootSendClaim, noteBootProjectPick } from '$lib/stores/session/session-init';
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
	let reject!: (error: Error) => void;
	const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
	return { promise, resolve, reject };
}
function setup() {
	const update = state.update, getState = () => get(state);
	const internal: InternalMutableState = { pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null };
	const send = vi.fn(() => true);
	const wsConn = { send, isConnected: () => true, resetReconnectAttempts: vi.fn(), activate: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), destroy: vi.fn() };
	const cache = { save: vi.fn(), restore: () => null, remove: vi.fn() };
	const switcher = createSessionSwitcher({ update, getState, internal, wsSend: send, sessionEphemeralCache: cache, onPersistTimerChanged: vi.fn() });
	const preferences = deferred<{ selectedProfile: string; selectedProject: string } | null>();
	const init = createSessionInit({ update, getState, internal, wsConn, defaultState: base, setState: state.set, notifySubscribers: vi.fn(),
		switcher: { ...switcher, loadSelectionFromBackend: () => preferences.promise, refreshSessionMetadata: vi.fn(async () => {}) },
		appData: { loadSessions: vi.fn() }, disconnect: vi.fn(), persistSession: switcher.persistSession,
		persistTimer: { get: () => null, clear: vi.fn() } });
	mocks.init.mockImplementation(init.init);
	mocks.destroy.mockImplementation(() => { init.destroy(); switcher.destroy(); });
	mocks.newChat.mockImplementation(() => { switcher.newChat(); });
	const createWorktree = vi.fn(async (): Promise<string | null> => null);
	const autoCreateWorktree = vi.fn<ReturnType<typeof createMessageSender>['autoCreateWorktree']>(async () => false);
	const resend = vi.fn<ReturnType<typeof createMessageSender>['sendMessage']>();
	const sender = createMessageSender({ update, getState, internal, wsConn, sessionEphemeralCache: cache,
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() }, persistSession: switcher.persistSession,
		stopTimeout: { get: () => null, set: vi.fn() }, getActiveSession: () => ({ createWorktree, autoCreateWorktree, sendMessage: resend }) });
	resend.mockImplementation(sender.sendMessage);
	const handlers = createMessageHandlers({ update, getState, internal, wsSend: send,
		streamingSessions: writable(new Map()), backgroundPermissions: writable(new Map()),
		gitRefreshStatus: vi.fn(), gitRefreshDiffStats: vi.fn(), gitGetProjectId: () => null, gitGetLoading: () => false,
		appLoadSessions: vi.fn(), appLoadApiUserSessions: vi.fn(), mergeSessionPatch: () => false,
		onNewChat: mocks.newChat, onPersistSession: switcher.persistSession, onNotifySessionComplete: vi.fn(),
		onNotifyWaitingForResponse: vi.fn(), wsIsConnected: () => true, clearStopTimeout: vi.fn() });
	return { sender, init, preferences, send, wsConn, switcher, createWorktree, autoCreateWorktree, resend, adopt: () => handlers.handleMessage({ type: 'session_created', session_id: 'created', request_id: internal.pendingRequestId }) };
}

beforeEach(() => {
	vi.clearAllMocks(); sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/');
	state.set(base()); views.set({ type: 'session', sessionId: null });
	mocks.openChat.mockImplementation(sessionId => views.set({ type: 'session', sessionId }));
	mocks.ready.mockReturnValue(vi.fn()); mocks.reconcileSessionMode.mockReturnValue('chat');
	vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')));
});
afterEach(() => { cleanup(); mocks.destroy(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

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
			expect(ctx.wsConn.activate).toHaveBeenCalledOnce();
			expect(ctx.wsConn.connect).toHaveBeenCalledOnce();
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


// A failed admission is not live ownership. Hold init with a real promise,
// exercise each sender refusal, and only then release preference hydration.
for (const remounted of [false, true]) {
	it.each([
		'missing-worktree', 'worktree-in-flight', 'compact-first', 'connection-timeout', 'connection-throws', 'stray-session',
		'dispatch-refused', 'dispatch-throws', 'create-refused', 'create-throws',
		'auto-refused', 'auto-throws', 'existing-no-profile', 'existing-no-project'
	])(`restores and reconnects after %s (remounted=${remounted})`, async refusal => {
		mocks.get.mockResolvedValue({ value: { sessionId: 'saved', title: 'Saved chat', profile: 'saved-profile' } });
		const ctx = setup();
		if (remounted) ctx.init.destroy();
		const initializing = ctx.init.init();
		const generation = getInitGeneration();
		const worktree = { worktreeIntent: 'worktree' as const, worktreeMode: 'new' as const };
		if (refusal === 'missing-worktree') state.update(s => ({ ...s, ...worktree, worktreeMode: 'existing' }));
		if (refusal === 'worktree-in-flight') state.update(s => ({ ...s, ...worktree, messages: [{ id: 'pending', role: 'user', content: 'pending' }] }));
		if (refusal === 'stray-session') state.update(s => ({ ...s, messages: [{ id: 'old', role: 'user', content: 'old chat' }] }));
		if (refusal.startsWith('create-')) state.update(s => ({ ...s, ...worktree, worktreeBranch: 'feature/test' }));
		if (refusal.startsWith('auto-')) state.update(s => ({ ...s, ...worktree }));
		if (refusal.startsWith('existing-no-')) state.update(s => ({ ...s, ...worktree, worktreeMode: 'existing', worktreeId: 'wt', profile: refusal === 'existing-no-profile' ? '' : s.profile }));
		if (refusal === 'dispatch-refused') ctx.send.mockReturnValue(false);
		if (refusal === 'dispatch-throws') ctx.send.mockImplementation(() => { throw new Error('dispatch failed'); });
		if (refusal === 'create-throws') ctx.createWorktree.mockRejectedValue(new Error('creation failed'));
		if (refusal === 'auto-throws') ctx.autoCreateWorktree.mockRejectedValue(new Error('creation failed'));
		if (refusal === 'connection-timeout') vi.useFakeTimers();
		if (refusal.startsWith('connection-')) vi.spyOn(ctx.wsConn, 'isConnected').mockReturnValue(false);
		if (refusal === 'connection-throws') ctx.wsConn.connect.mockImplementationOnce(() => { throw new Error('connection failed'); });
		const sending = ctx.sender.sendMessage('refused first message', refusal === 'compact-first' ? { compactFirst: true } : undefined);
		if (refusal === 'connection-timeout') await vi.advanceTimersByTimeAsync(3100);
		if (refusal.endsWith('-throws') || refusal === 'compact-first') await expect(sending).rejects.toThrow();
		else expect(await sending).toBe(false);
		expect(hasBootSendClaim(generation)).toBe(false);
		ctx.preferences.resolve({ selectedProfile: 'default-profile', selectedProject: 'default-project' });
		await initializing;
		expect(get(state).sessionId).toBe('saved');
		expect(ctx.wsConn.activate).toHaveBeenCalledOnce();
		expect(ctx.wsConn.connect).toHaveBeenCalled();
		expect(ctx.wsConn.activate.mock.invocationCallOrder[0]).toBeLessThan(ctx.wsConn.connect.mock.invocationCallOrder.at(-1)!);
		if (remounted) expect(ctx.wsConn.destroy.mock.invocationCallOrder[0]).toBeLessThan(ctx.wsConn.activate.mock.invocationCallOrder[0]);
	});
}

it.each([false, true])('fills defaults and re-arms the socket under an accepted send (adopted=%s)', async adopted => {
	mocks.get.mockResolvedValue({ value: { sessionId: 'stale', profile: 'stale-profile', project: 'stale-project' } });
	const ctx = setup();
	state.update(s => ({ ...s, mode: 'code', profile: '', project: '' }));
	const initializing = ctx.init.init();
	expect(await ctx.sender.sendMessage('accepted first message')).toBe(true);
	if (adopted) ctx.adopt();
	ctx.preferences.resolve({ selectedProfile: 'default-profile', selectedProject: 'default-project' });
	await initializing;
	expect(get(state)).toMatchObject({ sessionId: adopted ? 'created' : null, profile: 'default-profile', project: 'default-project', isRestoringSession: false });
	expect(get(state).messages.map(message => message.content)).toEqual(['accepted first message']);
	expect(ctx.wsConn.activate).toHaveBeenCalledOnce();
	expect(ctx.wsConn.connect).toHaveBeenCalledOnce();
});

it('logs an init rejection under a live claim, clears the restore window and reconnects', async () => {
	mocks.get.mockResolvedValue({ value: { sessionId: 'stale' } });
	const ctx = setup();
	const initializing = ctx.init.init();
	expect(await ctx.sender.sendMessage('accepted first message')).toBe(true);
	ctx.adopt();
	state.update(s => ({ ...s, isRestoringSession: true }));
	const failure = new Error('preference hydration failed');
	const error = vi.spyOn(console, 'error').mockImplementation(() => {});
	ctx.preferences.reject(failure);
	await initializing;
	expect(error).toHaveBeenCalledWith('[ActiveSession] Failed to initialize:', failure);
	expect(get(state)).toMatchObject({ sessionId: 'created', isRestoringSession: false });
	expect(ctx.wsConn.activate).toHaveBeenCalledOnce();
	expect(ctx.wsConn.connect).toHaveBeenCalledOnce();
});


it('owns boot while the first send waits for the socket and init finishes mid-wait', async () => {
	mocks.get.mockResolvedValue({ value: { sessionId: 'stale', title: 'Old saved chat', profile: 'old-agent' } });
	const ctx = setup();
	let connected = false;
	vi.spyOn(ctx.wsConn, 'isConnected').mockImplementation(() => connected);
	render(AppPage, { shell });
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	const sending = ctx.sender.sendMessage('first message across connection wait');
	expect(hasBootSendClaim(getInitGeneration())).toBe(true);
	expect(ctx.wsConn.connect).toHaveBeenCalledOnce();
	ctx.preferences.resolve(null);
	await waitFor(() => expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce());
	expect(get(state)).toMatchObject({ sessionId: null, isRestoringSession: false });
	expect(mocks.newChat).not.toHaveBeenCalled();
	connected = true;
	expect(await sending).toBe(true);
	ctx.adopt();
	await waitFor(() => expect(mocks.openChat).toHaveBeenCalledWith('created'));
	expect(get(state).sessionId).toBe('created');
	expect(get(state).messages.map(message => message.content)).toEqual(['first message across connection wait']);
	expect(ctx.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'query', session_id: null }));
	expect(ctx.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'close_session' }));
	expect(mocks.newChat).not.toHaveBeenCalled();
});

it('does not dispatch into a different session adopted during the connection wait', async () => {
	mocks.get.mockResolvedValue({ value: { sessionId: 'stale' } });
	const ctx = setup();
	let connected = false;
	vi.spyOn(ctx.wsConn, 'isConnected').mockImplementation(() => connected);
	const initializing = ctx.init.init();
	const generation = getInitGeneration();
	const sending = ctx.sender.sendMessage('must not reach another chat');
	// Match activeSession.switchSession's public navigation entry point.
	bumpInitGeneration();
	await ctx.switcher.switchSession('different');
	connected = true;
	expect(await sending).toBe(false);
	expect(get(state).sessionId).toBe('different');
	expect(ctx.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'query' }));
	expect(hasBootSendClaim(generation)).toBe(false);
	ctx.preferences.resolve(null);
	await initializing;
	expect(get(state).sessionId).toBe('different');
});

// #930: a fresh browser (no saved layout view) shows the new-session screen
// long before a slow boot reconciles; the project picked there must survive it.
it.each([true, false])('keeps a project picked on the new-session screen during the loads (picked=%s)', async picked => {
	mocks.get.mockResolvedValue(null);
	views.set(null);
	const ctx = setup();
	render(AppPage, { shell });
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	if (picked) {
		// What activeSession.setProject does on a new compose.
		noteBootProjectPick();
		state.update(s => ({ ...s, mode: 'code', project: 'picked-project' }));
	}
	ctx.preferences.resolve(null);
	await waitFor(() => expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce());
	await waitFor(() => expect(mocks.openChat).toHaveBeenCalledWith(null));
	if (picked) {
		expect(mocks.newChat).not.toHaveBeenCalled();
		expect(get(state)).toMatchObject({ sessionId: null, mode: 'code', project: 'picked-project' });
	} else {
		// An untouched landing still opens a fresh new chat.
		expect(mocks.newChat).toHaveBeenCalledOnce();
	}
	expect(hasBootProjectPick(getInitGeneration())).toBe(false);
});

it.each([true, false])('a saved unbound compose does not overwrite a project picked during init (picked=%s)', async picked => {
	mocks.get.mockResolvedValue({ value: { sessionId: null, title: 'Saved compose', profile: 'saved-agent', project: 'stale-project', worktreeBranch: 'stale-branch' } });
	const ctx = setup();
	const initializing = ctx.init.init();
	if (picked) {
		noteBootProjectPick();
		state.update(s => ({ ...s, mode: 'code', project: 'picked-project', projectV2Id: null, worktreeBranch: '' }));
	}
	ctx.preferences.resolve(null);
	await initializing;
	expect(get(state)).toMatchObject(picked
		? { sessionId: null, mode: 'code', project: 'picked-project', worktreeBranch: '' }
		: { sessionId: null, mode: 'code', project: 'stale-project', worktreeBranch: 'stale-branch' });
});

it.each(['explicit', 'auto'])('keeps a %s worktree adopted before its inner send is refused', async kind => {
	mocks.get.mockResolvedValue({ value: { sessionId: 'stale', title: 'Old saved chat', profile: 'old-agent' } });
	vi.mocked(api.post).mockResolvedValue({ session_id: 'created', branch_name: 'feature/test', base_branch: 'main', status: 'ready', worktree_path: '/test/worktree', query_started: false });
	const ctx = setup();
	ctx.createWorktree.mockImplementation(ctx.sender.createWorktree);
	ctx.autoCreateWorktree.mockImplementation(ctx.sender.autoCreateWorktree);
	state.update(s => ({ ...s, project: 'project', worktreeIntent: 'worktree', worktreeBranch: kind === 'explicit' ? 'feature/test' : '' }));
	ctx.send.mockReturnValue(false);
	render(AppPage, { shell });
	await waitFor(() => expect(mocks.init).toHaveBeenCalledOnce());
	expect(await ctx.sender.sendMessage('refused inner send')).toBe(false);
	expect(ctx.resend).toHaveBeenCalledWith('refused inner send', expect.objectContaining({ skipWorktreeSetup: true }));
	expect(get(state).sessionId).toBe('created');
	expect(hasBootSendClaim(getInitGeneration())).toBe(true);
	ctx.preferences.resolve(null);
	await waitFor(() => expect(mocks.resolveComposerDefaultsAfterCatalog).toHaveBeenCalledOnce());
	expect(get(state).sessionId).toBe('created');
	expect(get(views)).toEqual({ type: 'session', sessionId: 'created' });
	expect(mocks.newChat).not.toHaveBeenCalled();
	expect(ctx.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'close_session' }));
});
