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
import {
	createMessageHandlers,
	SESSION_LIST_RELOAD_DEBOUNCE_MS,
	type MessageHandlerDeps,
	type InternalMutableState,
} from './ws-message-handlers';
import type { ActiveSessionState } from '../active-session';
import { sessionTimestamp } from '$lib/components/layout/session-card-data';

// Mock the side-effect services the error handlers call. Keeps this a true unit
// test of message dispatch — isolated from the heavy active-session/layout
// module graph, and no real network POST or chat navigation fires. The services
// have their own dedicated tests (error-reporter.test.ts, fix-with-ai.test.ts).
vi.mock('$lib/services/error-reporter', () => ({ reportClientError: vi.fn() }));
vi.mock('$lib/services/fix-with-ai', () => ({ startFixChat: vi.fn() }));
// Error actions read the support capabilities; drive them per test.
vi.mock('$lib/stores/support-report.svelte', () => ({
	canSendSupportReport: vi.fn(() => true),
	canFixWithAi: vi.fn(() => false),
	openSupportReport: vi.fn(),
}));
// The unified notification funnel: assert the handler feeds server events into
// the store via `notify.ingest`, rather than rendering them itself. `notify.error`
// is used by the failed-answer-relay path to toast the user.
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
// The SSE-fed reducer singleton. Session adoption (session_created/start) starts
// the feed; mock it so we can assert adoption without a real EventSource, and so
// isFeeding() reports "not yet feeding" (the branch that triggers start()).
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(),
		start: vi.fn(),
		stop: vi.fn(),
		restart: vi.fn(),
		isFeeding: vi.fn(() => false),
		invalidate: vi.fn(),
	},
	// Mirrors the real constant: `start` marks streaming OPTIMISTICALLY so the
	// fold clears it once the reducer confirms the turn, instead of competing
	// with the feed as a second writer of isStreaming.
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

