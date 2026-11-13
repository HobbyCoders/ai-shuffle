/**
 * #1499: a send the worker refuses before any turn starts must leave the
 * composer idle (Send, not Stop). Covers the send with a local optimistic row
 * (an ordinary idle send) and the one without (the first send into a freshly
 * created worktree chat, `skipMessageAppend`), on the REAL feed controller and
 * message handlers: the store and the feed's own optimistic latch both drop,
 * and a later fold does not raise the flag again.
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
const post = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null), post, delete: vi.fn(async () => null) },
}));
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'feat/auto-name' })),
	generateBranchNameLocal: vi.fn(() => 'local/auto-name'),
}));
vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
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
import { createMessageSender } from './message-sender';
import { createMessageHandlers, type InternalMutableState, type MessageHandlerDeps } from './ws-message-handlers';
import { feedController, OPTIMISTIC_STREAMING_ON } from './v2/feed-controller';
import { pendingQueue } from './pending-queue';
import { hasStoppableWork } from '$lib/components/chat/stoppable-work';
import type { ActiveSessionState } from '../active-session';
import type { SessionEphemeral } from '../session-cache';

const A = 'session-A';
const CMD = 'client-' + 'r'.repeat(32);
const AT = '2026-11-10T07:00:00.000Z';
const REFUSAL = "The preview link can't send to a chat another worker runs.";

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

function harness() {
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
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
	// The real sender, as activeSession wires it (its nested sends call itself).
	const wsConn = { isConnected: vi.fn(() => true), connect: vi.fn(), resetReconnectAttempts: vi.fn(),
		send: vi.fn((_frame: Record<string, unknown>) => true) };
	const sender: ReturnType<typeof createMessageSender> = createMessageSender({
		update, getState, internal, wsConn,
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => sender,
	});
	return { switcher, handleMessage, sender, wsConn };
}

/** Two settled rows and a real sentinel: an idle chat. */
function hydrate(sessionId: string) {
	sse.onEvent!({ type: 'message.user', id: 'ev-1', seq: 1, session_id: sessionId, created_at: AT, turn_id: 't0', content: 'q' });
	sse.onEvent!({ type: 'message.assistant', id: 'ev-2', seq: 2, session_id: sessionId, created_at: AT, turn_id: 't0', content: 'a' });
	sse.onEvent!({ type: 'caught_up', seq: 2 });
	vi.advanceTimersByTime(300);
}

/** The send as message-sender raises it: the flip on both channels, the send in this page's run. */
function send(withLocalRow: boolean) {
	feedController.noteOptimisticSend(state.sessionId);
	state = { ...state,
		...(withLocalRow ? { messages: [...state.messages, {
			id: `user-${Date.now()}`, role: 'user', content: 'refused words', commandId: CMD, timestamp: AT,
		} as ActiveSessionState['messages'][number]] } : {}),
		...OPTIMISTIC_STREAMING_ON,
		streamStartTime: Date.now(),
		localSendIntent: { sessionId: state.sessionId, revision: 1, commandIds: [CMD] },
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	state = baseState();
	pendingQueue.reset(null, true);
});

afterEach(() => {
	feedController.stop();
	feedController.invalidate();
	pendingQueue.reset(null, true);
	vi.useRealTimers();
});

