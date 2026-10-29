/**
 * Binding a NEW session must only move the chat surface forward.
 *
 * Owner report: creating a worktree session went full page → reload → the
 * centred new-chat composer → full page. The journey
 * (tests/journeys/new-worktree-session.journey.mjs) traced it to two gaps,
 * pinned here for every creation path:
 *
 *  1. The new session's owner was unknown at bind (`isRemote` undefined until
 *     the sidebar reloaded). ChatView's MirrorSyncGate reads unknown as a
 *     mirrored chat, swapped the transcript for "Checking sync…" and then
 *     reloaded the feed from seq 0 — through `loading` to the empty
 *     `cleared` hero. Every path must bind with a known owner.
 *  2. createWorktree bound the id with no message row and only then waited
 *     for the socket, so the chat read as a settled empty session (the
 *     `cleared` hero) for the whole wait. The bind and the first row must
 *     land together.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMessageSender, newSessionWorker, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import { classifyChatSurface, type ChatSurface } from '$lib/components/layout/chat-empty-state';
import { feedController } from './v2/feed-controller';

vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'feat/auto-name' })),
	generateBranchNameLocal: vi.fn(() => 'local/auto-name'),
}));
const post = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { post, get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		start: vi.fn(), stop: vi.fn(), isFeeding: vi.fn(() => false),
		isReducerStreaming: vi.fn(() => false), clearOptimisticSend: vi.fn(),
	},
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

const PICK = { id: 'inst-other', name: 'Other worker' };

function setup(overrides: Partial<ActiveSessionState> = {}, opts: { connected?: boolean } = {}) {
	let state = {
		sessionId: null,
		title: 'New Chat',
		mode: 'code',
		profile: 'claude-code',
		project: 'proj-1',
		projectV2Id: null,
		messages: [],
		queuedMessages: [],
		pendingPeerMessages: [],
		activeTasks: [],
		isStreaming: false,
		isRestoringSession: false,
		isLoadingSession: false,
		worktreeIntent: 'worktree',
		worktreeMode: 'new',
		worktreeBaseBranch: 'main',
		worktreeBranch: '',
		worktreeCreateNewBranch: false,
		isCreatingWorktree: false,
		worktreeError: null,
		error: null,
		fastMode: false,
		deviceIntent: null,
		isRemote: undefined,
		ownerDeviceName: undefined,
		...overrides,
	} as unknown as ActiveSessionState;
	const internal: InternalMutableState = {
		pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null,
	};
	let connected = opts.connected ?? true;
	const wsConn = {
		isConnected: vi.fn(() => connected),
		// The socket opens a little later, as a real one does.
		connect: vi.fn(() => { setTimeout(() => { connected = true; }, 150); }),
		send: vi.fn(() => true),
		resetReconnectAttempts: vi.fn(),
	};
	// Every state the view could paint, with the socket state at that moment.
	// Svelte renders a store change on the next microtask, so states replaced
	// within one synchronous run never reach the screen: record per flush.
	const history: { state: ActiveSessionState; connected: boolean }[] = [{ state, connected }];
	let flushQueued = false;
	let sender!: ReturnType<typeof createMessageSender>;
	const deps: MessageSenderDeps = {
		update: (fn) => {
			state = fn(state);
			if (flushQueued) return;
			flushQueued = true;
			queueMicrotask(() => { flushQueued = false; history.push({ state, connected }); });
		},
		getState: () => state,
		internal,
		wsConn,
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		// The real second legs (createWorktree's follow-up query and friends).
		getActiveSession: () => ({
			sendMessage: (...args: Parameters<typeof sender.sendMessage>) => sender.sendMessage(...args),
			createWorktree: (...args: Parameters<typeof sender.createWorktree>) => sender.createWorktree(...args),
			autoCreateWorktree: (...args: Parameters<typeof sender.autoCreateWorktree>) => sender.autoCreateWorktree(...args),
		}),
	};
	sender = createMessageSender(deps);
	return { sender, internal, wsConn, history, state: () => state };
}

const surfaceOf = (s: ActiveSessionState): ChatSurface => classifyChatSurface({
	sessionId: s.sessionId,
	messageCount: s.messages.length,
	isRestoringSession: !!s.isRestoringSession,
	isLoadingSession: !!s.isLoadingSession,
});

/** The distinct surfaces in order: a new session must read new → active. */
function surfaces(history: { state: ActiveSessionState }[]): ChatSurface[] {
	const out: ChatSurface[] = [];
	for (const { state } of history) {
		const surface = surfaceOf(state);
		if (out[out.length - 1] !== surface) out.push(surface);
	}
	return out;
}

/** The first state carrying the new session id. */
const boundAt = (history: { state: ActiveSessionState; connected: boolean }[]) =>
	history.find((h) => h.state.sessionId !== null)!;

const created = (id: string, extra: Record<string, unknown> = {}) => ({
	session_id: id, branch_name: 'feat/auto-name', base_branch: 'main', worktree_path: '/w', status: 'ok', ...extra,
});

beforeEach(() => {
	post.mockReset();
	vi.mocked(feedController.start).mockReset();
	vi.useRealTimers();
});

describe('newSessionWorker', () => {
	it('is this worker unless the composer picked another one', () => {
		expect(newSessionWorker({ sessionId: null, deviceIntent: null })).toEqual({ isRemote: false, ownerDeviceName: null });
		expect(newSessionWorker({ sessionId: null, deviceIntent: PICK })).toEqual({ isRemote: true, ownerDeviceName: 'Other worker' });
	});

	it('says nothing about a bound session (its row and /live own that)', () => {
		expect(newSessionWorker({ sessionId: 's-1', deviceIntent: PICK })).toBeNull();
	});
});

