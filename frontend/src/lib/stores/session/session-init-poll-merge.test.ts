import { describe, expect, it, vi, beforeEach } from 'vitest';
import { createSessionInit, type SessionInitDeps } from './session-init';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { ChatMessage } from '../types';
import type { PollAppliedData } from './session-poller';

// The poll-error path toasts via notify.error; mock it so tests can observe it.
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn() } }));
import { notify } from '../notifications';

/**
 * Regressions in setupPollCallback's poll merge (session-init.ts):
 *
 * 1. Client-only splice ordering: preserved messages are collected in TWO
 *    passes (system msgs, then optimistic users), but the single-offset splice
 *    assumes ascending insert positions. A late-anchored system msg collected
 *    before an early-anchored optimistic user shoved the user's prompt to the
 *    tail of the chat: [m1, U, m2, SYS] + server [m1, m2] → [m1, m2, U, SYS].
 *
 * 2. Poll-error toast guard: _pollErrorNotified was reset AFTER the
 *    active-session gate, so a background session recovering while another was
 *    focused kept its stale guard entry and its NEXT failure episode toasted
 *    nothing.
 */

const SID = 'session-merge';

function baseState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: SID,
		title: 'Chat',
		messages: [],
		isStreaming: false,
		wsConnected: true,
		error: null,
		profile: 'profile-1',
		project: 'proj-1',
		isRestoringSession: false,
		wsStreamContent: '',
		activeTasks: [],
		...overrides,
	} as unknown as ActiveSessionState;
}

function makeHarness() {
	let state = baseState();
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;
	const setState = (s: ActiveSessionState) => { state = s; };

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		stopBackgroundRequested: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
	};

	const onPollApplied = vi.fn();
	const onPollError = vi.fn();

	const deps: SessionInitDeps = {
		update,
		getState,
		notifySubscribers: vi.fn(),
		defaultState: () => baseState(),
		setState,
		internal,
		sessionPoller: {
			onPollApplied,
			onStreamEnd: vi.fn(),
			onPollError,
			setInterruptGuard: vi.fn(),
			loadSession: vi.fn(async () => null),
			startStreaming: vi.fn(),
			stop: vi.fn(),
			stopAll: vi.fn(),
			isPolling: vi.fn(() => false),
		},
		wsConn: { connect: vi.fn(), disconnect: vi.fn(), destroy: vi.fn() },
		switcher: {
			loadSelectionFromBackend: vi.fn(async () => null),
			loadPersistedSession: vi.fn(async () => null),
			getPersistedProfile: () => 'profile-1',
			getPersistedProject: () => 'proj-1',
			destroy: vi.fn(),
		},
		appData: { loadSessions: vi.fn() },
		disconnect: vi.fn(),
		persistSession: vi.fn(),
		persistTimer: { get: () => null, clear: vi.fn() },
	};

	return { init: createSessionInit(deps).init, getState, setState, onPollApplied, onPollError };
}

function msg(id: string, role: string, content: string, type = 'text'): ChatMessage {
	return { id, role, content, type } as unknown as ChatMessage;
}

function pollData(messages: ChatMessage[]): PollAppliedData {
	return { messages, isStreaming: false, usage: {}, activeTasks: [] } as unknown as PollAppliedData;
}

describe('session-init · client-only message splice ordering', () => {
	beforeEach(() => {
		sessionStorage.clear();
		vi.clearAllMocks();
	});

	it('keeps an optimistic user msg at its position when a system msg follows it', async () => {
		const harness = makeHarness();
		await harness.init();
		const pollCb = harness.onPollApplied.mock.calls[0][0] as
			(sessionId: string, data: PollAppliedData) => void;

		// [m1, U(optimistic), m2, SYS] — server only knows m1 and m2.
		harness.setState(baseState({
			messages: [
				msg('msg-1', 'user', 'first prompt'),
				msg('user-1700000000', 'user', 'optimistic prompt'),
				msg('msg-2', 'assistant', 'reply'),
				msg('sys-1', 'system', 'hook note', 'system'),
			],
		}));

		pollCb(SID, pollData([
			msg('msg-1', 'user', 'first prompt'),
			msg('msg-2', 'assistant', 'reply'),
		]));

		expect(harness.getState().messages.map(m => m.id)).toEqual([
			'msg-1', 'user-1700000000', 'msg-2', 'sys-1',
		]);
	});

	it('keeps each client-only system card after its own anchor when the server payload REORDERS present messages (compaction)', async () => {
		// At a compaction/turn boundary the poller re-seeds from the durable
		// transcript, so the server payload can RE-ORDER present messages relative
		// to the client array. The client-only cards (hooks/system) must each stay
		// after their OWN preceding present neighbor. The old precomputed-index +
		// single-global-offset splice shoved a later card past an earlier present
		// message (sys-b jumped ahead of msg-p1) because the anchors were no longer
		// monotonic in the server payload's order.
		const harness = makeHarness();
		await harness.init();
		const pollCb = harness.onPollApplied.mock.calls[0][0] as
			(sessionId: string, data: PollAppliedData) => void;

		// Client array (correct at render time): P1, SysA, P2, SysB.
		harness.setState(baseState({
			messages: [
				msg('msg-p1', 'assistant', 'p1'),
				msg('sys-a', 'system', 'hook a', 'system'),
				msg('msg-p2', 'assistant', 'p2'),
				msg('sys-b', 'system', 'hook b', 'system'),
			],
		}));

		// Server payload re-orders the present pair to [P2, P1].
		pollCb(SID, pollData([
			msg('msg-p2', 'assistant', 'p2'),
			msg('msg-p1', 'assistant', 'p1'),
		]));

		// SysA stays after P1, SysB stays after P2.
		expect(harness.getState().messages.map(m => m.id)).toEqual([
			'msg-p2', 'sys-b', 'msg-p1', 'sys-a',
		]);
	});
});

describe('session-init · poll-error toast guard reset', () => {
	beforeEach(() => {
		sessionStorage.clear();
		vi.clearAllMocks();
	});

	it('a background recovery clears the guard so the next failure episode toasts again', async () => {
		const harness = makeHarness();
		await harness.init();
		const pollCb = harness.onPollApplied.mock.calls[0][0] as
			(sessionId: string, data: PollAppliedData) => void;
		const errCb = harness.onPollError.mock.calls[0][0] as
			(sessionId: string, consecutiveErrors: number, error: unknown) => void;

		// Episode 1: active session fails → one toast, guard armed.
		harness.setState(baseState({ sessionId: SID }));
		errCb(SID, 3, new Error('HTTP 503'));
		expect(notify.error).toHaveBeenCalledTimes(1);

		// User switches away; the session recovers in the BACKGROUND (the
		// callback early-returns on the active-session gate, but must still
		// reset the guard).
		harness.setState(baseState({ sessionId: 'other-session' }));
		pollCb(SID, pollData([msg('msg-1', 'user', 'hello')]));

		// Back on the session, a NEW failure episode must surface a toast.
		harness.setState(baseState({ sessionId: SID }));
		errCb(SID, 3, new Error('HTTP 503'));
		expect(notify.error).toHaveBeenCalledTimes(2);
	});
});
