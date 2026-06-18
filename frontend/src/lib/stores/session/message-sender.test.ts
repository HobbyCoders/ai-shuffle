import { describe, expect, it, vi } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';

/**
 * Regression tests for the "work in main" worktree bug.
 *
 * A worktree-intended NEW session (executionMode 'worktree', no sessionId yet)
 * must never fall through to the plain WS query path — that path carries no
 * worktree_id, so the backend would create the session in the project's MAIN
 * checkout. This happens if a second send slips in while autoCreateWorktree is
 * still running (e.g. the user presses Enter during the multi-second auto-name
 * step).
 */

function baseState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: null,
		title: 'New Chat',
		profile: 'claude-code',
		project: 'proj-1',
		messages: [],
		isStreaming: false,
		executionMode: 'local',
		worktreeId: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeMode: 'new',
		worktreeCreateNewBranch: false,
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		isReopeningWorktree: false,
		worktreeDeleted: false,
		worktreeError: null,
		error: null,
		// Anything the tested paths don't read can stay loose.
		...overrides,
	} as unknown as ActiveSessionState;
}

function makeDeps(initial: ActiveSessionState) {
	let state = initial;
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
	};

	const wsConn = {
		isConnected: vi.fn(() => true),
		connect: vi.fn(),
		send: vi.fn((_data: Record<string, unknown>) => true),
		resetLastMessageTime: vi.fn(),
		resetReconnectAttempts: vi.fn(),
	};

	const autoCreateWorktree = vi.fn(async () => {});
	const createWorktree = vi.fn(async () => {});
	const innerSendMessage = vi.fn(async () => true);

	const deps: MessageSenderDeps = {
		update,
		getState,
		internal,
		wsConn,
		sessionPoller: { stop: vi.fn(), startStreaming: vi.fn() },
		sessionState: { removeSession: vi.fn() },
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => ({
			sendMessage: innerSendMessage,
			createWorktree,
			autoCreateWorktree,
		}),
	};

	return { deps, wsConn, getState, autoCreateWorktree, createWorktree };
}

describe('message-sender worktree send backstop', () => {
	it('suppresses the send to main when worktree creation is still in flight', async () => {
		// executionMode worktree, no sessionId, but an optimistic message is already
		// present → autoCreateWorktree is mid-flight.
		const { deps, wsConn, getState } = makeDeps(baseState({
			executionMode: 'worktree',
			sessionId: null,
			isCreatingWorktree: true,
			messages: [{ id: 'user-1', role: 'user', content: 'first' } as any],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('second message while creating');

		expect(result).toBe(false);
		// CRITICAL: no WS query may be sent (that would run in main).
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(0);
		expect(getState().worktreeError).toMatch(/setting up the worktree/i);
	});

	it('routes a fresh worktree+auto first message to autoCreateWorktree (not main)', async () => {
		const { deps, wsConn, autoCreateWorktree } = makeDeps(baseState({
			executionMode: 'worktree',
			sessionId: null,
			worktreeBranch: '',
			messages: [],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('build me a thing');

		expect(result).toBe(true);
		expect(autoCreateWorktree).toHaveBeenCalledTimes(1);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(0);
	});

	it('still sends a normal local first message over WS', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			executionMode: 'local',
			sessionId: null,
			messages: [],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('hello local');

		expect(result).toBe(true);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).worktree_id).toBeUndefined();
	});
});