import { notify } from '../notifications';
import { startFixChat } from '$lib/services/fix-with-ai';
import { canFixWithAi, openSupportReport } from '$lib/stores/support-report.svelte';
import { feedController } from './v2/feed-controller';
import { pendingQueue } from './pending-queue';
import { composerSuggestion } from './composer-suggestion';
import { get } from 'svelte/store';
import { rateLimit } from '../rate-limit';
import { _resetProseQuestions, proseQuestionSessions, rememberLeavingProseQuestion } from './prose-questions';

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockInternal(): InternalMutableState {
	return {
		pendingTools: new Set(),
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
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
		gitRefreshPrState: vi.fn(),
		gitGetProjectId: vi.fn(() => null),
		gitGetLoading: vi.fn(() => false),
		appLoadSessions: vi.fn(),
		appLoadApiUserSessions: vi.fn(),
		mergeSessionPatch: vi.fn(() => true),
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
		mode: 'code',
		title: '',
		messages: [],
		isStreaming: false,
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: true,
		wsReconnectRequired: false,
		error: null,
		profile: 'default',
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: '',
		projectV2Id: null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		liveContextUsage: null,
		contextMax: 0,
		modelOverride: null,
		permissionModeOverride: null,
		effortLevelOverride: null,
		fastMode: false,
		contextWindowChoice: '1m',
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		pendingPeerMessages: [],
		activeTasks: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		worktreeIntent: 'local',
	deviceIntent: null,
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
		pendingRestorePrompt: null,
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

	describe('composer suggestions', () => {
		it.each([true, false])('routes a hint notification to the authoritative reader (envelope: %s)', envelope => {
			const receive = vi.spyOn(composerSuggestion, 'receive').mockImplementation(() => {});
			const frame = { type: 'composer_suggestion', session_id: 'session-1', turn_id: 't', suggestion: 'Next?' };
			handlers.handleMessage(envelope ? { event_type: 'stream_frame', data: frame } : frame);
			expect(receive).toHaveBeenCalledWith(frame);
			receive.mockRestore();
		});
		it.each(['turn.started', 'message.user'])('routes active-session %s through the suggestion reader', type => {
			const receive = vi.spyOn(composerSuggestion, 'receive').mockImplementation(() => {});
			const frame = { type, session_id: 'session-1', turn_id: 't', continued: true };
			handlers.handleMessage({ ...frame, session_id: 'other' });
			expect(receive).not.toHaveBeenCalled();
			handlers.handleMessage({ event_type: 'stream_frame', data: frame });
			expect(receive).toHaveBeenCalledExactlyOnceWith(frame);
			receive.mockRestore();
		});
		it.each(['stopped', 'interrupted', 'error'])('invalidates current-session %s, not background events', type => {
			const invalidate = vi.spyOn(composerSuggestion, 'invalidate');
			handlers.handleMessage({ type, session_id: 'other', message: 'test' });
			expect(invalidate).not.toHaveBeenCalled();
			handlers.handleMessage({ type, session_id: 'session-1', message: 'test' });
			expect(invalidate).toHaveBeenCalledOnce();
			invalidate.mockRestore();
		});
	});

	describe('rate_limit frames', () => {
		it('drops a frame without a window type, logging once, and forwards typed ones', () => {
			const apply = vi.spyOn(rateLimit, 'applyLiveEvent').mockImplementation(() => {});
			const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
			try {
				handlers.handleMessage({ type: 'rate_limit', status: 'rejected', utilization: 0.9 });
				handlers.handleMessage({ type: 'rate_limit', rate_limit_type: '', utilization: 0.9 });
				handlers.handleMessage({ type: 'rate_limit', rate_limit_type: null, status: 'allowed' });
				expect(apply).not.toHaveBeenCalled();
				expect(warn.mock.calls.filter((c) => String(c[0]).includes('rate_limit'))).toHaveLength(1);

				handlers.handleMessage({
					type: 'rate_limit', rate_limit_type: 'five_hour', status: 'rejected',
					utilization: null, resets_at: '1900000000', instance_id: 'inst-a',
				});
				expect(apply).toHaveBeenCalledOnce();
				expect(apply.mock.calls[0][0]).toMatchObject({
					rate_limit_type: 'five_hour', status: 'rejected', instance_id: 'inst-a',
				});
			} finally {
				apply.mockRestore();
				warn.mockRestore();
			}
		});
	});

	describe('message dispatch', () => {
		it('should handle ping messages by sending pong', () => {
			handlers.handleMessage({ type: 'ping' });
			expect(deps.wsSend).toHaveBeenCalledWith({ type: 'pong' });
		});

		it('unwraps a stream_frame envelope and routes the inner event (tool_use)', () => {
			// The per-session sync fan-out (session_runtime.py::_default_broadcast)
			// wraps a normal legacy WS frame under `data` and tags it
			// `event_type: 'stream_frame'`. Pre-fix, the dispatcher's `if (eventType)`
			// branch matched none of its cases and `return`ed, silently dropping the
			// frame — a device watching the session via stream_frame never saw the
			// tool_use tick (or permission_request, done, etc.) that this test guards.
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_frame',
				session_id: 'session-1',
				data: {
					type: 'tool_use',
					session_id: 'session-1',
					id: 'tool-wrapped',
					name: 'Read',
				},
			});

			expect(deps.internal.pendingTools.has('tool-wrapped')).toBe(true);
		});

		it('unwraps a stream_frame envelope and routes an inner permission_request', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [];
			let updatedPermissions: unknown[] = [];
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => {
					const result = fn(state);
					if (result.pendingPermissions) updatedPermissions = result.pendingPermissions as unknown[];
				}),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				event_type: 'stream_frame',
				session_id: 'session-1',
				data: {
					type: 'permission_request',
					session_id: 'session-1',
					request_id: 'perm-wrapped',
					tool_name: 'Bash',
					tool_input: { command: 'ls' },
				},
			});

			expect(updatedPermissions.length).toBe(1);
		});

		it('ignores a stream_frame envelope with no inner payload without throwing', () => {
			expect(() => {
				handlers.handleMessage({ event_type: 'stream_frame', session_id: 'session-1' });
			}).not.toThrow();
			expect(deps.update).not.toHaveBeenCalled();
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

		it('user_branch_changed refreshes status, diff stats, and PR state', () => {
			deps = createMockDeps({
				gitGetProjectId: vi.fn(() => 'project-1'),
				gitGetLoading: vi.fn(() => false),
				gitRefreshStatus: vi.fn(),
				gitRefreshDiffStats: vi.fn(),
				gitRefreshPrState: vi.fn(),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'user_branch_changed', project_id: 'project-1' });

			expect(deps.gitRefreshStatus).toHaveBeenCalled();
			expect(deps.gitRefreshDiffStats).toHaveBeenCalled();
			expect(deps.gitRefreshPrState).toHaveBeenCalled();
		});

		it('user_branch_changed skips a different project entirely', () => {
			deps = createMockDeps({
				gitGetProjectId: vi.fn(() => 'other-project'),
				gitRefreshStatus: vi.fn(),
				gitRefreshDiffStats: vi.fn(),
				gitRefreshPrState: vi.fn(),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'user_branch_changed', project_id: 'project-1' });

			expect(deps.gitRefreshStatus).not.toHaveBeenCalled();
			expect(deps.gitRefreshPrState).not.toHaveBeenCalled();
		});
	});

	describe('notification handler (unified funnel)', () => {
		// `notify.ingest` / `startFixChat` are module-level mocks; clear their call
		// history per-test so each can assert on `mock.calls[0]`.
		beforeEach(() => {
			(notify.ingest as ReturnType<typeof vi.fn>).mockClear();
			(startFixChat as ReturnType<typeof vi.fn>).mockClear();
			(openSupportReport as ReturnType<typeof vi.fn>).mockClear();
			(canFixWithAi as ReturnType<typeof vi.fn>).mockReturnValue(false);
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

		it('offers only "Send report to support" for api_error when Fix with AI is off', () => {
			handlers.handleMessage({
				type: 'notification',
				notification: {
					id: 'srv-3a',
					severity: 'error',
					category: 'api_error',
					message: 'API error: HTTP 500',
					session_id: 'session-9',
					action: { kind: 'fix_with_ai', payload: { status_code: 500, session_id: 'session-9' } },
				},
			});

			const [payload, action, secondary] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(payload.duration_ms).toBe(10000);
			expect(action.label).toBe('Send report to support');
			expect(secondary).toBeUndefined();
			action.onClick();
			expect(openSupportReport).toHaveBeenCalledWith({
				error: { source: 'ws', message: 'API error: HTTP 500', status_code: 500 },
			});
			expect(startFixChat).not.toHaveBeenCalled();
		});

		it('adds Fix with AI as a second action on a source checkout and wires startFixChat', () => {
			(canFixWithAi as ReturnType<typeof vi.fn>).mockReturnValue(true);
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

			const [payload, report, action] = (notify.ingest as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(payload.duration_ms).toBe(10000);
			expect(report.label).toBe('Send report to support');
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

	describe('authoritative user-wide streaming state', () => {
		it.each(['done', 'stopped', 'interrupted', 'error'])(
			'keeps a successor run busy after a delayed %s frame', (type) => {
				// Use the real handlers to produce the map, not pre-seeded state.
				let streaming = new Map();
				deps = createMockDeps({
					streamingSessions: {
						update: (fn) => { streaming = fn(streaming); },
						set: (value) => { streaming = value; },
					},
				});
				handlers = createMessageHandlers(deps);
				const busy = (session_id: string, is_streaming: boolean) =>
					handlers.handleMessage({ type: 'user_streaming_changed', session_id, is_streaming });
				busy('session-1', true);
				busy('other-session', true);
				busy('session-1', false); // previous run releases
				busy('session-1', true); // next run claims before the old WS frame arrives
				handlers.handleMessage({ type, session_id: 'session-1', message: 'old turn failed' });
				expect([...streaming.keys()].sort()).toEqual(['other-session', 'session-1']);
				busy('session-1', false);
				expect([...streaming.keys()]).toEqual(['other-session']);
				// Reconnect snapshots remain authoritative, including empty snapshots.
				handlers.handleMessage({ type: 'user_streaming_sessions', sessions: [] });
				expect(streaming.size).toBe(0);
			},
		);
	});

	describe('done handler', () => {
		it('stays silent while parked and notifies when the resumed turn completes', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'done', session_id: 'session-1', outcome: 'parked' });
			expect(deps.onNotifySessionComplete).not.toHaveBeenCalled();
			expect(deps.onPersistSession).toHaveBeenCalledOnce();
			expect(deps.clearStopTimeout).toHaveBeenCalledOnce();

			handlers.handleMessage({ type: 'done', session_id: 'session-1', outcome: 'completed' });
			expect(deps.onNotifySessionComplete).toHaveBeenCalledOnce();
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

		it('clears a stale permission dot for the finished session', () => {
			const state = mockState();
			state.sessionId = 'session-active'; // a different session is active here
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'done', session_id: 'session-bg' });

			// A finished turn can't still be waiting on a permission. The done
			// handler clears that dot defensively, in case the backend count=0
			// broadcast was missed — otherwise it stays lit until the user opens
			// the session.
			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			expect(fn(new Map([['session-bg', { count: 1, kind: 'permission' as const }]])).has('session-bg')).toBe(false);
		});

		it('keeps a question dot for the finished session', () => {
			// Changed behavior: a posted question stays pending after the turn
			// that asked it ends, so `done` must not erase "Has question". The
			// server sends the durable count when the question is answered.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'done', session_id: 'session-bg' });

			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			const before = new Map([['session-bg', { count: 1, kind: 'question' as const }]]);
			expect(fn(before).get('session-bg')).toEqual({ count: 1, kind: 'question' });
		});

		// Queue processing is now handled server-side — no frontend trigger to test

		// ── W1: `done` patches the row instead of re-paging the world ──────
		it('patches the finished row instead of reloading the whole session list', () => {
			handlers.handleMessage({ type: 'done', session_id: 'session-bg', metadata: {} });

			expect(deps.mergeSessionPatch).toHaveBeenCalledWith(
				'session-bg',
				expect.objectContaining({ updated_at: expect.any(String) })
			);
			expect(deps.appLoadSessions).not.toHaveBeenCalled();
		});

		it('never reloads the list for a `done`, even after the debounce window', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({ type: 'done', session_id: 'session-bg', metadata: {} });
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS * 3);

				expect(deps.appLoadSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});

		it('falls back to ONE debounced reload when the finished row is unknown locally', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps({ mergeSessionPatch: vi.fn(() => false) });
				handlers = createMessageHandlers(deps);

				// A burst of completions (several background sessions finishing, a
				// reconnect backfill) used to cost a full uncapped re-page EACH.
				for (let i = 0; i < 5; i++) {
					handlers.handleMessage({ type: 'done', session_id: `unknown-${i}`, metadata: {} });
				}
				expect(deps.appLoadSessions).not.toHaveBeenCalled();

				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('stopped handler', () => {
		it('clears a stale permission dot for the stopped session', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'stopped', session_id: 'session-1' });

			// Mirrors the done handler: a stopped/interrupted turn can't be
			// waiting on a permission, so a leftover permission dot is cleared.
			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			expect(fn(new Map([['session-1', { count: 1, kind: 'permission' as const }]])).has('session-1')).toBe(false);
		});

		it('keeps a question dot for the stopped session', () => {
			// Changed behavior: a posted question outlives a stop.
			const state = mockState();
			state.sessionId = 'session-1';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'interrupted', session_id: 'session-1' });

			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			const before = new Map([['session-1', { count: 2, kind: 'question' as const }]]);
			expect(fn(before).get('session-1')).toEqual({ count: 2, kind: 'question' });
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
					captured = fn(state).pendingRestorePrompt;
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

	describe('queued_message_removal handler (outcome of x on a pending message)', () => {
		beforeEach(() => {
			(notify.warning as ReturnType<typeof vi.fn>).mockClear();
		});

		function activeOn(sessionId: string) {
			const state = mockState();
			state.sessionId = sessionId;
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);
		}

		it('tells the user a message the agent already read was not taken back', () => {
			activeOn('session-1');

			handlers.handleMessage({
				type: 'queued_message_removal',
				session_id: 'session-1',
				message_id: 'turn-1',
				outcome: 'already_delivered',
			});

			expect(notify.warning).toHaveBeenCalledTimes(1);
			const [message, opts] = (notify.warning as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(message).toMatch(/already sent/i);
			expect(opts).toMatchObject({ session_id: 'session-1' });
			expect(deps.update).toHaveBeenCalledTimes(1);
		});

		it('stays quiet when the cancel took effect', () => {
			activeOn('session-1');

			handlers.handleMessage({
				type: 'queued_message_removal',
				session_id: 'session-1',
				message_id: 'turn-1',
				outcome: 'cancelled',
			});

			expect(notify.warning).not.toHaveBeenCalled();
		});

		it('ignores an outcome for a session that is not in view', () => {
			activeOn('session-1');

			handlers.handleMessage({
				type: 'queued_message_removal',
				session_id: 'session-2',
				message_id: 'turn-1',
				outcome: 'already_delivered',
			});

			expect(notify.warning).not.toHaveBeenCalled();
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

		it('resets streaming on its OWN error', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'error',
				session_id: 'session-1',
				message: 'boom',
			});

			// Active error: banner shown + isStreaming reset so the user can send again.
			expect(applied?.error).toBe('boom');
			expect(applied?.isStreaming).toBe(false);
		});

		it('does NOT clobber the active session when a BACKGROUND session errors', () => {
			// Frontend U1: pre-fix, the error handler ignored data.session_id and
			// always reset the ACTIVE session's isStreaming — so a background
			// session's stream error froze whatever was in view. Mirror handleDone /
			// handleStoppedOrInterrupted: route by the event's session_id.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'error',
				session_id: 'session-bg',
				message: 'background blew up',
			});

			// Only the run-status channel can clear the background session's pill.
			expect(deps.streamingSessions.update).not.toHaveBeenCalled();
			// The active session is untouched: no banner and no isStreaming reset
			// (the whole active-only block is skipped).
			expect(deps.update).not.toHaveBeenCalled();
		});

		it('offers a local fork when the owning worker is unreachable', () => {
			// The backend stamps `offer_fork: true` on the error frame when the
			// session's owner cannot be reached; the chat renders a "Fork here"
			// action from `forkOfferSessionId`. A plain error must NOT offer it.
			const state = mockState();
			state.sessionId = 'session-1';
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { applied = fn(state); return applied; }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'error', session_id: 'session-1', message: 'owner offline', offer_fork: true });
			expect(applied?.error).toBe('owner offline');
			expect(applied?.forkOfferSessionId).toBe('session-1');

			handlers.handleMessage({ type: 'error', session_id: 'session-1', message: 'plain failure' });
			expect(applied?.forkOfferSessionId).toBeNull();
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

	describe('pending_* reply for a socket with no session selected', () => {
		// The backend answers get_pending_permissions / get_pending_questions with
		// an empty list and `session_id: null` when the socket has no session
		// selected; that must not wipe the active session's open modals.
		it('leaves the permission and question queues untouched', () => {
			const state = mockState();
			state.sessionId = 'session-1';
			state.pendingPermissions = [
				{ session_id: 'session-1', request_id: 'perm-open', tool_name: 'Bash', tool_input: {} } as never,
			];
			state.pendingQuestions = [
				{ session_id: 'session-1', request_id: 'q-open', tool_use_id: 't', questions: [] } as never,
			];
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'pending_permissions', session_id: null, pending: [], deferred: [] });
			handlers.handleMessage({ type: 'pending_questions', session_id: null, questions: [] });

			expect(deps.update).not.toHaveBeenCalled();
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
		it('preserves nonblocking on a live question without changing streaming state', () => {
			const state = mockState(); state.sessionId = 'session-1'; state.isStreaming = true;
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({ getState: vi.fn(() => state), update: vi.fn(fn => { applied = fn(state); return applied; }) });
			handlers = createMessageHandlers(deps);
			handlers.handleMessage({ type: 'user_question', session_id: 'session-1', request_id: 'posted-1', tool_use_id: 'tool-1', nonblocking: true, questions: [{ question: 'Proceed?', header: 'Confirm' }] });
			expect(applied?.pendingQuestions[0].nonblocking).toBe(true);
			expect(applied?.isStreaming).toBe(true);
		});

		it('keeps the model picker on a live question and on a reconnect resync (#1827)', () => {
			const state = mockState(); state.sessionId = 'session-1';
			const applied: ActiveSessionState[] = [];
			deps = createMockDeps({ getState: vi.fn(() => state), update: vi.fn(fn => { const next = fn(state); applied.push(next); return next; }) });
			handlers = createMessageHandlers(deps);
			const question = { question: 'Which model should subagents use?', header: 'Subagent model', options: [], multiSelect: false, picker: 'model' };
			handlers.handleMessage({ type: 'user_question', session_id: 'session-1', request_id: 'subagent-default-1', tool_use_id: 'subagent-default-1', questions: [question, { question: 'Other?', header: 'Other', picker: 'colour' }] });
			const live = applied.at(-1)!.pendingQuestions[0].questions;
			expect(live[0].picker).toBe('model');
			// Only a picker the card knows is passed on.
			expect(live[1].picker).toBeUndefined();
			handlers.handleMessage({ type: 'pending_questions', session_id: 'session-1', questions: [{ request_id: 'subagent-default-2', tool_use_id: 'subagent-default-2', questions: [question] }] });
			expect(applied.at(-1)!.pendingQuestions.find(q => q.request_id === 'subagent-default-2')?.questions[0].picker).toBe('model');
		});

		it('hydrates durable nonblocking questions after the originating turn ended', () => {
			const state = mockState(); state.sessionId = 'session-1'; state.isStreaming = false;
			let applied: ActiveSessionState | undefined;
			deps = createMockDeps({ getState: vi.fn(() => state), update: vi.fn(fn => { applied = fn(state); return applied; }) });
			handlers = createMessageHandlers(deps);
			handlers.handleMessage({ type: 'pending_questions', session_id: 'session-1', questions: [{ request_id: 'posted-1', tool_use_id: 'tool-1', nonblocking: true, questions: [{ question: 'Proceed?', header: 'Confirm' }] }] });
			expect(applied?.pendingQuestions[0].nonblocking).toBe(true);
			expect(applied?.isStreaming).toBe(false);
		});
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
		// starting Map so we can assert the resulting attention state. Values are
		// {count, kind} records — the kind drives the sidebar card's label.
		type Attention = { count: number; kind: 'permission' | 'question' };
		function applyAttention(start: Map<string, Attention>): Map<string, Attention> {
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
			expect(applyAttention(new Map()).get('session-bg')).toEqual({ count: 2, kind: 'permission' });
		});

		it('carries the question kind through to the attention entry', () => {
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({
				type: 'session_attention',
				session_id: 'session-bg',
				count: 1,
				kind: 'question',
			});

			expect(applyAttention(new Map()).get('session-bg')).toEqual({ count: 1, kind: 'question' });
		});

		it('keeps an existing count when a different-kind prompt arrives', () => {
			// A session blocked on a permission AND a question holds TWO prompts;
			// the broadcast carries the total count with the latest kind — it must
			// replace (not increment) so the count stays server-authoritative.
			const state = mockState();
			state.sessionId = 'session-active';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'user_question', session_id: 'session-bg', request_id: 'q-1', tool_use_id: 't-1', questions: [{ question: '?', header: 'H' }] });
			handlers.handleMessage({ type: 'session_attention', session_id: 'session-bg', count: 2, kind: 'question' });

			expect(applyAttention(new Map([['session-bg', { count: 1, kind: 'permission' } as Attention]])).get('session-bg'))
				.toEqual({ count: 2, kind: 'question' });
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
			expect(applyAttention(new Map([['session-1', { count: 1, kind: 'permission' }]])).has('session-1')).toBe(false);
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

	describe('session_attention_snapshot handler', () => {
		it('replaces the attention map with the durable snapshot', () => {
			handlers.handleMessage({
				type: 'session_attention_snapshot',
				sessions: [
					{ session_id: 'session-q', count: 1, kind: 'question' },
					{ session_id: 'session-p', count: 2, kind: 'permission' },
					{ session_id: 'session-zero', count: 0, kind: 'question' },
				],
			});

			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			expect(fn).toBeDefined();
			// A dot missed while disconnected is dropped, not kept.
			const stale = new Map([['session-stale', { count: 1, kind: 'permission' as const }]]);
			const next = fn(stale);
			expect([...next.keys()].sort()).toEqual(['session-p', 'session-q']);
			expect(next.get('session-q')).toEqual({ count: 1, kind: 'question' });
			expect(next.get('session-p')).toEqual({ count: 2, kind: 'permission' });
		});

		it('an empty snapshot clears every dot', () => {
			handlers.handleMessage({ type: 'session_attention_snapshot', sessions: [] });

			const fn = (deps.backgroundPermissions.update as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
			const stale = new Map([['session-stale', { count: 1, kind: 'question' as const }]]);
			expect(fn(stale).size).toBe(0);
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

	describe.each(['session_updated', 'user_session_updated'])('%s handler (parked task kind)', (type) => {
		it.each(['agent', 'shell', null])('patches parked_task_kind=%s without a list reload', async (parkedTaskKind) => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				const updates = { parked_task_kind: parkedTaskKind };
				handlers.handleMessage(type === 'session_updated' ? {
					event_type: type,
					session_id: 'session-1',
					data: { updates },
				} : {
					type,
					session_id: 'session-1',
					updates,
				});

				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', {
					parked_task_kind: parkedTaskKind,
				});
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				expect(deps.appLoadApiUserSessions).not.toHaveBeenCalled();
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				expect(deps.appLoadApiUserSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe.each(['session_updated', 'user_session_updated'])('%s handler (running tasks)', (type) => {
		it.each([true, false])('patches has_running_tasks=%s without a list reload', async (hasRunningTasks) => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				const updates = { has_running_tasks: hasRunningTasks };
				handlers.handleMessage(type === 'session_updated' ? {
					event_type: type,
					session_id: 'session-1',
					data: { updates },
				} : {
					type,
					session_id: 'session-1',
					updates,
				});

				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', {
					has_running_tasks: hasRunningTasks,
				});
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				expect(deps.appLoadApiUserSessions).not.toHaveBeenCalled();
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				expect(deps.appLoadApiUserSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('session_updated handler (context-window pick, #1895)', () => {
		function announce(sessionId: string, contextWindow: number) {
			let state = mockState();
			state.contextWindowChoice = '1m';
			state.contextWindowPin = 1_000_000;
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { state = fn(state); }),
			});
			handlers = createMessageHandlers(deps);
			handlers.handleMessage({
				event_type: 'session_updated',
				session_id: sessionId,
				data: { updates: { context_window: contextWindow } },
			});
			return state;
		}

		it('adopts a pick made on another tab or device without a reload', () => {
			const state = announce('session-1', 200_000);
			expect(state.contextWindowChoice).toBe('standard');
			expect(state.contextWindowPin).toBe(200_000);
			expect(deps.onPersistSession).toHaveBeenCalled();
			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', { context_window: 200_000 });
		});

		it('leaves the open chat alone when another chat was picked', () => {
			const state = announce('session-2', 200_000);
			expect(state.contextWindowChoice).toBe('1m');
			expect(state.contextWindowPin).toBe(1_000_000);
			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-2', { context_window: 200_000 });
		});
	});

	describe('user_session_updated handler (working time, #1619)', () => {
		it('patches work_ms and work_started_at into the row without a list reload', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				const updates = { work_ms: 125_000, work_started_at: '2026-10-01T12:00:00+00:00' };
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'session-1', updates });

				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', updates);
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('user_session_updated handler (user-message revision, #2249)', () => {
		// The question was rendered from event seq 5 of log `evt-1`.
		const asked = [
			{ id: 'u1', role: 'user' as const, content: 'Deploy it', seq: 4, epoch: 'evt-1' },
			{ id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?', seq: 5, epoch: 'evt-1' },
		];
		const at = (seq: number | null, epoch: string | null = 'evt-1') => ({
			last_user_message_at: '2026-10-11T10:05:00.000001+00:00',
			last_user_message_seq: seq,
			last_user_message_epoch: epoch,
		});
		const remembered = () => get(proseQuestionSessions).has('elsewhere');

		it('clears a remembered prose question when another device answers: patches the row, then refreshes the list', async () => {
			vi.useFakeTimers();
			try {
				rememberLeavingProseQuestion('elsewhere', asked, at(4));
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				const updates = at(6);
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates });

				// The mark clears and the row is patched at once, before any reload…
				expect(remembered()).toBe(false);
				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('elsewhere', updates);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				// …and the server's companion updated_at bump arrives by the reload.
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				_resetProseQuestions();
				vi.useRealTimers();
			}
		});

		it('moves the answered session up the last-activity order after the refresh', async () => {
			vi.useFakeTimers();
			try {
				// The sidebar's rows: "elsewhere" is older, so it sorts second.
				let rows: Record<string, unknown>[] = [
					{ id: 'recent', updated_at: '2026-10-11T10:03:00+00:00', created_at: '2026-10-11T09:00:00+00:00' },
					{ id: 'elsewhere', updated_at: '2026-10-11T10:00:00+00:00', created_at: '2026-10-11T09:00:00+00:00' },
				];
				const order = () => [...rows]
					.sort((a, b) => sessionTimestamp(b as never) - sessionTimestamp(a as never))
					.map((r) => r.id);
				// The server stamps updated_at with the user message but the frame
				// carries only last_user_message_at; the list endpoint has both.
				const server = [
					{ id: 'recent', updated_at: '2026-10-11T10:03:00+00:00', created_at: '2026-10-11T09:00:00+00:00' },
					{ id: 'elsewhere', updated_at: '2026-10-11T10:05:00+00:00', created_at: '2026-10-11T09:00:00+00:00', last_user_message_at: '2026-10-11T10:05:00+00:00' },
				];
				deps = createMockDeps({
					mergeSessionPatch: vi.fn((id: string, patch: Record<string, unknown>) => {
						rows = rows.map((r) => (r.id === id ? { ...r, ...patch } : r));
						return true;
					}),
					appLoadSessions: vi.fn(() => { rows = server.map((r) => ({ ...r })); }),
				});
				handlers = createMessageHandlers(deps);
				expect(order()).toEqual(['recent', 'elsewhere']);

				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: at(6) });
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);

				expect(order()).toEqual(['elsewhere', 'recent']);
			} finally {
				_resetProseQuestions();
				vi.useRealTimers();
			}
		});

		it('keeps it for a pre-question seq, another epoch, a null position or a working-time-only update', () => {
			try {
				rememberLeavingProseQuestion('elsewhere', asked, at(4));
				// Same transaction as the question (voice): seq at or below it.
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: at(5) });
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: at(20, 'evt-0') });
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: at(null, null) });
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: { work_ms: 5000, work_started_at: '2026-10-11T11:00:00+00:00' } });
				handlers.handleMessage({ event_type: 'session_updated', session_id: 'elsewhere', data: { updates: { is_active: true } } });
				expect(remembered()).toBe(true);
			} finally {
				_resetProseQuestions();
			}
		});

		it('patches all three revision fields onto the row', () => {
			try {
				handlers.handleMessage({ type: 'user_session_updated', session_id: 'elsewhere', updates: at(6) });
				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('elsewhere', at(6));
			} finally {
				_resetProseQuestions();
			}
		});

		it('drops it when the conversation is cleared', () => {
			try {
				rememberLeavingProseQuestion('elsewhere', asked);
				handlers.handleMessage({ type: 'user_session_cleared', session_id: 'elsewhere' });
				expect(remembered()).toBe(false);
			} finally {
				_resetProseQuestions();
			}
		});
	});

	describe('user_session_updated handler (hidden flip)', () => {
		// The voice attach backfill hides a wake-word "command" session after
		// the row already existed visibly (create→attach window). The flip is
		// fanned user-wide (nobody watches a wake-word session, so the
		// session-scoped channel would reach no device) and must make a list
		// that rendered the card drop it live via a refetch.
		it('hides the row immediately and refetches the list (debounced)', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({
					type: 'user_session_updated',
					session_id: 'voice-session',
					updates: { hidden: true },
				});

				// The card disappears at once via the row patch…
				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('voice-session', { hidden: true });
				// …and `hidden` also reshapes the list server-side, so an
				// authoritative reload still follows — once, on the trailing edge.
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('user_session_updated handler (is_active flip)', () => {
		// Settle/unsettle from any device fans out as user_session_updated with
		// { is_active }. Merging the row in place (no refetch) is what keeps a
		// stale paginated response from re-flipping the card the user just
		// settled — the regression this pins.
		it('merges the is_active patch without a refetch', () => {
			handlers.handleMessage({
				type: 'user_session_updated',
				session_id: 'session-1',
				updates: { is_active: false },
			});

			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', { is_active: false });
			expect(deps.appLoadSessions).not.toHaveBeenCalled();
		});

		it('merges persisted PR fields without a refetch', () => {
			handlers.handleMessage({
				type: 'user_session_updated',
				session_id: 'session-1',
				updates: {
					pr_number: 12,
					pr_url: 'https://github.com/o/r/pull/12',
					pr_state: 'merged',
				},
			});

			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', {
				pr_number: 12,
				pr_url: 'https://github.com/o/r/pull/12',
				pr_state: 'merged',
			});
			expect(deps.appLoadSessions).not.toHaveBeenCalled();
		});

		it('falls back to a debounced refetch when the row is unknown locally', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps({ mergeSessionPatch: vi.fn(() => false) });
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({
					type: 'user_session_updated',
					session_id: 'unknown-session',
					updates: { is_active: true },
				});

				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});

		it('patches title + is_active together without any refetch', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({
					type: 'user_session_updated',
					session_id: 'session-1',
					updates: { is_active: false, title: 'Renamed' },
				});

				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', {
					is_active: false,
					title: 'Renamed',
				});
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});

		it('reloads (debounced) when the payload carries a field the row cannot hold', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({
					type: 'user_session_updated',
					session_id: 'session-1',
					updates: { tags: [{ id: 't1' }] },
				});

				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});

		it('coalesces a creation burst into one list reload (sessions + api users)', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps();
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({ type: 'user_session_created', session_id: 'a' });
				handlers.handleMessage({ type: 'user_session_created', session_id: 'b' });
				handlers.handleMessage({ type: 'user_session_created', session_id: 'c' });
				expect(deps.appLoadSessions).not.toHaveBeenCalled();

				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
				expect(deps.appLoadApiUserSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('user_session_cleared handler (in-place transcript wipe)', () => {
		// POST /sessions/{id}/clear rewrites the event log WITHOUT appending
		// anything a resume cursor could see. Pre-fix, a tab with the session
		// open kept rendering the deleted transcript — even across a switch
		// away and back, because the transcript cache served the stale copy.
		beforeEach(() => {
			(feedController.invalidate as ReturnType<typeof vi.fn>).mockClear();
			(feedController.restart as ReturnType<typeof vi.fn>).mockClear();
		});

		it('drops the cached transcript and patches the row counters (R3, no refetch)', () => {
			handlers.handleMessage({ type: 'user_session_cleared', session_id: 'background-session' });

			expect(feedController.invalidate).toHaveBeenCalledWith('background-session');
			// Not being fed → nothing on screen to reset, so no feed restart.
			expect(feedController.restart).not.toHaveBeenCalled();
			expect(deps.mergeSessionPatch).toHaveBeenCalledWith(
				'background-session',
				expect.objectContaining({ turn_count: 0, total_cost_usd: 0 })
			);
			expect(deps.appLoadSessions).not.toHaveBeenCalled();
		});

		it('re-hydrates from seq 0 when the cleared session is the one being fed', () => {
			(feedController.isFeeding as ReturnType<typeof vi.fn>).mockReturnValue(true);

			handlers.handleMessage({ type: 'user_session_cleared', session_id: 'session-1' });

			// The emptied log folds to zero messages, collapsing the open view to
			// the "Conversation cleared" hero instead of the stale transcript.
			expect(feedController.invalidate).toHaveBeenCalledWith('session-1');
			expect(feedController.restart).toHaveBeenCalledWith('session-1');
		});

		it('clears the attention dot — a wiped conversation awaits no input', () => {
			handlers.handleMessage({ type: 'user_session_cleared', session_id: 'session-1' });
			expect(deps.backgroundPermissions.update).toHaveBeenCalled();
		});

		it('ignores a payload without a session_id', () => {
			handlers.handleMessage({ type: 'user_session_cleared' });
			expect(feedController.invalidate).not.toHaveBeenCalled();
			expect(deps.mergeSessionPatch).not.toHaveBeenCalled();
		});
	});

	describe('user_session_deleted handler', () => {
		beforeEach(() => {
			(feedController.invalidate as ReturnType<typeof vi.fn>).mockClear();
		});

		it('drops the cache, reloads the list (debounced), and leaves the open session', async () => {
			vi.useFakeTimers();
			try {
				deps = createMockDeps(); // mockState().sessionId === 'session-1'
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({ type: 'user_session_deleted', session_id: 'session-1' });

				expect(feedController.invalidate).toHaveBeenCalledWith('session-1');
				// The open view must not keep rendering a deleted transcript —
				// mirror of the session_not_found escape hatch.
				expect(deps.onNewChat).toHaveBeenCalledTimes(1);
				// A row disappeared: list shape changed, so an authoritative
				// (debounced) reload follows.
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).toHaveBeenCalledTimes(1);
			} finally {
				vi.useRealTimers();
			}
		});

		it('stays put when a different session was deleted', () => {
			handlers.handleMessage({ type: 'user_session_deleted', session_id: 'other-session' });

			expect(feedController.invalidate).toHaveBeenCalledWith('other-session');
			expect(deps.onNewChat).not.toHaveBeenCalled();
		});
	});

	describe('session_updated handler (row patch, not a refetch)', () => {
		it('applies a backfilled title to the open session and the row without reloading', async () => {
			vi.useFakeTimers();
			try {
				const state = mockState();
				state.sessionId = 'session-1';
				deps = createMockDeps({ getState: vi.fn(() => state) });
				handlers = createMessageHandlers(deps);

				handlers.handleMessage({
					event_type: 'session_updated',
					session_id: 'session-1',
					data: { updates: { title: 'Backfilled title' } },
				});

				expect(deps.update).toHaveBeenCalled();
				expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', { title: 'Backfilled title' });
				await vi.advanceTimersByTimeAsync(SESSION_LIST_RELOAD_DEBOUNCE_MS + 10);
				expect(deps.appLoadSessions).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});
	});

	describe('user_session_updated handler (rename)', () => {
		it('retitles the open chat, not only its row', () => {
			let state = mockState();
			state.sessionId = 'session-1';
			state.title = 'Old title';
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { state = fn(state); }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'user_session_updated', session_id: 'session-1', updates: { title: 'Renamed' } });

			expect(state.title).toBe('Renamed');
			expect(deps.onPersistSession).toHaveBeenCalled();
			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-1', { title: 'Renamed' });
		});

		it('leaves the open chat alone when another chat is renamed', () => {
			let state = mockState();
			state.sessionId = 'session-1';
			state.title = 'Mine';
			deps = createMockDeps({
				getState: vi.fn(() => state),
				update: vi.fn((fn) => { state = fn(state); }),
			});
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'user_session_updated', session_id: 'session-2', updates: { title: 'Other' } });

			expect(state.title).toBe('Mine');
			expect(deps.mergeSessionPatch).toHaveBeenCalledWith('session-2', { title: 'Other' });
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

		it('records the missing chat so the New Chat it falls back to says why', async () => {
			const { unavailableChat, clearUnavailableChat } = await import('../unavailable-chat');
			const { get } = await import('svelte/store');
			clearUnavailableChat();
			const state = mockState();
			state.sessionId = 'missing-session';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_not_found', session_id: 'missing-session' });

			expect(get(unavailableChat)).toEqual({ sessionId: 'missing-session', status: 404 });
			clearUnavailableChat();
		});

		it('leaves the notice alone when another session is missing', async () => {
			const { unavailableChat, clearUnavailableChat } = await import('../unavailable-chat');
			const { get } = await import('svelte/store');
			clearUnavailableChat();
			const state = mockState();
			state.sessionId = 'open-session';
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'session_not_found', session_id: 'other-session' });

			expect(deps.onNewChat).not.toHaveBeenCalled();
			expect(get(unavailableChat)).toBeNull();
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

	describe('query_queued handler (a send that landed in the queue)', () => {
		// Sending while a turn is live is a plain `query` now: the backend queues it
		// and acks with `query_queued` while the turn keeps streaming. The handler
		// must therefore leave isStreaming and the streaming map alone — the `turn.*`
		// sync events own that state, and clearing it here killed the spinner
		// mid-turn. The pending bubble itself rides on `queue_updated`.

		function streamingState() {
			const state = mockState();
			state.sessionId = 'session-1';
			state.isStreaming = true;
			return state;
		}

		it('leaves the live turn streaming and the streaming map untouched', () => {
			const state = streamingState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'query_queued', session_id: 'session-1' });

			expect(deps.update).not.toHaveBeenCalled();
			expect(deps.streamingSessions.update).not.toHaveBeenCalled();
			expect(deps.clearStopTimeout).not.toHaveBeenCalled();
		});

		it('ignores a query_queued for a non-active session', () => {
			const state = streamingState();
			deps = createMockDeps({ getState: vi.fn(() => state) });
			handlers = createMessageHandlers(deps);

			handlers.handleMessage({ type: 'query_queued', session_id: 'some-other-session' });

			expect(deps.update).not.toHaveBeenCalled();
		});
	});
});

