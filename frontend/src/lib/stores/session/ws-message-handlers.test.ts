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

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		pendingTools: new Set(),
		interruptPending: false,
		awaitingNewSession: false,
		worktreeCreationGeneration: 0,
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
		fastMode: false,
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

			// In the polling-only architecture, done triggers pollNow —
			// the poller detects streaming→ended and updates state.
			expect(deps.sessionPoller.pollNow).toHaveBeenCalledWith('session-1');
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

		it('should trigger pollNow when interruptPending is false', () => {
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

			expect(deps.sessionPoller.pollNow).toHaveBeenCalledWith('session-1');
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
