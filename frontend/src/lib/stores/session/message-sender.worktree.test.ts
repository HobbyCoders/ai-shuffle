/**
 * autoCreateWorktree — the "new worktree" first send: one keepalive POST
 * creates the worktree, the session and the first turn. These tests pin the
 * commit, the navigate-away guard and the failure rollback.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from '$lib/components/chat/attach-disable';

vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'feat/auto-name' })),
	generateBranchNameLocal: vi.fn(() => 'local/auto-name'),
}));
const post = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { post, get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));
vi.mock('./v2/feed-controller', () => ({
	feedController: { start: vi.fn(), stop: vi.fn(), isFeeding: vi.fn(() => false) },
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

import { feedController } from './v2/feed-controller';

function setup(overrides: Partial<ActiveSessionState> = {}) {
	let state = {
		sessionId: null,
		title: 'New Chat',
		mode: 'code',
		profile: 'claude-code',
		project: 'proj-1',
		projectV2Id: null,
		messages: [],
		queuedMessages: [],
		activeTasks: [{ id: 'old-task' }],
		isStreaming: false,
		worktreeIntent: 'worktree',
		worktreeBaseBranch: 'develop',
		worktreeBranch: '',
		isCreatingWorktree: false,
		worktreeError: null,
		error: null,
		fastMode: false,
		deviceIntent: null,
		...overrides,
	} as unknown as ActiveSessionState;
	const internal: InternalMutableState = {
		pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null,
	};
	const wsConn = { isConnected: vi.fn(() => true), connect: vi.fn(), send: vi.fn(() => true), resetReconnectAttempts: vi.fn() };
	const sendMessage = vi.fn(async () => true);
	const deps: MessageSenderDeps = {
		update: (fn) => { state = fn(state); },
		getState: () => state,
		internal,
		wsConn,
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => ({ sendMessage, createWorktree: vi.fn(), autoCreateWorktree: vi.fn() }),
	};
	return { sender: createMessageSender(deps), deps, internal, wsConn, sendMessage, state: () => state };
}

beforeEach(() => {
	post.mockReset();
	vi.mocked(feedController.start).mockClear();
});

describe('autoCreateWorktree', () => {
	it('commits the new session in one step and opens its feed when the backend started the turn', async () => {
		post.mockResolvedValueOnce({ session_id: 'wt-1', branch_name: 'feat/auto-name', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
		const { sender, deps, state, sendMessage } = setup();
		const prompt = `  ${'x'.repeat(120)}  `;

		await sender.autoCreateWorktree(prompt);

		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions/with-worktree', expect.objectContaining({
			project_id: 'proj-1', branch_name: 'feat/auto-name', base_branch: 'develop', prompt: 'x'.repeat(120), create_new_branch: true,
		}), { keepalive: true });
		expect(state()).toMatchObject({
			sessionId: 'wt-1',
			title: `${'x'.repeat(100)}...`,
			selectedWorktreeBranch: 'feat/auto-name',
			isCreatingWorktree: false,
			isStreaming: true,
			activeTasks: [],
		});
		expect(state().messages.map((m) => m.content)).toEqual(['x'.repeat(120)]);
		expect(feedController.start).toHaveBeenCalledExactlyOnceWith('wt-1');
		expect(deps.persistSession).toHaveBeenCalledOnce();
		// The backend already runs the turn; a second send would run it twice.
		expect(sendMessage).not.toHaveBeenCalled();
	});

	it('leaves the view the user switched to alone, and sends the turn itself only if the backend did not', async () => {
		let resolvePost!: (v: unknown) => void;
		post.mockReturnValueOnce(new Promise((r) => { resolvePost = r; }));
		const { sender, internal, state, wsConn, sendMessage } = setup();

		const pending = sender.autoCreateWorktree('build it');
		await vi.waitFor(() => expect(post).toHaveBeenCalled());
		// The user opens another session mid-POST.
		internal.worktreeCreationGeneration++;
		const before = state();
		resolvePost({ session_id: 'wt-2', branch_name: 'b', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: false });
		await pending;

		expect(state().sessionId).toBeNull();
		expect(state()).toBe(before);
		expect(feedController.start).not.toHaveBeenCalled();
		expect(sendMessage).not.toHaveBeenCalled();
		expect(wsConn.send).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: 'query', session_id: 'wt-2', prompt: 'build it' }));
	});

	it('rolls back the optimistic message and explains a 403, but stays silent once the user has left', async () => {
		post.mockRejectedValueOnce({ status: 403, detail: "Permission denied — you don't have access to this resource" });
		const first = setup();
		await first.sender.autoCreateWorktree('try');
		expect(first.state().messages).toEqual([]);
		expect(first.state().isCreatingWorktree).toBe(false);
		expect(first.state().worktreeError).toBe('Permission denied creating worktree. Check folder permissions on the server.');
		expect(first.state().worktreeBranch).toBe('feat/auto-name');

		let rejectPost!: (e: unknown) => void;
		post.mockReturnValueOnce(new Promise((_, rej) => { rejectPost = rej; }));
		const second = setup();
		const pending = second.sender.autoCreateWorktree('try again');
		await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));
		second.internal.worktreeCreationGeneration++;
		rejectPost({ status: 500, detail: 'Internal server error' });
		await pending;
		expect(second.state().worktreeError).toBeNull();
	});

	it('refuses attachments for a remote-only project before any request', async () => {
		const { sender, state } = setup({ project: '', projectV2Id: 'v2-remote' } as Partial<ActiveSessionState>);
		await sender.autoCreateWorktree('look at this', ['/workspace/uploads/a.png']);
		expect(post).not.toHaveBeenCalled();
		expect(state().worktreeError).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
		expect(state().messages).toEqual([]);
	});
});
