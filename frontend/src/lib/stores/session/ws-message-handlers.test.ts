/**
 * Tests for ws-message-handlers.ts — Message dispatch and key handler behavior.
 *
 * Architecture: WS handlers are notification-only. They trigger polls and fire
 * callbacks but NEVER write session message state (messages, isStreaming).
 * Session state is written SOLELY by the session-poller via onPollApplied.
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

import { notify } from '../notifications';
import { startFixChat } from '$lib/services/fix-with-ai';

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		pendingTools: new Set(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
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
		sessionPoller: {
			pollNow: vi.fn(),
			pollFinal: vi.fn(),
			startStreaming: vi.fn(),
			stop: vi.fn(),
			isPolling: vi.fn(() => false),
			loadSession: vi.fn(async () => null),
		},
		sessionNotifier: {
			handleMessage: vi.fn(),
		},
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
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		executionMode: 'local',
		cloudTargetInstanceId: null,
		cloudWorktree: false,
		cloudBranch: null,
		cloudCreateNewBranch: false,
		worktreeBranch: '',
		worktreeBaseBranch: '',
		worktreeCreateNewBranch: true,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		isReopeningWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		wsStreamContent: '',
		pendingInterruptPrompt: null,
		remoteControllable: true,
		relayError: undefined,
		isRestoringSession: false,
	};
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('ws-message-handlers', () => {
	let deps: MessageHandlerDeps;
	let handlers: ReturnType<typeof createMessageHandlers>;

	beforeEach(() => {
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
		it('should trigger a final poll (notification-only, no state writes)', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-1',
			});

			// done triggers pollFinal (survives stop()) so the
			// post-end_streaming state is always fetched.
			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-1');
		});

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

		it('should trigger pollFinal when interruptPending is false', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'stopped',
				session_id: 'session-1',
			});

			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-1');
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
			// any OTHER session wiped the active session's pendingTools /
			// wsStreamContent and fired a spurious pollFinal on it. Mirror handleDone:
			// route by the event's session_id.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.interruptPending = false;
			deps.internal.pendingTools.add('tool-keep');
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped', session_id: 'session-bg' });

			// Final poll targets the session that actually stopped, not the active one.
			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-bg');
			expect(deps.sessionPoller.pollFinal).not.toHaveBeenCalledWith('session-active');
			// The active session is untouched: pending tools preserved, no stop-timeout
			// clear, no wsStreamContent mutation (the whole active-only block is skipped).
			expect(deps.internal.pendingTools.has('tool-keep')).toBe(true);
			expect(deps.clearStopTimeout).not.toHaveBeenCalled();
			expect(deps.update).not.toHaveBeenCalled();
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
			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-1');
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
			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-1');
		});
	});

	describe('user_message_discarded handler', () => {
		it('should set pendingInterruptPrompt and trigger a final poll', () => {
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

			// Prompt is stashed so ChatInput can restore it to the editor.
			expect(captured).toBe('write me a haiku');
			// Poller (sole authority for the chat view) re-fetches the now-cleaned
			// live store so the orphan user message disappears.
			expect(deps.sessionPoller.pollFinal).toHaveBeenCalledWith('session-1');
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
			expect(deps.sessionPoller.pollFinal).not.toHaveBeenCalled();
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

			// Must NOT be adopted: state untouched, poller not started, still awaiting.
			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.sessionPoller.startStreaming).not.toHaveBeenCalled();
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
			expect(deps.sessionPoller.startStreaming).not.toHaveBeenCalled();
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

		it('adopts a start with the matching request_id, resets activeTasks, and starts polling', () => {
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
			expect(deps.sessionPoller.startStreaming).toHaveBeenCalledWith('my-session');
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
			expect(deps.sessionPoller.startStreaming).toHaveBeenCalledWith('session-1');
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

		it('stops the active session poller and clears interrupt state on its OWN error', () => {
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

			// Active error: poller stopped, banner shown, interrupt-and-query promise
			// rejected and the flag cleared.
			expect(deps.sessionPoller.stop).toHaveBeenCalledWith('session-1');
			expect(deps.update).toHaveBeenCalled();
			expect(deps.internal.interruptPending).toBe(false);
			expect(rejected).toBe(false);
			expect(deps.internal.interruptConfirmResolve).toBeNull();
		});

		it('does NOT clobber the active session when a BACKGROUND session errors', () => {
			// Frontend U1: pre-fix, the error handler ignored data.session_id and
			// always stopped the ACTIVE session's poller, reset its isStreaming, blew
			// away wsStreamContent, and rejected its interrupt-and-query promise — so a
			// background session's stream error froze whatever was in view. Mirror
			// handleDone / handleStoppedOrInterrupted: route by the event's session_id.
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

			// The errored (background) session's poller is stopped — cross-session safe.
			expect(deps.sessionPoller.stop).toHaveBeenCalledWith('session-bg');
			expect(deps.sessionPoller.stop).not.toHaveBeenCalledWith('session-active');
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

			expect(deps.sessionPoller.stop).toHaveBeenCalledWith('session-1');
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

		it('keeps the question AND toasts an error on a failed relay (success:false)', () => {
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
			});

			// Modal preserved: NO removal write happened.
			expect(deps.update).not.toHaveBeenCalled();
			// User is told the delivery failed.
			expect(notify.error).toHaveBeenCalledTimes(1);
			expect((notify.error as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatch(/deliver your answer/i);
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
		it('should start polling and persist session', () => {
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

			// In the polling-only architecture, start triggers startStreaming on the poller.
			expect(deps.sessionPoller.startStreaming).toHaveBeenCalledWith('session-1');
			expect(deps.onPersistSession).toHaveBeenCalled();
		});
	});

	describe('history handler (notification-only)', () => {
		it('should trigger a poll instead of writing state directly', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'history',
				session_id: 'session-1',
				messages: [
					{ id: 'msg-1', role: 'user', type: 'text', content: 'Hello' },
				],
			});

			// History is now loaded via HTTP — WS history triggers pollNow
			expect(deps.sessionPoller.pollNow).toHaveBeenCalledWith('session-1');
		});
	});

	// Queue processing is now server-side — no frontend tests needed

	describe('sync event forwarding', () => {
		it('should forward sync events to session notifier', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true;

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'stream_chunk',
				event_type: 'stream_chunk',
				session_id: 'session-1',
				data: { content: 'hello' },
			});

			expect(deps.sessionNotifier.handleMessage).toHaveBeenCalled();
		});
	});

	describe('parked-watcher recovery (Bug 2)', () => {
		// A watcher whose session parked on a long-running background task has
		// isStreaming=false (it stopped streaming when the turn parked). When the
		// task finishes, the server fires a follow-up unpark turn that sync_engine
		// broadcasts as an `event_type` ('stream_start'/'stream_end'/'stream_chunk')
		// with NO top-level `type`. Before the fix, the shouldNotify gate matched
		// only `type` values, so a not-streaming watcher dropped these events, the
		// stopped poller never restarted, and the UI showed stale state until a
		// manual reload. These tests pin the recovery: each unpark event_type must
		// reach the notifier for the in-view session even when isStreaming=false.

		function parkedWatcherDeps() {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = false; // parked — not actively streaming
			const d = createMockDeps({ getState: vi.fn(() => state) });
			return d;
		}

		it('forwards an unpark stream_start to the notifier while parked (isStreaming=false)', () => {
			deps = parkedWatcherDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_start',
				session_id: 'session-1',
				data: { message_id: 'm-unpark' },
			});

			expect(deps.sessionNotifier.handleMessage).toHaveBeenCalledWith(
				'session-1',
				expect.objectContaining({ event_type: 'stream_start' }),
			);
		});

		it('forwards an unpark stream_end to the notifier while parked', () => {
			deps = parkedWatcherDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_end',
				session_id: 'session-1',
				data: {},
			});

			expect(deps.sessionNotifier.handleMessage).toHaveBeenCalledWith(
				'session-1',
				expect.objectContaining({ event_type: 'stream_end' }),
			);
		});

		it('forwards an unpark stream_chunk to the notifier while parked', () => {
			deps = parkedWatcherDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_chunk',
				session_id: 'session-1',
				data: { content: 'resumed work' },
			});

			expect(deps.sessionNotifier.handleMessage).toHaveBeenCalledWith(
				'session-1',
				expect.objectContaining({ event_type: 'stream_chunk' }),
			);
		});

		it('does NOT forward an unpark stream event for a different (background) session', () => {
			// The recovery is scoped inside the isForActiveSession guard, so an
			// unpark broadcast for a session that is NOT in view must not trigger a
			// poll for the active session (that was the ghost-poll / phantom-token
			// regression the routing comment warns about).
			deps = parkedWatcherDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_start',
				session_id: 'some-other-session',
				data: {},
			});

			expect(deps.sessionNotifier.handleMessage).not.toHaveBeenCalled();
		});

		it('does NOT forward a non-stream event_type to a parked watcher (e.g. message_added)', () => {
			// Only the three streaming-lifecycle event_types recover a parked
			// watcher. A generic sync event like message_added must still be gated
			// out while not streaming, so we do not re-introduce broad ghost polls.
			deps = parkedWatcherDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'message_added',
				session_id: 'session-1',
				data: {},
			});

			expect(deps.sessionNotifier.handleMessage).not.toHaveBeenCalled();
		});
	});
});
