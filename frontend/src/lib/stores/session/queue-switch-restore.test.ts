/**
 * #524 — queued ("Not seen yet") bubbles across a session switch, end to end
 * through the real switcher, WS handler, pending-queue arbiter and
 * queue-hydration controller sharing one store. Only the transport edges (SSE
 * feed, HTTP, app data) are mocked.
 *
 * The contract under test: a pending bubble belongs to its session. Leaving and
 * coming back shows it at once; it disappears only when the server says it was
 * consumed (a snapshot without it, or a transcript receipt carrying its command
 * id) or cancelled (a withdrawn verdict). An `unavailable` snapshot is not an
 * empty queue: the bubble stays, marked stale, and the watch is retried.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../app-data.svelte', () => ({
	appData: { get current() { return { sessions: [] }; } },
}));
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null) },
}));
vi.mock('$lib/services/error-reporter', () => ({ reportClientError: vi.fn() }));
vi.mock('$lib/services/fix-with-ai', () => ({ startFixChat: vi.fn() }));
vi.mock('$lib/stores/support-report.svelte', () => ({
	canSendSupportReport: vi.fn(() => true),
	canFixWithAi: vi.fn(() => false),
	openSupportReport: vi.fn(),
}));
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(),
		start: vi.fn(),
		stop: vi.fn(),
		restart: vi.fn(),
		isFeeding: vi.fn(() => false),
		cachedPatch: vi.fn(() => null),
		hasCachedTranscript: vi.fn(() => false),
		invalidate: vi.fn(),
		hasAppliedUsage: vi.fn(() => false),
		isReducerStreaming: vi.fn(() => false),
		reconcileRunningTasks: vi.fn(() => false),
		noteOptimisticSend: vi.fn(),
		clearOptimisticSend: vi.fn(),
	},
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

import { createSessionSwitcher } from './session-switcher';
import { createMessageHandlers, type InternalMutableState, type MessageHandlerDeps } from './ws-message-handlers';
import { pendingQueue } from './pending-queue';
import { queueHydration, HYDRATION_TIMEOUT_MS, RETRY_DELAYS_MS } from './queue-hydration';
import type { ActiveSessionState } from '../active-session';
import type { SessionEphemeral } from '../session-cache';

const A = 'session-A';
const B = 'session-B';
const CMD = 'client-' + 'a'.repeat(32);
const QUEUED_AT = '2026-10-24T12:00:00.000Z';

function baseState(): ActiveSessionState {
	return {
		sessionId: A, mode: 'code', title: 'A', messages: [], isStreaming: true,
		liveTurnId: null, liveModel: null, liveRequestedModel: null, liveProvider: null,
		wsConnected: true, wsReconnectRequired: false, error: null, profile: 'default',
		sessionProfileId: null, currentQueryProfileId: null, project: '', projectV2Id: null,
		totalTokensIn: 0, totalTokensOut: 0, totalCacheCreationTokens: 0, totalCacheReadTokens: 0,
		contextUsed: null, liveContextUsage: null, contextMax: 0, modelOverride: null,
		permissionModeOverride: null, effortLevelOverride: null, fastMode: false,
		contextWindowChoice: '1m', pendingPermissions: [], deferredPermissions: [],
		pendingQuestions: [], queuedMessages: [], queueStale: false, pendingPeerMessages: [],
		activeTasks: [], draft: '', scrollTop: 0, streamStartTime: null, worktreeIntent: 'local',
		deviceIntent: null, worktreeBranch: '', worktreeBaseBranch: '', worktreeCreateNewBranch: true,
		worktreeId: null, worktreeMode: 'new', selectedWorktreeBranch: null, isCreatingWorktree: false,
		worktreeError: null, worktreeDeleted: false, sessionLocked: false, sessionLockedBy: undefined,
		pendingRestorePrompt: null, isRestoringSession: false, isLoadingSession: false,
	};
}

let state: ActiveSessionState;
let connected: boolean;
const wsSend = vi.fn((_data: Record<string, unknown>) => connected);

function harness() {
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;
	const internal: InternalMutableState = {
		pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null,
	};
	const ephemeral = new Map<string, SessionEphemeral>();
	const switcher = createSessionSwitcher({
		update, getState, wsSend, internal,
		sessionEphemeralCache: { save: (id, e) => { ephemeral.set(id, e); }, restore: (id) => ephemeral.get(id) ?? null },
		onPersistTimerChanged: vi.fn(),
	});
	const deps = {
		update, getState, wsSend,
		streamingSessions: { update: vi.fn(), set: vi.fn() },
		backgroundPermissions: { update: vi.fn() },
		gitRefreshStatus: vi.fn(), gitRefreshDiffStats: vi.fn(), gitRefreshPrState: vi.fn(),
		gitGetProjectId: vi.fn(() => null), gitGetLoading: vi.fn(() => false),
		appLoadSessions: vi.fn(), appLoadApiUserSessions: vi.fn(), mergeSessionPatch: vi.fn(() => true),
		internal, onNewChat: vi.fn(), onPersistSession: vi.fn(), onNotifySessionComplete: vi.fn(),
		onNotifyWaitingForResponse: vi.fn(), wsIsConnected: () => connected, clearStopTimeout: vi.fn(),
	} as unknown as MessageHandlerDeps;
	const { handleMessage } = createMessageHandlers(deps);
	queueHydration.attach({ update, getState, wsSend, wsIsConnected: () => connected });
	return { switcher, handleMessage };
}

type Row = { id: string; content: string; client_message_id?: string; command_id?: string; created_at?: string };

function available(sessionId: string, rows: Row[]): Record<string, unknown> {
	return {
		type: 'sync', event_type: 'queue_updated', session_id: sessionId,
		data: {
			queue_status: 'available', queue_owner_device_id: null,
			queued_messages: rows.map(r => ({ created_at: QUEUED_AT, ...r })),
			pending_peer_messages: [],
		},
	};
}

function unavailable(sessionId: string): Record<string, unknown> {
	return {
		type: 'sync', event_type: 'queue_updated', session_id: sessionId,
		data: { queue_status: 'unavailable', queue_owner_device_id: 'owner-device' },
	};
}

const watchesFor = (id: string) =>
	wsSend.mock.calls.filter(([d]) => d.type === 'watch_session' && d.session_id === id).length;
const bubbles = () => state.queuedMessages.map(m => m.content);

beforeEach(() => {
	vi.useFakeTimers();
	state = baseState();
	connected = true;
	wsSend.mockClear();
	pendingQueue.reset(A, true);
	queueHydration.reset();
});

afterEach(() => {
	queueHydration.reset();
	pendingQueue.reset(null, true);
	vi.useRealTimers();
});

/** A is busy; the user queued "also check the tests" and the server confirmed it. */
function queuedOnA(handleMessage: (d: Record<string, unknown>) => void) {
	handleMessage(available(A, [{ id: 'turn-q', content: 'also check the tests', client_message_id: CMD, command_id: CMD }]));
	expect(bubbles()).toEqual(['also check the tests']);
}

