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

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		pendingTools: new Set(),
		interruptPending: false,
		awaitingNewSession: false,
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
		function newSessionDeps() {
			const state = mockState();
			state.sessionId = null; // brand-new session, no id yet
			const d = createMockDeps({ getState: vi.fn(() => state) });
			d.internal.awaitingNewSession = true;
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

			handlers.handleMessage({ type: 'session_created', session_id: 'sess-new' });

			const sends = stopSends(deps);
			expect(sends).toHaveLength(1);
			expect((sends[0][0] as any).session_id).toBe('sess-new');
			expect(deps.internal.stopRequestedBeforeSession).toBe(false);
		});

		it('start flushes a deferred stop and clears the flag', () => {
			deps = newSessionDeps();
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'start', session_id: 'sess-new', message_id: 'm1' });

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
			// stopRequestedBeforeSession stays false
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_created', session_id: 'sess-new' });

			expect(stopSends(deps)).toHaveLength(0);
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

		it('does not raise a background dot for the active session (modal handles it)', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_attention', session_id: 'session-1', count: 1 });

			expect(deps.backgroundPermissions.update).not.toHaveBeenCalled();
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
});