describe('queue_updated (the pending "not seen yet" bubbles)', () => {
	// The snapshot is authoritative about what EXISTS, but the backend emits it
	// from two unordered places (the socket that handled the frame, and the turn
	// pump when a steer is absorbed), so a pre-absorption snapshot can land last.
	// Replace-all alone then resurrects an answered bubble for good.

	let state: ActiveSessionState;
	let deps: MessageHandlerDeps;
	let handlers: ReturnType<typeof createMessageHandlers>;

	function frame(rows: Array<{ id: string; content: string }>): Record<string, unknown> {
		return {
			type: 'sync',
			event_type: 'queue_updated',
			session_id: 'session-1',
			data: {
				queued_messages: rows.map((r) => ({
					...r,
					created_at: '2026-08-23T12:00:00.000Z',
				})),
			},
		};
	}

	function pendingIds(): string[] {
		return state.queuedMessages.map((m) => m.id);
	}

	beforeEach(() => {
		pendingQueue.reset('session-1', true);
		state = mockState();
		deps = createMockDeps({
			update: vi.fn((fn) => { state = fn(state); }),
			getState: vi.fn(() => state),
		});
		handlers = createMessageHandlers(deps);
	});

	it('keeps attributed peer rows separate, replaces them on delivery, and ignores other sessions', () => {
		const peerFrame = {
			type: 'sync', event_type: 'queue_updated', session_id: 'session-1',
			data: { queued_messages: [], pending_peer_messages: [{ id: 'peer-turn', content: 'coordinate',
				sender_label: 'Reviewer', sender_session_id: 'sender', created_at: '2026-08-23T12:00:00.000Z' }] }
		};
		handlers.handleMessage(peerFrame);
		handlers.handleMessage(peerFrame);
		expect(state.pendingPeerMessages).toEqual([{ id: 'peer-turn', content: 'coordinate',
			senderLabel: 'Reviewer', senderSessionId: 'sender', queuedAt: Date.parse('2026-08-23T12:00:00.000Z') }]);
		expect(state.queuedMessages).toEqual([]);
		expect(deps.wsSend).not.toHaveBeenCalled();
		handlers.handleMessage({ ...peerFrame, session_id: 'other', data: { pending_peer_messages: [] } });
		expect(state.pendingPeerMessages).toHaveLength(1);
		state.messages = [{ id: 'delivered', role: 'user', content: 'wrapped peer content', turnId: 'peer-turn' }];
		handlers.handleMessage(frame([]));
		expect(state.pendingPeerMessages).toEqual([]);
		handlers.handleMessage(peerFrame); // A stale pre-delivery snapshot must not resurrect it.
		expect(state.pendingPeerMessages).toEqual([]);
	});

	it('retains cancellation across feed teardown and a delayed verdict for another session', () => {
		const entry = { id: 'client-switch', clientMessageId: 'client-switch', content: 'restore only A', queuedAt: 1 };
		pendingQueue.requestRemoval('session-1', entry);
		pendingQueue.reset(); // A's feed is torn down
		state = { ...state, sessionId: 'session-2', draft: 'B draft', queuedMessages: [] };
		pendingQueue.reset('session-2');
		expect(pendingQueue.reconcile('session-1', [])).toEqual([{ ...entry, status: 'cancelling' }]);
		handlers.handleMessage({ type: 'queued_message_removal', session_id: 'session-1',
			client_message_id: entry.id, message_id: 'turn-q', verdict: 'withdrawn' });
		expect(state.sessionId).toBe('session-2');
		expect(state.draft).toBe('B draft');
		expect(state.pendingRestorePrompt).toBeNull();
		expect(pendingQueue.takeWithdrawnDraft('session-2')).toBeNull();
		pendingQueue.reset();
		pendingQueue.reset('session-1');
		expect(pendingQueue.reconcile('session-1', [])).toHaveLength(0);
		expect(pendingQueue.takeWithdrawnDraft('session-1')).toBe(entry.content);
		handlers.handleMessage({ type: 'queued_message_removal', session_id: 'session-1',
			client_message_id: entry.id, verdict: 'withdrawn' });
		expect(pendingQueue.takeWithdrawnDraft('session-1')).toBeNull();
	});

	it('retains a delivered chip and does not restore its text when delivery wins', () => {
		const entry = { id: 'turn-q', clientMessageId: 'client-a', content: 'sent', queuedAt: 1 };
		pendingQueue.requestRemoval('session-1', entry);
		handlers.handleMessage(frame([]));
		handlers.handleMessage({ type: 'queued_message_removal', session_id: 'session-1',
			message_id: 'turn-q', client_message_id: 'client-a', verdict: 'already_delivered' });
		expect(state.queuedMessages).toEqual([{ ...entry, status: 'delivered' }]);
		expect(state.pendingRestorePrompt).toBeNull();
		expect(notify.warning).toHaveBeenCalledTimes(1);
		handlers.handleMessage(frame([]));
		expect(state.queuedMessages).toHaveLength(1);
	});

	it('renders the server snapshot', () => {
		handlers.handleMessage(frame([{ id: 'turn-q', content: 'steer me' }]));
		expect(pendingIds()).toEqual(['turn-q']);
	});

	it('ignores a snapshot for another session', () => {
		handlers.handleMessage({
			type: 'sync',
			event_type: 'queue_updated',
			session_id: 'other',
			data: { queued_messages: [{ id: 'x', content: 'y', created_at: '2026-08-23T12:00:00.000Z' }] },
		});
		expect(pendingIds()).toEqual([]);
	});

	it('does not resurrect a bubble the transcript already answered', () => {
		handlers.handleMessage(frame([{ id: 'turn-q', content: 'steer me' }]));
		// The steer is absorbed: the SSE feed reports the user message on the LIVE
		// turn's id, and the pump broadcasts the cleared queue.
		pendingQueue.noteDelivered('session-1', 'steer me', 'turn-live');
		handlers.handleMessage(frame([]));
		expect(pendingIds()).toEqual([]);
		// A snapshot taken BEFORE the absorb arrives last — this is the stuck bubble.
		handlers.handleMessage(frame([{ id: 'turn-q', content: 'steer me' }]));
		expect(pendingIds()).toEqual([]);
	});

	it('shows a row the server put back after an interrupt', () => {
		handlers.handleMessage(frame([{ id: 'turn-q', content: 'steer me' }]));
		pendingQueue.noteDelivered('session-1', 'steer me', 'turn-live');
		handlers.handleMessage(frame([]));
		// Re-queued as a fresh turn row because the turn that swallowed it was
		// interrupted. Same text, new row — it must render.
		handlers.handleMessage(frame([{ id: 'turn-q2', content: 'steer me' }]));
		expect(pendingIds()).toEqual(['turn-q2']);
	});

	it('keeps a cancelling chip until the server withdrawal verdict, then restores the draft', () => {
		const entry = { id: 'client-a', clientMessageId: 'client-a', content: 'nope', queuedAt: 1 };
		pendingQueue.noteOptimistic('session-1', entry.id);
		pendingQueue.requestRemoval('session-1', entry);
		handlers.handleMessage(frame([])); // queue clearing must not hide a pending verdict
		expect(pendingIds()).toEqual(['client-a']);
		expect(state.queuedMessages[0].status).toBe('cancelling');
		expect(state.pendingRestorePrompt).toBeNull();
		handlers.handleMessage({
			type: 'queued_message_removal', session_id: 'session-1',
			message_id: 'turn-q', client_message_id: 'client-a', verdict: 'withdrawn',
		});
		expect(pendingIds()).toHaveLength(0);
		expect(state.pendingRestorePrompt).toBe('nope');
		expect(deps.wsSend).not.toHaveBeenCalled();
	});
});
