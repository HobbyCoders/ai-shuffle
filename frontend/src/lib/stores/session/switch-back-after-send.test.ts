/**
 * #1142: switching away from a chat right after sending, then straight back,
 * must not show that chat one message short (10 -> 9 -> 10).
 *
 * Drives the REAL session switcher and the REAL feed controller on one store
 * (only the SSE transport, HTTP and app data are mocked): the send's durable
 * receipt (`message.user`) is held back, the local echo is appended the way
 * message-sender appends it, and the user leaves before any fold has adopted
 * it. The cached transcript the switch back restores must hold the echo, and a
 * send refused after the user left must not stay in that cache as a ghost.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const sse = vi.hoisted(() => ({
	onEvent: null as ((e: Record<string, unknown>) => void) | null,
}));

vi.mock('./v2/sse-feed', () => ({
	createSseFeed: (_sessionId: string, opts: { onEvent: (e: Record<string, unknown>) => void }) => {
		sse.onEvent = opts.onEvent;
		return { running: true, lastSeq: 0, epoch: 'epoch-1', revive() {}, stop() {} };
	},
}));
vi.mock('../app-data.svelte', () => ({
	appData: { get current() { return { sessions: [] }; } },
}));
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null) },
}));
vi.mock('$lib/services/error-reporter', () => ({ reportClientError: vi.fn() }));
vi.mock('$lib/services/error-actions', () => ({ serverNotificationActions: vi.fn(() => []) }));
vi.mock('$lib/services/fix-with-ai', () => ({ startFixChat: vi.fn() }));
vi.mock('$lib/stores/support-report.svelte', () => ({
	canSendSupportReport: vi.fn(() => true),
	canFixWithAi: vi.fn(() => false),
	openSupportReport: vi.fn(),
}));
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

import { createSessionSwitcher } from './session-switcher';
import { createMessageHandlers, type InternalMutableState, type MessageHandlerDeps } from './ws-message-handlers';
import { feedController } from './v2/feed-controller';
import { pendingQueue } from './pending-queue';
import type { ActiveSessionState } from '../active-session';
import type { SessionEphemeral } from '../session-cache';

const A = 'session-A';
const B = 'session-B';
const CMD = 'client-' + 'a'.repeat(32);
const AT = '2026-11-10T07:00:00.000Z';

function baseState(): ActiveSessionState {
	return {
		worktreeCheckoutPending: false,
		sessionId: null, mode: 'code', title: '', messages: [], isStreaming: false,
		liveTurnId: null, liveModel: null, liveRequestedModel: null, liveProvider: null,
		wsConnected: true, wsReconnectRequired: false, wsManualReconnect: 'idle', error: null, profile: 'default',
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
/** Message count of every published state, oldest first: the frames a view could paint. */
let counts: number[];

function harness() {
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => {
		state = fn(state);
		counts.push(state.messages.length);
	};
	const getState = () => state;
	const wsSend = vi.fn(() => true);
	const internal: InternalMutableState = {
		pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null,
	};
	const ephemeral = new Map<string, SessionEphemeral>();
	feedController.attach({
		update, getState,
		requestPendingQuestions: vi.fn(), requestPendingPermissions: vi.fn(),
		resolveInteractionByRequestId: vi.fn(),
	});
	const switcher = createSessionSwitcher({
		update, getState, wsSend, internal,
		sessionEphemeralCache: { save: (id, e) => { ephemeral.set(id, e); }, restore: (id) => ephemeral.get(id) ?? null },
		onPersistTimerChanged: vi.fn(),
	});
	const { handleMessage } = createMessageHandlers({
		update, getState, wsSend,
		streamingSessions: { update: vi.fn(), set: vi.fn() },
		backgroundPermissions: { update: vi.fn() },
		gitRefreshStatus: vi.fn(), gitRefreshDiffStats: vi.fn(), gitRefreshPrState: vi.fn(),
		gitGetProjectId: vi.fn(() => null), gitGetLoading: vi.fn(() => false),
		appLoadSessions: vi.fn(), appLoadApiUserSessions: vi.fn(), mergeSessionPatch: vi.fn(() => true),
		internal, onNewChat: vi.fn(), onPersistSession: vi.fn(), onNotifySessionComplete: vi.fn(),
		onNotifyWaitingForResponse: vi.fn(), wsIsConnected: () => true, clearStopTimeout: vi.fn(),
	} as unknown as MessageHandlerDeps);
	return { switcher, handleMessage };
}

