import { describe, expect, it, vi, beforeEach } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import { reopenWorktree } from '$lib/api/git';

// message-sender pulls these from $lib/api/git at module load. Only reopenWorktree
// is exercised here (the best-effort reopen for cleaned-up worktree sessions); the
// rest are stubbed so the import binding resolves.
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	migrateUploadsToWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'auto/x' })),
	generateBranchNameLocal: vi.fn(() => 'auto/x'),
}));

/**
 * Regression tests for the "work in main" worktree bug.
 *
 * A worktree-intended NEW session (executionMode 'worktree', no sessionId yet)
 * must never fall through to the plain WS query path — that path carries no
 * worktree_id, so the backend would create the session in the project's MAIN
 * checkout. This happens if a second send slips in while autoCreateWorktree is
 * still running (e.g. the user presses Enter during the multi-second auto-name
 * step).
 */

function baseState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: null,
		title: 'New Chat',
		profile: 'claude-code',
		project: 'proj-1',
		messages: [],
		isStreaming: false,
		executionMode: 'local',
		worktreeId: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeMode: 'new',
		worktreeCreateNewBranch: false,
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		isReopeningWorktree: false,
		worktreeDeleted: false,
		worktreeError: null,
		error: null,
		// Anything the tested paths don't read can stay loose.
		...overrides,
	} as unknown as ActiveSessionState;
}

function makeDeps(initial: ActiveSessionState) {
	let state = initial;
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
	};

	const wsConn = {
		isConnected: vi.fn(() => true),
		connect: vi.fn(),
		send: vi.fn((_data: Record<string, unknown>) => true),
		resetLastMessageTime: vi.fn(),
		resetReconnectAttempts: vi.fn(),
	};

	const autoCreateWorktree = vi.fn(async () => {});
	const createWorktree = vi.fn(async () => {});
	const startCloudSession = vi.fn(async () => {});
	const innerSendMessage = vi.fn(async () => true);

	const deps: MessageSenderDeps = {
		update,
		getState,
		internal,
		wsConn,
		sessionPoller: { stop: vi.fn(), startStreaming: vi.fn() },
		sessionState: { removeSession: vi.fn() },
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => ({
			sendMessage: innerSendMessage,
			createWorktree,
			autoCreateWorktree,
			startCloudSession,
		}),
	};

	return { deps, wsConn, getState, autoCreateWorktree, createWorktree, startCloudSession };
}

describe('message-sender worktree send backstop', () => {
	it('suppresses the send to main when worktree creation is still in flight', async () => {
		// executionMode worktree, no sessionId, but an optimistic message is already
		// present → autoCreateWorktree is mid-flight.
		const { deps, wsConn, getState } = makeDeps(baseState({
			executionMode: 'worktree',
			sessionId: null,
			isCreatingWorktree: true,
			messages: [{ id: 'user-1', role: 'user', content: 'first' } as any],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('second message while creating');

		expect(result).toBe(false);
		// CRITICAL: no WS query may be sent (that would run in main).
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(0);
		expect(getState().worktreeError).toMatch(/setting up the worktree/i);
	});

	it('routes a fresh worktree+auto first message to autoCreateWorktree (not main)', async () => {
		const { deps, wsConn, autoCreateWorktree } = makeDeps(baseState({
			executionMode: 'worktree',
			sessionId: null,
			worktreeBranch: '',
			messages: [],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('build me a thing');

		expect(result).toBe(true);
		expect(autoCreateWorktree).toHaveBeenCalledTimes(1);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(0);
	});

	it('still sends a normal local first message over WS', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			executionMode: 'local',
			sessionId: null,
			messages: [],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('hello local');

		expect(result).toBe(true);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).worktree_id).toBeUndefined();
	});
});

/**
 * Worktree sessions are never a dead end. A session whose worktree folder was
 * cleaned up (worktreeDeleted) must NOT block the send or surface a permanent
 * "worktree deleted" error — it best-effort reopens, clears the flag, and sends
 * the query (carrying worktree_id) so the backend transparently recreates the
 * worktree from its branch on the query path.
 */
describe('message-sender cleaned-up worktree never blocks', () => {
	beforeEach(() => {
		vi.mocked(reopenWorktree).mockReset();
		vi.mocked(reopenWorktree).mockResolvedValue(undefined as never);
	});

	function deletedWorktreeState() {
		return baseState({
			executionMode: 'worktree',
			sessionId: 'ses-1', // existing session → reaches the WS query path
			worktreeId: 'wt-1',
			worktreeBranch: 'feat/login',
			worktreeDeleted: true,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		});
	}

	it('best-effort reopens, clears the flag, and sends the query', async () => {
		const { deps, wsConn, getState } = makeDeps(deletedWorktreeState());
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('keep going');

		expect(result).toBe(true);
		// No permanent block: flag cleared, spinner reset, no error set.
		expect(getState().worktreeDeleted).toBe(false);
		expect(getState().isReopeningWorktree).toBe(false);
		expect(getState().error).toBeNull();
		// Best-effort reopen attempted with the session's worktree.
		expect(reopenWorktree).toHaveBeenCalledWith('proj-1', 'wt-1');
		// The query went out carrying worktree_id so the backend recreates it.
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).worktree_id).toBe('wt-1');
	});

	it('still sends when the best-effort reopen fails (backend auto-recreates)', async () => {
		vi.mocked(reopenWorktree).mockRejectedValueOnce(new Error('reopen 500'));
		const { deps, wsConn, getState } = makeDeps(deletedWorktreeState());
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('keep going anyway');

		expect(result).toBe(true);
		expect(getState().worktreeDeleted).toBe(false);
		expect(getState().isReopeningWorktree).toBe(false);
		expect(getState().error).toBeNull();
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
	});
});

/**
 * stopGeneration: a Stop clicked on a brand-new session before the backend has
 * minted its id can't be addressed yet (no session_id). It must be DEFERRED —
 * recorded so the session_created/start handler can send it once the id arrives
 * — instead of being silently dropped (the original "message saved even though I
 * stopped before output" bug). Once a sessionId exists, the stop goes out
 * immediately.
 */
describe('message-sender stopGeneration deferral', () => {
	function stopSends(wsConn: { send: { mock: { calls: unknown[][] } } }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'stop');
	}

	it('sends the stop immediately when a sessionId already exists', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: 'sess-1', isStreaming: true }));
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		const sends = stopSends(wsConn);
		expect(sends).toHaveLength(1);
		expect((sends[0][0] as any).session_id).toBe('sess-1');
		expect(deps.internal.stopRequestedBeforeSession).toBe(false);
	});

	it('defers the stop when no sessionId yet but a new session is awaited', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: null, isStreaming: true }));
		deps.internal.awaitingNewSession = true;
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		// Nothing addressable yet → must NOT send, but MUST record the intent.
		expect(stopSends(wsConn)).toHaveLength(0);
		expect(deps.internal.stopRequestedBeforeSession).toBe(true);
	});

	it('does nothing when there is no session and none is being created', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: null }));
		// awaitingNewSession stays false (no send in flight).
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		expect(stopSends(wsConn)).toHaveLength(0);
		expect(deps.internal.stopRequestedBeforeSession).toBe(false);
	});
});
