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
		pendingRequestId: null,
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

	return { deps, wsConn, getState, internal, autoCreateWorktree, createWorktree, startCloudSession };
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

describe('message-sender new-session adoption token (hijack fix)', () => {
	function querySends(wsConn: { send: ReturnType<typeof vi.fn> }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
	}

	it('attaches a request_id to a NEW-session query and records it on internal', async () => {
		const { deps, wsConn, internal } = makeDeps(baseState({
			executionMode: 'local',
			sessionId: null,
			messages: [],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('hello new');

		const sends = querySends(wsConn);
		expect(sends).toHaveLength(1);
		const frame = sends[0][0] as any;
		// The outgoing query carries the adoption token so the backend can echo it.
		expect(typeof frame.request_id).toBe('string');
		expect(frame.request_id).toBeTruthy();
		// The same token is recorded so the session_created/start handlers can match it.
		expect(internal.pendingRequestId).toBe(frame.request_id);
		expect(internal.awaitingNewSession).toBe(true);
	});

	it('does NOT attach a request_id to an EXISTING-session query', async () => {
		const { deps, wsConn, internal } = makeDeps(baseState({
			executionMode: 'local',
			sessionId: 'existing-session',
			title: 'Existing',
			messages: [{ id: 'user-1', role: 'user', content: 'earlier' } as any],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('follow up');

		const sends = querySends(wsConn);
		expect(sends).toHaveLength(1);
		const frame = sends[0][0] as any;
		// An existing session routes its start by the known id — no token needed, and
		// awaitingNewSession must NOT be set (it's gated on the absence of a sessionId).
		expect(frame.request_id).toBeUndefined();
		expect(internal.pendingRequestId).toBeNull();
		expect(internal.awaitingNewSession).toBe(false);
	});
});

/**
 * "Send now" (interrupt-and-send): when the user submits while a stream is live,
 * the sender runs the two-phase interrupt_for_query handshake and then fires the
 * replacement query. That query MUST carry interrupt_followup so the backend
 * treats a start-frame send that fails on a socket dropped during the interrupt
 * drain as non-fatal (stream via sync_engine + polling) instead of dropping the
 * message — the same class of bug fixed for "Send later".
 */
describe('message-sender "Send now" flags the interrupt follow-up query', () => {
	function streamingState() {
		return baseState({
			executionMode: 'local',
			sessionId: 'ses-1',
			isStreaming: true,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		});
	}

	it('sets interrupt_followup on the query after the interrupt is confirmed', async () => {
		const { deps, wsConn } = makeDeps(streamingState());
		// Confirm the interrupt as soon as the handshake message goes out, exactly
		// like the real interrupt_confirmed handler resolving the pending promise.
		wsConn.send.mockImplementation((data: Record<string, unknown>) => {
			if (data?.type === 'interrupt_for_query') {
				deps.internal.interruptConfirmResolve?.(true);
			}
			return true;
		});
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('answer this now');

		expect(result).toBe(true);
		// The two-phase handshake ran...
		const interruptSends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'interrupt_for_query');
		expect(interruptSends).toHaveLength(1);
		// ...and the replacement query is flagged as the interrupt follow-up.
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).interrupt_followup).toBe(true);
	});

	it('does not flag a normal (non-interrupting) send', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			executionMode: 'local',
			sessionId: 'ses-1',
			isStreaming: false,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('no rush');

		expect(result).toBe(true);
		const interruptSends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'interrupt_for_query');
		expect(interruptSends).toHaveLength(0);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).interrupt_followup).toBeUndefined();
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

/**
 * Restored-session race guard (e2e Bug A).
 *
 * init() adopts a persisted sessionId BEFORE its history has rendered — the UI
 * shows the "New Chat" empty state while _state.sessionId silently points at an
 * old session. A send fired in that window continues the invisible old session.
 * sendMessage must wait for the restore's initial history load to settle
 * (isRestoringSession → false) before targeting the session, and refuse with a
 * clear error if it never settles.
 */
describe('message-sender restored-session race guard', () => {
	function querySends(wsConn: { send: ReturnType<typeof vi.fn> }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
	}

	it('waits for the restore to settle, then sends into the restored session', async () => {
		vi.useFakeTimers();
		try {
			const { deps, wsConn } = makeDeps(baseState({
				sessionId: 'restored-1',
				messages: [],
				isRestoringSession: true,
			} as any));
			const sender = createMessageSender(deps);

			const pending = sender.sendMessage('hello restored');

			// Still restoring → the query must NOT have gone out yet.
			await vi.advanceTimersByTimeAsync(300);
			expect(querySends(wsConn)).toHaveLength(0);

			// History lands (poll applied) and the restore settles.
			deps.update(s => ({
				...s,
				isRestoringSession: false,
				messages: [{ id: 'm1', role: 'user', content: 'earlier' } as any],
			}));
			await vi.advanceTimersByTimeAsync(200);

			const result = await pending;
			expect(result).toBe(true);
			const sends = querySends(wsConn);
			expect(sends).toHaveLength(1);
			expect((sends[0][0] as any).session_id).toBe('restored-1');
		} finally {
			vi.useRealTimers();
		}
	});

	it('refuses the send when the restore never settles', async () => {
		vi.useFakeTimers();
		try {
			const { deps, wsConn, getState } = makeDeps(baseState({
				sessionId: 'restored-1',
				messages: [],
				isRestoringSession: true,
			} as any));
			const sender = createMessageSender(deps);

			const pending = sender.sendMessage('hello stuck');
			await vi.advanceTimersByTimeAsync(6000);

			const result = await pending;
			expect(result).toBe(false);
			expect(querySends(wsConn)).toHaveLength(0);
			expect(getState().error).toMatch(/loading/i);
		} finally {
			vi.useRealTimers();
		}
	});
});