/** Hydrate `sessionId` with `n` settled user/assistant rows and a real sentinel. */
function hydrate(sessionId: string, n: number) {
	let seq = 0;
	for (let i = 0; i < n; i++) {
		seq++;
		const base = { id: `${sessionId}-ev-${seq}`, seq, session_id: sessionId, created_at: AT };
		sse.onEvent!(i % 2 === 0
			? { ...base, type: 'message.user', turn_id: `${sessionId}-t${i}`, content: `${sessionId} question ${i}` }
			: { ...base, type: 'message.assistant', turn_id: `${sessionId}-t${i - 1}`, content: `${sessionId} answer ${i}` });
	}
	sse.onEvent!({ type: 'caught_up', seq });
}

/** The local echo exactly as message-sender appends it, receipt not yet seen. */
function appendLocalSend(content: string) {
	feedController.noteOptimisticSend(state.sessionId);
	state = { ...state,
		messages: [...state.messages, {
			id: `user-${CMD}`, role: 'user', content, commandId: CMD, timestamp: AT,
		} as ActiveSessionState['messages'][number]],
		optimisticStreaming: true, isStreaming: true,
	};
	counts.push(state.messages.length);
}

beforeEach(() => {
	vi.useFakeTimers();
	state = baseState();
	counts = [];
	pendingQueue.reset(null, true);
});

afterEach(() => {
	feedController.stop();
	feedController.invalidate();
	pendingQueue.reset(null, true);
	vi.useRealTimers();
});

describe('switch back right after a send (#1142)', () => {
	it('keeps the just-sent message when the user leaves before the next fold and comes straight back', async () => {
		const { switcher } = harness();
		await switcher.switchSession(A);
		hydrate(A, 9);
		await switcher.switchSession(B);
		hydrate(B, 4);
		await switcher.switchSession(A);
		expect(state.messages).toHaveLength(9);
		// The resumed feed has nothing newer than the cached cursor.
		sse.onEvent!({ type: 'caught_up', seq: 9 });

		appendLocalSend('ten');
		expect(state.messages).toHaveLength(10);
		// Leave before any fold adopts the echo, then come straight back.
		await switcher.switchSession(B);
		counts = [];
		await switcher.switchSession(A);

		expect(state.sessionId).toBe(A);
		expect(state.messages).toHaveLength(10);
		expect(state.messages.at(-1)?.commandId).toBe(CMD);
		// No frame of A painted one short on the way back.
		expect(counts.filter(n => n > 0 && n < 10)).toEqual([]);

		// The held receipt lands: reconciled in place, never a duplicate.
		sse.onEvent!({ type: 'message.user', id: 'A-ev-10', seq: 10, session_id: A, created_at: AT,
			turn_id: 'A-t9', content: 'ten', command_id: CMD });
		vi.advanceTimersByTime(300);
		expect(state.messages).toHaveLength(10);
		expect(state.messages.filter(m => m.commandId === CMD)).toHaveLength(1);
	});

	it('leaves no ghost in the cache when the held send is refused after the user left', async () => {
		const { switcher, handleMessage } = harness();
		await switcher.switchSession(A);
		hydrate(A, 9);
		appendLocalSend('ten');
		await switcher.switchSession(B);
		hydrate(B, 4);
		expect(feedController.cachedPatch(A)?.messages?.some(m => m.commandId === CMD)).toBe(true);

		// The worker refuses the send while B is open.
		handleMessage({ type: 'error', session_id: A, client_message_id: CMD, message: 'Missing agent' });
		expect(state.sessionId).toBe(B);
		expect(feedController.cachedPatch(A)?.messages?.some(m => m.commandId === CMD)).toBe(false);

		await switcher.switchSession(A);
		expect(state.messages).toHaveLength(9);
		expect(state.messages.some(m => m.commandId === CMD)).toBe(false);
		// The refused words come back to A's composer, not as a transcript row.
		expect(state.pendingRestorePrompt).toBe('ten');
	});
});
