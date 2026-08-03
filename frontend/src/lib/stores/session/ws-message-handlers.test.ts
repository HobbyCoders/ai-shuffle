/**
 * Tests for ws-message-handlers.ts — Message dispatch and key handler behavior.
 *
 * Architecture (SSE-fed): the SSE-fed reducer (feedController) owns the session
 * transcript and its streaming state — it is the sole writer of `messages` /
 * isStreaming for the active session. These handlers NEVER render stream content;
 * they adopt sessions (start the feed), manage permissions/questions and the
 * cross-session streaming map, and fire side-effect callbacks (completion
 * notifications, error banners).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createMessageHandlers, type MessageHandlerDeps, type InternalMutableState } from './ws-message-handlers';
import type { ActiveSessionState } from '../active-session';

// Mock the side-effect services the error handlers call. Keeps this a true unit
// test of message dispatch — isolated from the heavy active-session/layout
// module graph, and no real network POST or chat navigation fires. The services
// have their own dedicated tests (error-reporter.test.ts, fix-with-ai.test.ts).
vi.mock('$lib/services/error-reporter', () => ({ reportClientError: vi.fn() }));
vi.mock('$lib/services/fix-with-ai', () => ({ startFixChat: vi.fn() }));
// The unified notification funnel: assert the handler feeds server events into
// the store via `notify.ingest`, rather than rendering them itself. `notify.error`
// is used by the failed-answer-relay path to toast the user.
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn() } }));
// The SSE-fed reducer singleton. Session adoption (session_created/start) starts
// the feed; mock it so we can assert adoption without a real EventSource, and so
// isFeeding() reports "not yet feeding" (the branch that triggers start()).
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(),
		start: vi.fn(),
		stop: vi.fn(),
		isFeeding: vi.fn(() => false),
		currentSessionId: vi.fn(() => null),
	},
}));

import { notify } from '../notifications';
import { startFixChat } from '$lib/services/fix-with-ai';
import { feedController } from './v2/feed-controller';

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		pendingTools: new Set(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		stopBackgroundRequested: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
	};
}

function createMockDeps(overrides?: Partial<MessageHandlerDeps>): MessageHandlerDeps {
	return {
		update: vi.fn((fn) => fn(mockState())),
		getState: vi.fn(() => mockState()),
		wsSend: vi.fn(() => true),
		streamingSessions: {
			update: vi.fn(),
			set: vi.fn(),
		},
		backgroundPermissions: {
			update: vi.fn(),
		},
		gitRefreshStatus: vi.fn(),
		gitRefreshDiffStats: vi.fn(),
		gitGetProjectId: vi.fn(() => null),
		gitGetLoading: vi.fn(() => false),
		appLoadSessions: vi.fn(),
		appLoadApiUserSessions: vi.fn(),
		internal: createMockInternal(),
		onNewChat: vi.fn(),
		onPersistSession: vi.fn(),
		onNotifySessionComplete: vi.fn(),
		onNotifyWaitingForResponse: vi.fn(),
		wsIsConnected: vi.fn(() => true),
		clearStopTimeout: vi.fn(),
		...overrides,
	};
}

function mockState(): ActiveSessionState {
	return {
		sessionId: 'session-1',
		title: '',
		messages: [],
		isStreaming: false,
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: true,
		error: null,
		profile: 'default',
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: '',
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		contextMax: 0,
		modelOverride: null,
		permissionModeOverride: null,
		effortLevelOverride: null,
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		activeTasks: [],
		preserveBackgroundOnInterrupt: false,
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		worktreeIntent: 'local',
		worktreeBranch: '',
		worktreeBaseBranch: '',
		worktreeCreateNewBranch: true,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		pendingInterruptPrompt: null,
		isRestoringSession: false,
		isLoadingSession: false,
	};
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('ws-message-handlers', () => {
	let deps: MessageHandlerDeps;
	let handlers: ReturnType<typeof createMessageHandlers>;

	beforeEach(() => {
		(feedController.start as ReturnType<typeof vi.fn>).mockClear();
		(feedController.isFeeding as ReturnType<typeof vi.fn>).mockReturnValue(false);
		deps = createMockDeps();
		handlers = createMessageHandlers(deps);
	});

	describe('message dispatch', () => {
		it('should handle ping messages by sending pong', () => {
			handlers.handleMessage({ type: 'ping' });
			expect(deps.wsSend).toHaveBeenCalledWith({ type: 'pong' });
		});

		it('should handle unknown message types without throwing', () => {
			expect(() => {
				handlers.handleMessage({ type: 'totally_unknown_type' });
			}).not.toThrow();
		});

		it('should handle messages with no type without throwing', () => {
			expect(() => {
				handlers.handleMessage({});
			}).not.toThrow();
		});
	});

	describe('notification handler (unified funnel)', () => {
		// `notify.ingest` / `startFixChat` are module-level mocks; clear their call
		// history per-test so each can assert on `mock.calls[0]`.
		beforeEach(() => {
			(notify.ingest as ReturnType<typeof vi.fn>).mockClear();
			(startFixChat as ReturnType<typeof vi.fn>).mockClear();
		});

		it('feeds a server notification into the store via notify.ingest', () => {
			handlers.handleMessage({
				type: 'notification',
				notification: {
					id: 'srv-1',
					severity: 'success',
					category: 'session_complete',
					message: 'Session finished',
					session_id: 'session-1',
				},
			});

			expect(notify.ingest).toHaveBeenCalledTimes(1);
			const [payload, action] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(payload.id).toBe('srv-1');
			expect(payload.message).toBe('Session finished');
			// Non-api_error categories carry no rebuilt client action.
			expect(action).toBeUndefined();
		});

		it('strips the wire action descriptor before ingesting', () => {
			handlers.handleMessage({
				type: 'notification',
				notification: {
					id: 'srv-2',
					severity: 'error',
					category: 'api_error',
					message: 'API error: HTTP 500',
					action: { kind: 'fix_with_ai', payload: { status_code: 500 } },
				},
			});

			const [payload] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			// The server `{kind, payload}` descriptor can't render as a button, so it
			// must not be forwarded as part of the ingested payload.
			expect(payload.action).toBeUndefined();
		});

		it('rebuilds a Fix-with-AI action for api_error (non-429) and wires startFixChat', () => {
			handlers.handleMessage({
				type: 'notification',
				notification: {
					id: 'srv-3',
					severity: 'error',
					category: 'api_error',
					message: 'API error: HTTP 500',
					session_id: 'session-9',
					action: { kind: 'fix_with_ai', payload: { status_code: 500, session_id: 'session-9' } },
				},
			});

			const [payload, action] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(payload.duration_ms).toBe(10000);
			expect(action).toBeTruthy();
			expect(action.label).toBe('Fix with AI');

			action.onClick();
			expect(startFixChat).toHaveBeenCalledTimes(1);
			expect((startFixChat as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({
				status_code: 500,
				session_id: 'session-9',
			});
		});

		it('treats a 429 api_error as a transient warning with no Fix-with-AI action', () => {
			handlers.handleMessage({
				type: 'notification',
				notification: {
					id: 'srv-4',
					severity: 'warning',
					category: 'api_error',
					message: 'Rate limited — slow down or wait for the next window.',
				},
			});

			const [payload, action] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(payload.duration_ms).toBe(5000);
			expect(action).toBeUndefined();
		});

		it('ignores a notification message with no notification payload', () => {
			expect(() => handlers.handleMessage({ type: 'notification' })).not.toThrow();
			expect(notify.ingest).not.toHaveBeenCalled();
		});
	});

	describe('done handler', () => {
		it('should call onNotifySessionComplete with metadata', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({
				getState: vi.fn(() => state),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-1',
				metadata: {
					total_cost_usd: 0.01,
					tokens_in: 100,
					tokens_out: 50,
					model: 'claude-sonnet-4-20250514',
				},
			});

			expect(deps.onNotifySessionComplete).toHaveBeenCalledWith(
				expect.objectContaining({
					sessionId: 'session-1',
					totalCostUsd: 0.01,
				})
			);
		});

		it('clears any stale attention dot for the finished session', () => {
			const state = mockState();
			state.sessionId = 'session-active'; // a different session is active here
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'done', session_id: 'session-bg' });

			// A finished session can't be awaiting input. The done handler clears
			// its attention dot defensively, in case the backend count=0 broadcast
			// was missed — otherwise the dot (which now outranks the streaming dot)
			// stays lit until the user opens the session.
			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			expect(fn(new Map([['session-bg', 1]])).has('session-bg')).toBe(false);
		});

		// Queue processing is now handled server-side — no frontend trigger to test
	});

	describe('stopped handler', () => {
		it('should suppress when interruptPending is true', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = true;
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'stopped',
				session_id: 'session-1',
			});

			// When interruptPending is true the handler returns early
			expect(deps.internal.pendingTools.size).toBe(0);
		});

		it('clears any stale attention dot for the stopped session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped', session_id: 'session-1' });

			// Mirrors the done handler: a stopped/interrupted session can't be
			// awaiting input, so any leftover attention dot is cleared.
			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			expect(fn(new Map([['session-1', 1]])).has('session-1')).toBe(false);
		});

		it('does NOT clobber the active session when a BACKGROUND session stops', () => {
			// Bug 3: pre-fix, handleStoppedOrInterrupted ignored data.session_id and
			// always mutated the ACTIVE session — so a stopped/interrupted event for
			// any OTHER session wiped the active session's pendingTools. Route by the
			// event's session_id (mirror handleDone) so the active session's
			// active-only block is skipped for a background stop.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			deps.internal.pendingTools.add('tool-keep');
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped', session_id: 'session-bg' });

			// The active session is untouched: pending tools preserved, no stop-timeout
			// clear (the whole active-only block is skipped).
			expect(deps.internal.pendingTools.has('tool-keep')).toBe(true);
			expect(deps.clearStopTimeout).not.toHaveBeenCalled();
		});

		it('clears the active session pendingTools on its OWN stopped event', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			deps.internal.pendingTools.add('tool-x');
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped', session_id: 'session-1' });

			expect(deps.internal.pendingTools.has('tool-x')).toBe(false);
			expect(deps.clearStopTimeout).toHaveBeenCalled();
		});

		it('treats a stopped event with no session_id as the active session', () => {
			// Backward-compat: events that omit session_id still apply to the session
			// in view (matches handleDone's `!sessionId` fallback).
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			deps.internal.pendingTools.add('tool-y');
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped' });

			expect(deps.internal.pendingTools.has('tool-y')).toBe(false);
			expect(deps.clearStopTimeout).toHaveBeenCalled();
		});
	});

	describe('user_message_discarded handler', () => {
		it('should stash the discarded prompt for the active session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			let captured: string | null = '__unset__';

			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					captured = fn(state).pendingInterruptPrompt;
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_message_discarded',
				session_id: 'session-1',
				prompt: 'write me a haiku',
			});

			// Prompt is stashed so ChatInput can restore it to the editor. The SSE
			// feed drops the now-removed orphan user message from the transcript.
			expect(captured).toBe('write me a haiku');
		});

		it('should ignore events for a non-active session', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_message_discarded',
				session_id: 'some-other-session',
				prompt: 'not mine',
			});

			expect(deps.update).not.toHaveBeenCalled();
		});
	});

	describe('deferred Stop delivery (stopRequestedBeforeSession)', () => {
		// A new session has no id when the user clicks Stop, so stopGeneration
		// records the intent on internal.stopRequestedBeforeSession. The
		// session_created / start handlers must flush it once the backend mints
		// the id, then clear the flag so it never double-fires.
		// The deferred stop flushes only when the frame is ACTUALLY adopted, which
		// now requires the echoed adoption token to match (see the hijack fix). The
		// realistic flow always has a pendingRequestId set by sendMessage and the
		// backend echoes it, so the fixture carries a matching token.
		const TOKEN = 'req-defer-abc';
		function newSessionDeps() {
			const state = mockState();
			state.sessionId = null; // brand-new session, no id yet
			const d = createMockDeps({ getState: vi.fn(() => state) });
			d.internal.awaitingNewSession = true;
			d.internal.pendingRequestId = TOKEN;
			d.internal.stopRequestedBeforeSession = true;
			return d;
		}

		function stopSends(d: MessageHandlerDeps) {
			return (d.wsSend as ReturnType<typeof vi.fn>).mock.calls.filter(
				([m]) => (m as any)?.type === 'stop',
			);
		}

		it('session_created flushes a deferred stop and clears the flag', () => {
			deps = newSessionDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_created', session_id: 'sess-new', request_id: TOKEN });

			const sends = stopSends(deps);
			expect(sends).toHaveLength(1);
			expect((sends[0][0] as any).session_id).toBe('sess-new');
			expect(deps.internal.stopRequestedBeforeSession).toBe(false);
		});

		it('start flushes a deferred stop and clears the flag', () => {
			deps = newSessionDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'start', session_id: 'sess-new', message_id: 'm1', request_id: TOKEN });

			const sends = stopSends(deps);
			expect(sends).toHaveLength(1);
			expect((sends[0][0] as any).session_id).toBe('sess-new');
			expect(deps.internal.stopRequestedBeforeSession).toBe(false);
		});

		it('does not send a stop when none was deferred', () => {
			const state = mockState();
			state.sessionId = null;
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = TOKEN;
			// stopRequestedBeforeSession stays false
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_created', session_id: 'sess-new', request_id: TOKEN });

			expect(stopSends(deps)).toHaveLength(0);
		});
	});

	describe('new-session adoption authentication (hijack fix)', () => {
		// PRIMARY BUG: while a brand-new chat is awaiting its minted session
		// (sessionId=null, awaitingNewSession=true), the OLD adoption predicate
		// accepted ANY session_created / start frame. The device WS is shared, so a
		// FOREIGN session's server-initiated turn (unpark follow-up / queue drain)
		// unparking during that window got adopted: its sessionId, polls, messages
		// and running tasks flashed on the new chat's page. The fix authenticates
		// adoption with a client-generated request_id echoed by the backend in both
		// frames — a frame without the matching token can no longer be adopted.

		function awaitingState() {
			const state = mockState();
			state.sessionId = null; // brand-new chat, no id yet
			state.activeTasks = []; // clean board
			return state;
		}

		it('does NOT adopt a foreign start frame that lacks the matching request_id', () => {
			const state = awaitingState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = 'req-mine-123';
			handlers = createMessageHandlers(deps);

			// A foreign session's server-initiated start — NO request_id (deferred /
			// unpark turns never carry one).
			handlers.handleMessage({ type: 'start', session_id: 'other-session', message_id: 'm-foreign' });

			// Must NOT be adopted: state untouched, feed not started, still awaiting.
			expect(deps.update).not.toHaveBeenCalled();
			expect(feedController.start).not.toHaveBeenCalled();
			expect(deps.internal.awaitingNewSession).toBe(true);
			expect(deps.internal.pendingRequestId).toBe('req-mine-123');
		});

		it('does NOT adopt a foreign session_created frame that lacks the matching request_id', () => {
			const state = awaitingState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = 'req-mine-123';
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_created', session_id: 'other-session', title: 'Foreign' });

			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.internal.awaitingNewSession).toBe(true);
			expect(deps.internal.pendingRequestId).toBe('req-mine-123');
		});

		it('does NOT adopt a frame whose request_id mismatches ours', () => {
			const state = awaitingState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = 'req-mine-123';
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'start', session_id: 'other-session', request_id: 'req-someone-else' });

			expect(deps.update).not.toHaveBeenCalled();
			expect(feedController.start).not.toHaveBeenCalled();
			expect(deps.internal.awaitingNewSession).toBe(true);
		});

		it('adopts a session_created with the matching request_id AND resets activeTasks to []', () => {
			const state = awaitingState();
			state.activeTasks = [
				{ id: 'inherited', subject: 'stale', status: 'running', kind: 'shell', summary: null, startedAt: '', updatedAt: '' },
			] as ActiveSessionState['activeTasks'];
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = 'req-mine-123';
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'session_created',
				session_id: 'my-session',
				title: 'Mine',
				request_id: 'req-mine-123',
			});

			expect(applied?.sessionId).toBe('my-session');
			// activeTasks reset so no prior/foreign running-task strip flashes.
			expect(applied?.activeTasks).toEqual([]);
			// Awaiting flag + token cleared so a later foreign frame can't re-adopt.
			expect(deps.internal.awaitingNewSession).toBe(false);
			expect(deps.internal.pendingRequestId).toBeNull();
		});

		it('adopts a start with the matching request_id, resets activeTasks, and starts the feed', () => {
			const state = awaitingState();
			state.activeTasks = [
				{ id: 'inherited', subject: 'stale', status: 'running', kind: 'shell', summary: null, startedAt: '', updatedAt: '' },
			] as ActiveSessionState['activeTasks'];
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			deps.internal.awaitingNewSession = true;
			deps.internal.pendingRequestId = 'req-mine-123';
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'start',
				session_id: 'my-session',
				message_id: 'm1',
				request_id: 'req-mine-123',
			});

			expect(applied?.sessionId).toBe('my-session');
			expect(applied?.activeTasks).toEqual([]);
			expect(feedController.start).toHaveBeenCalledWith('my-session');
			expect(deps.internal.awaitingNewSession).toBe(false);
			expect(deps.internal.pendingRequestId).toBeNull();
		});

		it('still adopts a start for the ALREADY-open session without a request_id (existing-session turn)', () => {
			// An existing-session query (e.g. interrupt_for_query follow-up) routes by
			// the known sessionId and needs no token. Its start must still be accepted
			// and must NOT wipe the live task strip (sessionId unchanged).
			const state = mockState();
			state.sessionId = 'session-1';
			state.activeTasks = [
				{ id: 'live', subject: 'running work', status: 'running', kind: 'agent', summary: null, startedAt: '', updatedAt: '' },
			] as ActiveSessionState['activeTasks'];
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'start', session_id: 'session-1', message_id: 'm2' });

			expect(applied?.sessionId).toBe('session-1');
			// sessionId did NOT change → activeTasks preserved (reset is adoption-only).
			expect(applied?.activeTasks).toHaveLength(1);
			expect(feedController.start).toHaveBeenCalledWith('session-1');
		});
	});

	describe('error handler', () => {
		it('should set error on the store', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			let capturedError: string | null = null;

			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					capturedError = result.error as string | null;
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'error',
				session_id: 'session-1',
				message: 'Something went wrong',
			});

			expect(deps.update).toHaveBeenCalled();
			expect(capturedError).toBe('Something went wrong');
		});

		it('clears interrupt state and resets streaming on its OWN error', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = true;
			let rejected: unknown = '__unset__';
			deps.internal.interruptConfirmResolve = (v: boolean) => { rejected = v; };
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'error',
				session_id: 'session-1',
				message: 'boom',
			});

			// Active error: banner shown + isStreaming reset (so the user can send
			// again), interrupt-and-query promise rejected and the flag cleared.
			expect(deps.update).toHaveBeenCalled();
			expect(deps.internal.interruptPending).toBe(false);
			expect(rejected).toBe(false);
			expect(deps.internal.interruptConfirmResolve).toBeNull();
		});

		it('does NOT clobber the active session when a BACKGROUND session errors', () => {
			// Frontend U1: pre-fix, the error handler ignored data.session_id and
			// always reset the ACTIVE session's isStreaming and rejected its
			// interrupt-and-query promise — so a background session's stream error
			// froze whatever was in view. Mirror handleDone / handleStoppedOrInterrupted:
			// route by the event's session_id.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = true;
			let resolverCalled = false;
			deps.internal.interruptConfirmResolve = () => { resolverCalled = true; };
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'error',
				session_id: 'session-bg',
				message: 'background blew up',
			});

			// The errored (background) session's streaming pill is dropped — cross-session safe.
			expect(deps.streamingSessions.update).toHaveBeenCalled();
			// The active session is untouched: no banner, no isStreaming reset, and the
			// interrupt machinery is preserved (the whole active-only block is skipped).
			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.internal.interruptPending).toBe(true);
			expect(resolverCalled).toBe(false);
			expect(deps.internal.interruptConfirmResolve).not.toBeNull();
		});

		it('treats an error with no session_id as the active session', () => {
			// Backward-compat: a synchronous pre-stream command error (e.g. an empty
			// prompt) carries no session_id and still applies to the session in view.
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'error', message: 'empty prompt' });

			expect(deps.update).toHaveBeenCalled();
		});
	});

	describe('tool tracking', () => {
		it('should add tool_id to pendingTools on tool_use', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'tool_use',
				session_id: 'session-1',
				id: 'tool-abc',
				name: 'Read',
			});

			expect(deps.internal.pendingTools.has('tool-abc')).toBe(true);
		});

		it('should remove tool_id from pendingTools on tool_result', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.pendingTools.add('tool-abc');
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'tool_result',
				session_id: 'session-1',
				tool_use_id: 'tool-abc',
			});

			expect(deps.internal.pendingTools.has('tool-abc')).toBe(false);
		});
	});

	describe('permission handling', () => {
		it('should add permission request to pending list', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];

			let updatedPermissions: unknown[] = [];
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					if (result.pendingPermissions) {
						updatedPermissions = result.pendingPermissions as unknown[];
					}
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'permission_request',
				session_id: 'session-1',
				request_id: 'perm-1',
				tool_name: 'Bash',
				tool_input: { command: 'ls' },
			});

			expect(deps.update).toHaveBeenCalled();
			expect(updatedPermissions.length).toBe(1);
		});

		it('should keep permission requests for inactive sessions out of the active modal', () => {
			const state = mockState();
			state.sessionId = 'session-2';
			state.pendingPermissions = [];

			deps = createMockDeps({
				getState: vi.fn(() => state),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'permission_request',
				session_id: 'session-1',
				request_id: 'perm-1',
				tool_name: 'Bash',
				tool_input: { command: 'ls' },
			});

			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.backgroundPermissions.update).toHaveBeenCalled();
		});

		it('ignores the duplicate that arrives on the user-wide fan-out leg', () => {
			// The server now delivers every prompt twice by design: once on the
			// origin socket and once on the user-wide device fan-out (both legs are
			// best-effort, so neither can be dropped). The echo must not double the
			// modal queue or fire a second "waiting for response" notification.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];

			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					state.pendingPermissions = result.pendingPermissions ?? state.pendingPermissions;
				}),
			});
			handlers = createMessageHandlers(deps);

			const frame = {
				type: 'permission_request',
				session_id: 'session-1',
				request_id: 'perm-dup',
				tool_name: 'Bash',
				tool_input: { command: 'ls' },
			};
			handlers.handleMessage({ ...frame });
			handlers.handleMessage({ ...frame });

			expect(state.pendingPermissions.length).toBe(1);
			expect(deps.update).toHaveBeenCalledTimes(1);
			expect(deps.onNotifyWaitingForResponse).toHaveBeenCalledTimes(1);
		});

		it('still queues a genuinely different request for the same session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];

			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					state.pendingPermissions = result.pendingPermissions ?? state.pendingPermissions;
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'permission_request', session_id: 'session-1',
				request_id: 'perm-a', tool_name: 'Bash', tool_input: { command: 'ls' },
			});
			handlers.handleMessage({
				type: 'permission_request', session_id: 'session-1',
				request_id: 'perm-b', tool_name: 'Write', tool_input: { file_path: '/tmp/x' },
			});

			expect(state.pendingPermissions.map(p => p.request_id)).toEqual(['perm-a', 'perm-b']);
		});
	});

	describe('pending_permissions handler (reconnect replay)', () => {
		// On WS reconnect, active-session re-requests get_pending_permissions so a
		// permission modal that was emitted while disconnected pops up without a
		// session switch. The server replies with `pending_permissions`. This must
		// REPLACE the active queue (idempotent) so re-requesting never double-shows
		// an already-visible modal.
		it('replaces the active permission queue with the server set', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [
				{
					session_id: 'session-1',
					request_id: 'perm-1',
					tool_name: 'Bash',
					tool_input: { command: 'ls' },
				} as never,
			];

			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					updated = result.pendingPermissions as unknown[];
				}),
			});
			handlers = createMessageHandlers(deps);

			// Re-broadcast of the SAME request_id (as happens on reconnect replay).
			handlers.handleMessage({
				type: 'pending_permissions',
				session_id: 'session-1',
				requests: [
					{ request_id: 'perm-1', tool_name: 'Bash', tool_input: { command: 'ls' } },
				],
			});

			// Replaced (length 1), not appended (would be 2 → duplicate modal).
			expect(updated?.length).toBe(1);
		});

		it('leaves the open permission modal untouched on a stale reply (cluster relay failed)', () => {
			// Cross-install bug: on every reconnect/session-switch the client resyncs
			// via get_pending_permissions. If the relay to the owning install fails, the
			// backend replies `stale: true` with a non-authoritative (empty) list.
			// Replacing pendingPermissions with it would wipe a locally-open permission
			// modal. The handler must skip the replace.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [
				{
					session_id: 'session-1',
					request_id: 'perm-open',
					tool_name: 'Bash',
					tool_input: { command: 'rm -rf /' },
				} as never,
			];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'pending_permissions',
				session_id: 'session-1',
				pending: [], // non-authoritative fallback
				deferred: [],
				stale: true,
			});

			// No state write at all — the modal survives.
			expect(deps.update).not.toHaveBeenCalled();
		});

		it('an authoritative empty reply (no marker) STILL clears the permission modal', () => {
			// The deliberate stale-modal-clearing path: a SUCCESSFUL relay with zero
			// items omits the marker and must replace (empty out) pendingPermissions.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [
				{
					session_id: 'session-1',
					request_id: 'perm-open',
					tool_name: 'Bash',
					tool_input: { command: 'rm -rf /' },
				} as never,
			];

			let updatedPerms: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					updatedPerms = result.pendingPermissions as unknown[];
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'pending_permissions',
				session_id: 'session-1',
				pending: [], // authoritative empty — no `stale` marker
				deferred: [],
			});

			expect(updatedPerms?.length).toBe(0);
		});
	});

	describe('pending_questions handler (reconnect replay)', () => {
		it('replaces the active question queue with the server set', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingQuestions = [
				{
					session_id: 'session-1',
					request_id: 'q-1',
					tool_use_id: 'tool-1',
					questions: [],
				} as never,
			];

			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					updated = result.pendingQuestions as unknown[];
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'pending_questions',
				session_id: 'session-1',
				questions: [
					{ request_id: 'q-1', tool_use_id: 'tool-1', questions: [] },
				],
			});

			expect(updated?.length).toBe(1);
		});

		it('leaves the open modal untouched on a stale reply (cluster relay failed)', () => {
			// Cross-install bug: on every reconnect/session-switch the client resyncs
			// via get_pending_questions. If the relay to the owning install fails, the
			// backend replies `stale: true` with a non-authoritative (empty) list.
			// Replacing pendingQuestions with it would wipe a locally-open question
			// modal and its in-progress answer. The handler must skip the replace.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingQuestions = [
				{
					session_id: 'session-1',
					request_id: 'q-open',
					tool_use_id: 'tool-1',
					questions: [],
				} as never,
			];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'pending_questions',
				session_id: 'session-1',
				questions: [], // non-authoritative fallback
				stale: true,
			});

			// No state write at all — the modal (and its in-progress answer) survives.
			expect(deps.update).not.toHaveBeenCalled();
		});

		it('an authoritative empty reply (no marker) STILL clears the modal', () => {
			// The deliberate stale-modal-clearing path: a SUCCESSFUL relay with zero
			// questions omits the marker and must replace (empty out) pendingQuestions.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingQuestions = [
				{
					session_id: 'session-1',
					request_id: 'q-open',
					tool_use_id: 'tool-1',
					questions: [],
				} as never,
			];

			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					updated = result.pendingQuestions as unknown[];
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'pending_questions',
				session_id: 'session-1',
				questions: [], // authoritative empty — no `stale` marker
			});

			expect(updated?.length).toBe(0);
		});
	});

	describe('user_question_response_ack handler (relay success/failure)', () => {
		beforeEach(() => {
			(notify.error as ReturnType<typeof vi.fn>).mockClear();
		});

		function stateWithOpenQuestion() {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingQuestions = [
				{ session_id: 'session-1', request_id: 'q-1', tool_use_id: 'tool-1', questions: [] } as never,
			];
			return state;
		}

		it('removes the question on a successful ack', () => {
			const state = stateWithOpenQuestion();
			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { updated = fn(state).pendingQuestions as unknown[]; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question_response_ack',
				session_id: 'session-1',
				request_id: 'q-1',
				success: true,
			});

			expect(updated).toEqual([]);
			expect(notify.error).not.toHaveBeenCalled();
		});

		it('keeps the question AND toasts an error on a retryable relay failure (reason: relay_offline)', () => {
			// Cross-install bug: a failed answer relay (owner install offline) was acked
			// with success:false, but the handler removed the question anyway — dismissing
			// the modal as if answered while the owning waiter kept blocking (answer lost).
			// The fix keeps the modal up (removal is ONLY here) and surfaces the failure so
			// the user can retry.
			const state = stateWithOpenQuestion();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question_response_ack',
				session_id: 'session-1',
				request_id: 'q-1',
				success: false,
				reason: 'relay_offline',
			});

			// Modal preserved: NO removal write happened.
			expect(deps.update).not.toHaveBeenCalled();
			// User is told the delivery failed.
			expect(notify.error).toHaveBeenCalledTimes(1);
			expect((notify.error as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatch(/deliver your answer/i);
		});

		it('dismisses the question on a not_pending failure (owner has no such question)', () => {
			// Regression: an ack with success:false but reason:'not_pending' means the
			// owner authoritatively has no pending question (already answered elsewhere
			// or timed out). Keeping the modal would strand the user on a question
			// nothing is waiting for — it must be removed, without a relay-error toast.
			const state = stateWithOpenQuestion();
			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { updated = fn(state).pendingQuestions as unknown[]; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question_response_ack',
				session_id: 'session-1',
				request_id: 'q-1',
				success: false,
				reason: 'not_pending',
			});

			expect(updated).toEqual([]);
			expect(notify.error).not.toHaveBeenCalled();
		});

		it('dismisses the question on a legacy failed ack without a reason', () => {
			// A reason-less failure must behave like pre-relay code (dismiss), not
			// latch the modal open forever.
			const state = stateWithOpenQuestion();
			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { updated = fn(state).pendingQuestions as unknown[]; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question_response_ack',
				session_id: 'session-1',
				request_id: 'q-1',
				success: false,
			});

			expect(updated).toEqual([]);
			expect(notify.error).not.toHaveBeenCalled();
		});

		it('user_question_timeout still removes unconditionally (no success field)', () => {
			// The timeout type SHARES this handler and carries no success field. It must
			// keep removing the question (the turn ended) and must NOT toast a relay error.
			const state = stateWithOpenQuestion();
			let updated: unknown[] | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { updated = fn(state).pendingQuestions as unknown[]; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question_timeout',
				session_id: 'session-1',
				request_id: 'q-1',
			});

			expect(updated).toEqual([]);
			expect(notify.error).not.toHaveBeenCalled();
		});
	});

	describe('question handling', () => {
		it('should keep user questions for inactive sessions out of the active modal', () => {
			const state = mockState();
			state.sessionId = 'session-2';
			state.pendingQuestions = [];

			deps = createMockDeps({
				getState: vi.fn(() => state),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question',
				session_id: 'session-1',
				request_id: 'question-1',
				tool_use_id: 'tool-1',
				questions: [{ question: 'Proceed?', header: 'Confirm' }],
			});

			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.backgroundPermissions.update).toHaveBeenCalled();
		});

		it('should not duplicate a question already in the active queue (resync race)', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingQuestions = [
				{ request_id: 'question-1', tool_use_id: 'tool-1', questions: [] },
			] as unknown as ActiveSessionState['pendingQuestions'];

			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_question',
				session_id: 'session-1',
				request_id: 'question-1',
				tool_use_id: 'tool-1',
				questions: [{ question: 'Proceed?', header: 'Confirm' }],
			});

			expect(applied?.pendingQuestions).toHaveLength(1);
		});
	});

	describe('session_attention handler', () => {
		// Applies the function passed to backgroundPermissions.update against a
		// starting Map so we can assert the resulting attention state.
		function applyAttention(start: Map<string, number>): Map<string, number> {
			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			return fn(start);
		}

		it('sets attention for a background session', () => {
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_attention', session_id: 'session-bg', count: 2 });

			expect(deps.backgroundPermissions.update).toHaveBeenCalled();
			expect(applyAttention(new Map()).get('session-bg')).toBe(2);
		});

		it('resyncs pending queues for the active session instead of raising a dot', () => {
			// session_attention for the ACTIVE session must NOT raise a background
			// dot (the modal owns the surface) but MUST trigger a resync: it is the
			// one signal that survives a dead origin socket / headless / other-device
			// question, where the per-socket user_question event never arrived.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];
			state.pendingQuestions = [];
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_attention', session_id: 'session-1', count: 1 });

			expect(deps.backgroundPermissions.update).not.toHaveBeenCalled();
			expect(deps.wsSend).toHaveBeenCalledWith(
				expect.objectContaining({ type: 'get_pending_questions' })
			);
			expect(deps.wsSend).toHaveBeenCalledWith(
				expect.objectContaining({ type: 'get_pending_permissions' })
			);
		});

		it('resync + pending_questions reply populates the active modal', () => {
			// End-to-end recovery: attention → resync request → server reply lands a
			// question into pendingQuestions so the modal (pendingQuestions.length>0)
			// renders even though the live user_question event was lost.
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];
			state.pendingQuestions = [];

			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_attention', session_id: 'session-1', count: 1 });
			expect(deps.wsSend).toHaveBeenCalledWith(
				expect.objectContaining({ type: 'get_pending_questions' })
			);

			handlers.handleMessage({
				type: 'pending_questions',
				session_id: 'session-1',
				questions: [{ request_id: 'q-1', tool_use_id: 'tool-1', questions: [] }],
			});

			expect(applied?.pendingQuestions).toHaveLength(1);
			expect(applied?.pendingQuestions[0].request_id).toBe('q-1');
		});

		it('clears attention on count 0 even for the active session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_attention', session_id: 'session-1', count: 0 });

			expect(deps.backgroundPermissions.update).toHaveBeenCalled();
			expect(applyAttention(new Map([['session-1', 1]])).has('session-1')).toBe(false);
		});
	});

	describe('heartbeat handler', () => {
		it('should request pending permissions when heartbeat reports missed ones', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true;
			state.pendingPermissions = [];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'heartbeat',
				pending_permissions: 2,
				pending_questions: 0,
			});

			expect(deps.wsSend).toHaveBeenCalledWith(
				expect.objectContaining({ type: 'get_pending_permissions' })
			);
		});
	});

	describe('user_streaming_sessions handler', () => {
		it('should bulk-set streaming sessions', () => {
			deps = createMockDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'user_streaming_sessions',
				sessions: [
					{ session_id: 's1', message_id: 'm1', project_id: 'p1' },
					{ session_id: 's2', message_id: 'm2', project_id: null },
				],
				session_ids: ['s1', 's2'],
			});

			expect(deps.streamingSessions.set).toHaveBeenCalled();
		});
	});

	describe('session_not_found handler', () => {
		it('should trigger new chat when current session is not found', () => {
			const state = mockState();
			state.sessionId = 'missing-session';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'session_not_found',
				session_id: 'missing-session',
			});

			expect(deps.onNewChat).toHaveBeenCalled();
		});
	});

	describe('start handler', () => {
		it('should start the SSE feed and persist session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = false;

			deps = createMockDeps({
				getState: vi.fn(() => state),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'start',
				session_id: 'session-1',
			});

			// The SSE-fed reducer owns the transcript: start adopts the id and starts
			// the feed (its replay renders the turn).
			expect(feedController.start).toHaveBeenCalledWith('session-1');
			expect(deps.onPersistSession).toHaveBeenCalled();
		});
	});

	// Queue processing is now server-side — no frontend tests needed

	describe('query_queued handler (send-now replacement routed to the queue)', () => {
		// Finding F5: the user clicks "Send now" while a turn is streaming. The FE
		// sends interrupt_for_query, latches internal.interruptPending, and sets
		// optimistic isStreaming=true. The backend confirms, the FE sends the query
		// — but the pooled client is still winding down the force-cancelled turn, so
		// the backend routes the query to the queue and acks with `query_queued`
		// (plus the usual queue_updated broadcast for the chip). Without a handler,
		// interruptPending stays latched and isStreaming stays optimistically true →
		// the spinner never clears. The handler clears interruptPending, drops the
		// optimistic streaming state + streaming-session map entry; the SSE feed
		// truth-ups the transcript.

		function interruptedState() {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true; // optimistic streaming set by the interrupt path
			return state;
		}

		it('clears interruptPending, drops isStreaming, and cleans the streaming map', () => {
			const state = interruptedState();
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			deps.internal.interruptPending = true;
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'query_queued', session_id: 'session-1' });

			// The interrupt latch is released.
			expect(deps.internal.interruptPending).toBe(false);
			// Optimistic streaming state cleared so the feed's truth wins.
			expect(applied?.isStreaming).toBe(false);
			expect(applied?.streamStartTime).toBeNull();
			// Streaming-session map entry removed (mirrors done/stopped terminal frames).
			const fn = (deps.streamingSessions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			expect(fn(new Map([['session-1', { projectId: null, worktreeId: null }]])).has('session-1')).toBe(false);
		});

		it('ignores a query_queued for a non-active session', () => {
			const state = interruptedState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = true;
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'query_queued', session_id: 'some-other-session' });

			// Cross-session safety: the active session's latch/streaming/state untouched.
			expect(deps.internal.interruptPending).toBe(true);
			expect(deps.update).not.toHaveBeenCalled();
		});
	});
});