describe('#524 switch away and back', () => {
	it('shows the parked bubble the instant A is re-opened, before its snapshot lands', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);

		await switcher.switchSession(B);
		expect(state.sessionId).toBe(B);
		expect(bubbles()).toEqual([]); // never bleeds into B

		await switcher.switchSession(A);
		expect(bubbles()).toEqual(['also check the tests']);
		expect(state.queuedMessages[0].clientMessageId).toBe(CMD);
		expect(state.queueStale).toBe(false);
		expect(watchesFor(A)).toBe(1);

		// The return snapshot confirms it: still exactly one bubble, by command id.
		handleMessage(available(A, [{ id: 'turn-q', content: 'also check the tests', client_message_id: CMD }]));
		expect(bubbles()).toEqual(['also check the tests']);
		expect(state.queueStale).toBe(false);
	});

	it('keeps an optimistic echo that has no server row yet across the round trip', async () => {
		const { switcher } = harness();
		state = { ...state, queuedMessages: [{ id: CMD, clientMessageId: CMD, content: 'echo', queuedAt: Date.now() }] };
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		expect(bubbles()).toEqual(['echo']);
	});

	it('ignores a late snapshot for A while B is open, and B never inherits A\'s bubbles', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		await switcher.switchSession(B);
		handleMessage(available(A, [])); // delayed, for the session we left
		expect(bubbles()).toEqual([]);
		handleMessage(available(B, [{ id: 'turn-b', content: 'B work' }]));
		expect(bubbles()).toEqual(['B work']);
		await switcher.switchSession(A);
		expect(bubbles()).toEqual(['also check the tests']);
	});

	it('restores across New Chat too', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		switcher.newChat();
		expect(bubbles()).toEqual([]);
		await switcher.switchSession(A);
		expect(bubbles()).toEqual(['also check the tests']);
		switcher.destroy();
	});
});

describe('#524 consumed while away', () => {
	it('drops the bubble when the available return snapshot no longer holds it', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		handleMessage(available(A, []));
		expect(bubbles()).toEqual([]);
	});

	it('drops it when replayed history carries its receipt, even while the queue is unreadable', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		pendingQueue.reset(A); // the real feed resets the arbiter when it starts
		handleMessage(unavailable(A));
		expect(bubbles()).toEqual(['also check the tests']);
		// The feed's backfill (not live) replays the steer's message.user.
		pendingQueue.noteDelivered(A, 'also check the tests', 'turn-live', false, CMD, '2026-10-24T12:00:05.000Z');
		state = { ...state, queuedMessages: pendingQueue.reconcile(A, state.queuedMessages) };
		expect(bubbles()).toEqual([]);
	});

	it('keeps a server re-queue of the same command that postdates the replayed receipt', () => {
		harness();
		pendingQueue.noteDelivered(A, 'redo', 'turn-live', false, CMD, '2026-10-24T11:59:00.000Z');
		const requeued = { id: 'turn-q2', clientMessageId: CMD, content: 'redo', queuedAt: Date.parse(QUEUED_AT) };
		expect(pendingQueue.reconcile(A, [requeued])).toEqual([requeued]);
	});
});

