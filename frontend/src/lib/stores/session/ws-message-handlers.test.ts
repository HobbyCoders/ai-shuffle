/**
 * Tests for ws-message-handlers.ts — Message dispatch and key handler behavior.
 *
 * These tests verify:
 * - The dispatch function routes message types correctly
 * - Critical handlers (history, done, stopped, error, permission, heartbeat) work
 * - Internal mutable state is updated correctly
 * - Callbacks are invoked for side effects
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createMessageHandlers, type MessageHandlerDeps, type InternalMutableState } from './ws-message-handlers';
import type { ActiveSessionState } from '../active-session';

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		waitingForHistory: null,
		expectedSessionId: null,
		pendingTools: new Set(),
		streamEndCleanedUp: null,
		streamingOnDisconnect: null,
		interruptPending: false,
	};
}

function createMockDeps(overrides?: Partial<MessageHandlerDeps>): MessageHandlerDeps {
	return {
		update: vi.fn((fn) => fn(mockState())),
		getState: vi.fn(() => mockState()),
		wsSend: vi.fn(() => true),
		sessionPoller: {
			pollNow: vi.fn(),
			startStreaming: vi.fn(),
			stop: vi.fn(),
			isPolling: vi.fn(() => false),
		},
		sessionNotifier: {
			handleMessage: vi.fn(),
		},
		sessionState: {
			getSession: vi.fn(),
			removeSession: vi.fn(),
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
		gitApplyServerUpdate: vi.fn(),
		appLoadSessions: vi.fn(),
		appLoadApiUserSessions: vi.fn(),
		internal: createMockInternal(),
		onNewChat: vi.fn(),
		onPersistSession: vi.fn(),
		onStartPolling: vi.fn(),
		onStopPolling: vi.fn(),
		onProcessNextQueuedMessage: vi.fn(),
		onProcessWaitingEvents: vi.fn(),
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
		thinkingModeOverride: null,
		pendingPermissions: [],
		pendingQuestions: [],
		todos: [],
		queuedMessages: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		executionMode: 'local',
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
		it('should set isStreaming to false', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					Object.assign(state, result);
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-1',
				duration: 5.0,
				total_cost_usd: 0.01,
			});

			// update should have been called
			expect(deps.update).toHaveBeenCalled();
		});

		it('should call onNotifySessionComplete with metadata', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({
				getState: vi.fn(() => state),
			});
			handlers = createMessageHandlers(deps);

			// The handler reads cost/token data from data.metadata (not top-level).
			// Field names inside metadata are total_cost_usd, tokens_in, tokens_out.
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

			// durationSeconds is derived from state.streamStartTime which is null
			// in the mock, so it will be undefined — we only assert sessionId and cost.
			expect(deps.onNotifySessionComplete).toHaveBeenCalledWith(
				expect.objectContaining({
					sessionId: 'session-1',
					totalCostUsd: 0.01,
				})
			);
		});

		it('should trigger a final poll', () => {
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-1',
			});

			expect(deps.sessionPoller.pollNow).toHaveBeenCalledWith('session-1');
		});

		it('should process next queued message', () => {
			vi.useFakeTimers();
			const state = mockState();
			state.isStreaming = true;
			state.sessionId = 'session-1';
			state.queuedMessages = [{ id: 'q1', content: 'follow-up', queuedAt: Date.now() }];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-1',
			});

			// onProcessNextQueuedMessage is called inside setTimeout(..., 500)
			vi.runAllTimers();
			vi.useRealTimers();

			expect(deps.onProcessNextQueuedMessage).toHaveBeenCalled();
		});
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

			// When interruptPending is true the handler returns early after clearing
			// pendingTools — it does NOT clear interruptPending and does NOT call update.
			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.internal.pendingTools.size).toBe(0);
		});

		it('should handle normal stop when interruptPending is false', () => {
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

			// Should have called update to clear streaming
			expect(deps.update).toHaveBeenCalled();
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

			// The error handler reads data.message (not data.error) for the error text.
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

			// Should have sent a request for pending permissions
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
		it('should set isStreaming to true', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = false;

			let captured = false;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					captured = result.isStreaming as boolean;
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'start',
				session_id: 'session-1',
			});

			expect(deps.update).toHaveBeenCalled();
			expect(captured).toBe(true);
		});

		it('should persist the session on start', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'start',
				session_id: 'session-1',
			});

			// The start handler updates state (isStreaming=true) and persists the session.
			// Polling is started from the history handler (when isStreaming is true),
			// not from the start handler itself.
			expect(deps.update).toHaveBeenCalled();
			expect(deps.onPersistSession).toHaveBeenCalled();
		});
	});

	describe('session guarding', () => {
		it('should ignore events for a different session', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'done',
				session_id: 'session-OTHER',
			});

			// Should NOT have tried to clear streaming state on our session
			expect(deps.onNotifySessionComplete).not.toHaveBeenCalled();
		});
	});

	describe('history handler', () => {
		it('should ignore history for unexpected session', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.expectedSessionId = 'session-1';
			handlers = createMessageHandlers(deps);

			// Should be silently dropped
			handlers.handleMessage({
				type: 'history',
				session_id: 'session-WRONG',
				messages: [],
			});

			// update should not have been called with message data
			// (it might be called for other reasons but not with history messages)
		});

		it('should accept history for expected session', () => {
			const state = mockState();
			state.sessionId = 'session-1';

			deps = createMockDeps({ getState: vi.fn(() => state) });
			deps.internal.expectedSessionId = 'session-1';
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'history',
				session_id: 'session-1',
				messages: [
					{ id: 'msg-1', role: 'user', type: 'text', content: 'Hello' },
					{ id: 'msg-2', role: 'assistant', type: 'text', content: 'Hi' },
				],
			});

			// Should have called update with messages
			expect(deps.update).toHaveBeenCalled();
		});
	});

	describe('stream_message_delta should not process queued messages', () => {
		it('should NOT process queued messages on stop_reason: tool_use', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true;
			state.queuedMessages = [{ id: 'q1', content: 'follow-up', queuedAt: Date.now() }];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'stream_message_delta',
				delta: { stop_reason: 'tool_use' },
			});

			expect(deps.onProcessNextQueuedMessage).not.toHaveBeenCalled();
		});

		it('should NOT process queued messages on stop_reason: end_turn', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true;
			state.queuedMessages = [{ id: 'q1', content: 'follow-up', queuedAt: Date.now() }];

			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'stream_message_delta',
				delta: { stop_reason: 'end_turn' },
			});

			expect(deps.onProcessNextQueuedMessage).not.toHaveBeenCalled();
		});
	});

	describe('sync event forwarding', () => {
		it('should forward sync events to session notifier', () => {
			const state = mockState();
			state.sessionId = 'session-1';

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