describe('a send refused before any turn starts (#1499)', () => {
	it.each([
		['with a local optimistic row', true],
		['without a local row (worktree first send)', false],
	])('leaves the composer idle %s', async (_label, withLocalRow) => {
		const { switcher, handleMessage } = harness();
		await switcher.switchSession(A);
		hydrate(A);
		expect(state.isStreaming).toBe(false);

		send(withLocalRow);
		expect(hasStoppableWork(state)).toBe(true);

		handleMessage({ type: 'error', session_id: A, client_message_id: CMD, message: REFUSAL });
		expect(state.error).toContain('Message not sent');
		expect(state.isStreaming).toBe(false);
		expect(state.optimisticStreaming).toBe(false);
		expect(state.streamStartTime).toBeNull();
		expect(state.localSendIntent?.commandIds).toEqual([]);
		expect(hasStoppableWork(state)).toBe(false);

		// The feed's own latch is down too: the next fold does not raise it again.
		sse.onEvent!({ type: 'caught_up', seq: 2 });
		vi.advanceTimersByTime(300);
		expect(state.isStreaming).toBe(false);
		expect(hasStoppableWork(state)).toBe(false);
	});

	it('keeps Stop for a turn that really started before the refusal of another send', async () => {
		const { switcher, handleMessage } = harness();
		await switcher.switchSession(A);
		hydrate(A);
		send(false);
		sse.onEvent!({ type: 'turn.started', id: 'ev-3', seq: 3, session_id: A, created_at: AT, turn_id: 't1' });
		vi.advanceTimersByTime(300);
		expect(state.isStreaming).toBe(true);

		handleMessage({ type: 'error', session_id: A, client_message_id: CMD, message: REFUSAL });
		expect(state.isStreaming).toBe(true);
		expect(hasStoppableWork(state)).toBe(true);
	});

	// Review S2: the worker announced another turn on the socket (WS `start`)
	// before its feed shows it. Refusing the idle send with a local row must not
	// take that turn's Stop away (#1258: a WS start can run ahead of the feed).
	it('keeps Stop for a turn announced on the socket before its feed, when an idle send with a row is refused', async () => {
		const { switcher, handleMessage } = harness();
		await switcher.switchSession(A);
		hydrate(A);
		send(true);
		handleMessage({ type: 'start', session_id: A });
		expect(hasStoppableWork(state)).toBe(true);

		handleMessage({ type: 'error', session_id: A, client_message_id: CMD, message: REFUSAL });
		expect(state.messages.some(m => m.commandId === CMD)).toBe(false);
		expect(state.error).toContain('Message not sent');
		expect(state.isStreaming).toBe(true);
		expect(hasStoppableWork(state)).toBe(true);

		// A fold before the feed shows the turn keeps it; the feed then confirms it.
		vi.advanceTimersByTime(300);
		expect(hasStoppableWork(state)).toBe(true);
		sse.onEvent!({ type: 'turn.started', id: 'ev-3', seq: 3, session_id: A, created_at: AT, turn_id: 't1' });
		vi.advanceTimersByTime(300);
		expect(state.isStreaming).toBe(true);
		expect(hasStoppableWork(state)).toBe(true);
	});
});

// Review S1: the auto-worktree fallback (the backend created the chat but did
// not start its turn) sends the first turn over the socket with no local row
// of its own. Its refusal must still leave the composer idle.
describe('a refused auto-worktree fallback first send (#1499)', () => {
	it('leaves the composer idle', async () => {
		post.mockReset();
		post.mockResolvedValueOnce({ session_id: 'wt-fb', branch_name: 'feat/auto-name', base_branch: 'main',
			worktree_path: '/w', status: 'ok', query_started: false });
		state = { ...baseState(), project: 'proj-1', worktreeIntent: 'worktree' };
		const { handleMessage, sender, wsConn } = harness();

		const sending = sender.sendMessage('refused words');
		await vi.advanceTimersByTimeAsync(50);
		expect(await sending).toBe(true);
		const query = wsConn.send.mock.calls.map(([f]) => f).find(f => f.type === 'query');
		expect(query).toMatchObject({ session_id: 'wt-fb' });
		expect(state.sessionId).toBe('wt-fb');
		expect(hasStoppableWork(state)).toBe(true);

		handleMessage({ type: 'error', session_id: 'wt-fb', client_message_id: query!.client_message_id, message: REFUSAL });
		expect(state.error).toContain('Message not sent');
		expect(state.isStreaming).toBe(false);
		expect(state.optimisticStreaming).toBe(false);
		expect(state.streamStartTime).toBeNull();
		expect(hasStoppableWork(state)).toBe(false);

		// The feed's history arrives: still idle.
		sse.onEvent!({ type: 'caught_up', seq: 0 });
		vi.advanceTimersByTime(300);
		expect(state.isStreaming).toBe(false);
		expect(hasStoppableWork(state)).toBe(false);
	});
});