describe('#524 cancelled while away', () => {
	it('does not bring back a bubble withdrawn while B was open, and returns its text to A\'s composer', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		const entry = state.queuedMessages[0];
		pendingQueue.requestRemoval(A, entry);
		await switcher.switchSession(B);
		handleMessage({ type: 'queued_message_removal', session_id: A, client_message_id: CMD,
			message_id: 'turn-q', verdict: 'withdrawn' });
		expect(state.sessionId).toBe(B);

		await switcher.switchSession(A);
		expect(bubbles()).toEqual([]);
		expect(state.pendingRestorePrompt).toBe('also check the tests');
	});

	it('keeps a cancel still awaiting its verdict as "cancelling" after the round trip', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		pendingQueue.requestRemoval(A, state.queuedMessages[0]);
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		handleMessage(available(A, []));
		expect(state.queuedMessages).toHaveLength(1);
		expect(state.queuedMessages[0].status).toBe('cancelling');
	});
});

describe('#524 queue unavailable (stale)', () => {
	it('keeps the bubbles, marks them stale, retries watch_session and clears on an available snapshot', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		expect(watchesFor(A)).toBe(1);

		handleMessage(unavailable(A));
		expect(bubbles()).toEqual(['also check the tests']);
		expect(state.queueStale).toBe(true);

		vi.advanceTimersByTime(RETRY_DELAYS_MS[0]);
		expect(watchesFor(A)).toBe(2);
		handleMessage(unavailable(A));
		vi.advanceTimersByTime(RETRY_DELAYS_MS[0]);
		expect(watchesFor(A)).toBe(2); // backoff grows
		vi.advanceTimersByTime(RETRY_DELAYS_MS[1] - RETRY_DELAYS_MS[0]);
		expect(watchesFor(A)).toBe(3);

		handleMessage(available(A, [{ id: 'turn-q', content: 'also check the tests', client_message_id: CMD }]));
		expect(state.queueStale).toBe(false);
		expect(bubbles()).toEqual(['also check the tests']);
		vi.advanceTimersByTime(60_000);
		expect(watchesFor(A)).toBe(3); // no more retries once confirmed
	});

	it('marks bubbles stale when the watch reply never comes, and stops retrying after switching away', async () => {
		const { switcher, handleMessage } = harness();
		queuedOnA(handleMessage);
		await switcher.switchSession(B);
		await switcher.switchSession(A);
		vi.advanceTimersByTime(HYDRATION_TIMEOUT_MS);
		expect(state.queueStale).toBe(true);
		expect(bubbles()).toEqual(['also check the tests']);

		await switcher.switchSession(B);
		expect(state.queueStale).toBe(false);
		const before = watchesFor(A);
		vi.advanceTimersByTime(120_000);
		expect(watchesFor(A)).toBe(before);
	});

	it('does not flag an empty session stale when its reply is slow', async () => {
		const { switcher } = harness();
		await switcher.switchSession(B);
		vi.advanceTimersByTime(HYDRATION_TIMEOUT_MS * 2);
		expect(state.queueStale).toBe(false);
	});
});

describe('#524 reconnect', () => {
	it('keeps the bubbles through a reconnect and reconciles them against its snapshot', () => {
		const { handleMessage } = harness();
		queuedOnA(handleMessage);
		// Socket drops: a scheduled retry is skipped, the bubble stays.
		connected = false;
		handleMessage(unavailable(A));
		vi.advanceTimersByTime(RETRY_DELAYS_MS[0]);
		expect(watchesFor(A)).toBe(0);
		expect(bubbles()).toEqual(['also check the tests']);
		// onConnected re-watches the active session (active-session.ts).
		connected = true;
		wsSend({ type: 'watch_session', session_id: A });
		queueHydration.watching(A);
		handleMessage(available(A, [
			{ id: 'turn-q', content: 'also check the tests', client_message_id: CMD },
			{ id: 'turn-r', content: 'also check the tests', command_id: 'client-' + 'b'.repeat(32) },
		]));
		// Identical text under two command ids stays two bubbles.
		expect(state.queuedMessages.map(m => m.clientMessageId)).toEqual([CMD, 'client-' + 'b'.repeat(32)]);
		expect(state.queueStale).toBe(false);
	});
});