describe('every path binds a new session with a known owner', () => {
	it('auto worktree (POST with the prompt)', async () => {
		post.mockResolvedValueOnce(created('wt-auto', { query_started: true }));
		const { sender, history } = setup();
		expect(await sender.autoCreateWorktree('hello')).toBe(true);
		expect(boundAt(history).state).toMatchObject({ sessionId: 'wt-auto', isRemote: false });
	});

	it('explicit branch (POST, then the first query)', async () => {
		post.mockResolvedValueOnce(created('wt-branch'));
		const { sender, history } = setup({ worktreeBranch: 'feat/picked' });
		expect(await sender.createWorktree('hello')).toBe('wt-branch');
		expect(boundAt(history).state).toMatchObject({ sessionId: 'wt-branch', isRemote: false });
	});

	it('explicit branch opens the new session\'s feed itself, once its first row exists', async () => {
		// Its follow-up query lands as a queued turn whose WS `start` may never
		// reach this device; without this the reply waited for a later reload.
		post.mockResolvedValueOnce(created('wt-branch'));
		const { sender, state } = setup({ worktreeBranch: 'feat/picked' });
		vi.mocked(feedController.start).mockImplementationOnce(() => {
			expect(state().messages).toHaveLength(1);
		});
		// A failed expectation inside start() would surface as a failed create.
		expect(await sender.createWorktree('hello')).toBe('wt-branch');
		expect(state().worktreeError).toBeNull();
		expect(feedController.start).toHaveBeenCalledWith('wt-branch');
	});

	it('first send from the composer, worktree or not (the WebSocket query)', async () => {
		// The id arrives later on session_created/start, which keep the state.
		const { sender, state, wsConn } = setup({ worktreeIntent: 'local' });
		expect(await sender.sendMessage('hello')).toBe(true);
		expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'query', session_id: null }));
		expect(state()).toMatchObject({ sessionId: null, isRemote: false, ownerDeviceName: null });
	});

	it('existing worktree (the WebSocket query with its worktree id)', async () => {
		const { sender, state, wsConn } = setup({ worktreeMode: 'existing', worktreeId: 'wt-legacy' });
		expect(await sender.sendMessage('hello')).toBe(true);
		expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'query', worktree_id: 'wt-legacy' }));
		expect(state()).toMatchObject({ isRemote: false });
	});

	it('a call started before any message (POST /sessions)', async () => {
		post.mockResolvedValueOnce({ session_id: 'call-1' });
		const { sender, history } = setup({ worktreeIntent: 'local' });
		expect(await sender.startSessionWithoutPrompt()).toBe('call-1');
		expect(boundAt(history).state).toMatchObject({ sessionId: 'call-1', isRemote: false });
	});

	it('a "Runs on" pick binds as that worker\'s session, after the pick is spent', async () => {
		post.mockResolvedValueOnce(created('wt-remote', { query_started: true }));
		const { sender, state } = setup({ deviceIntent: PICK });
		await sender.autoCreateWorktree('hello');
		expect(state()).toMatchObject({ sessionId: 'wt-remote', deviceIntent: null, isRemote: true, ownerDeviceName: 'Other worker' });

		const ws = setup({ worktreeIntent: 'local', deviceIntent: PICK });
		await ws.sender.sendMessage('hello');
		expect(ws.state()).toMatchObject({ deviceIntent: null, isRemote: true, ownerDeviceName: 'Other worker' });
	});
});

describe('the surface only moves forward while a new session binds', () => {
	it('explicit branch with the socket still connecting: no empty bound session', async () => {
		post.mockResolvedValueOnce(created('wt-branch'));
		const { sender, history } = setup({ worktreeBranch: 'feat/picked' }, { connected: false });
		expect(await sender.createWorktree('hello')).toBe('wt-branch');
		// Bound only once the socket is up, and in the same synchronous run as
		// the first message's row (no state between them reads `cleared`).
		expect(boundAt(history).connected).toBe(true);
		expect(surfaces(history)).toEqual(['new', 'active']);
	});

	it('explicit branch, socket already up', async () => {
		post.mockResolvedValueOnce(created('wt-branch'));
		const { sender, history } = setup({ worktreeBranch: 'feat/picked' });
		await sender.createWorktree('hello');
		expect(surfaces(history)).toEqual(['new', 'active']);
	});

	it('auto worktree, both when the backend started the turn and on its WebSocket fallback', async () => {
		post.mockResolvedValueOnce(created('wt-auto', { query_started: true }));
		const started = setup();
		await started.sender.autoCreateWorktree('hello');
		expect(surfaces(started.history)).toEqual(['new', 'active']);

		post.mockResolvedValueOnce(created('wt-auto-2', { query_started: false }));
		const fallback = setup({}, { connected: false });
		await fallback.sender.autoCreateWorktree('hello');
		expect(surfaces(fallback.history)).toEqual(['new', 'active']);
	});

	it('a user who navigated away during the socket wait gets the query sent, and this view stays unbound', async () => {
		post.mockResolvedValueOnce(created('wt-branch'));
		const { sender, internal, history, wsConn } = setup({ worktreeBranch: 'feat/picked' }, { connected: false });
		const pending = sender.createWorktree('hello');
		await vi.waitFor(() => expect(wsConn.connect).toHaveBeenCalled());
		internal.worktreeCreationGeneration++;
		expect(await pending).toBeNull();
		expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'query', session_id: 'wt-branch', prompt: 'hello' }));
		expect(history.every((h) => h.state.sessionId === null)).toBe(true);
	});
});
