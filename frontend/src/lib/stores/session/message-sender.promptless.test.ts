/**
 * startSessionWithoutPrompt — the call button on the new-session composer
 * creates the composed session with no first message, then calls it (#1616).
 * These tests pin which creator each composer state uses, that the composer's
 * picks travel, the one-time branch choice, adoption, and the failure paths.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import { composerPreferences } from '../composer-preferences';

vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
vi.mock('$lib/api/git', () => ({
	generateBranchName: vi.fn(async () => ({ branch_name: 'feat/from-prompt' })),
	generateBranchNameLocal: vi.fn((message: string, prefix?: string) => `${prefix ?? 'feature'}/auto-x1`),
}));
const post = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { post, get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));
vi.mock('./v2/feed-controller', () => ({
	feedController: { start: vi.fn(), stop: vi.fn(), isFeeding: vi.fn(() => false), clearOptimisticSend: vi.fn(), isReducerStreaming: vi.fn(() => false) },
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

import { feedController } from './v2/feed-controller';
import { generateBranchNameLocal } from '$lib/api/git';

function setup(overrides: Partial<ActiveSessionState> = {}) {
	let state = {
		sessionId: null,
		title: 'New Chat',
		mode: 'code',
		profile: 'agent-a',
		project: 'proj-1',
		projectV2Id: null,
		messages: [],
		queuedMessages: [],
		pendingPeerMessages: [],
		activeTasks: [],
		isStreaming: false,
		isRestoringSession: false,
		worktreeIntent: 'local',
		worktreeMode: 'new',
		worktreeId: null,
		worktreeBaseBranch: 'main',
		worktreeBranch: '',
		worktreeCreateNewBranch: false,
		isCreatingWorktree: false,
		worktreeError: null,
		error: null,
		modelOverride: 'opus',
		permissionModeOverride: 'acceptEdits',
		effortLevelOverride: 'high',
		fastMode: false,
		contextWindowChoice: null,
		workspaceProjectIds: [],
		deviceIntent: null,
		...overrides,
	} as unknown as ActiveSessionState;
	const internal: InternalMutableState = {
		pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
		stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null,
	};
	const wsConn = { isConnected: vi.fn(() => true), connect: vi.fn(), send: vi.fn(() => true), resetReconnectAttempts: vi.fn() };
	const ephemeral = { remove: vi.fn() };
	const appData = { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() };
	let sender!: ReturnType<typeof createMessageSender>;
	const deps: MessageSenderDeps = {
		update: (fn) => { state = fn(state); },
		getState: () => state,
		internal,
		wsConn,
		sessionEphemeralCache: ephemeral,
		appData,
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		// The real creators, as the store wires them.
		getActiveSession: () => ({
			sendMessage: (...args) => sender.sendMessage(...args),
			createWorktree: (...args) => sender.createWorktree(...args),
			autoCreateWorktree: (...args) => sender.autoCreateWorktree(...args),
		}),
	};
	sender = createMessageSender(deps);
	return { sender, deps, internal, wsConn, ephemeral, appData, state: () => state };
}

function expectAdopted(h: ReturnType<typeof setup>, id: string) {
	expect(h.state().sessionId).toBe(id);
	expect(h.wsConn.send).toHaveBeenCalledWith({ type: 'watch_session', session_id: id });
	expect(feedController.start).toHaveBeenCalledWith(id);
	expect(h.ephemeral.remove).toHaveBeenCalledWith('__new_chat__');
	expect(h.appData.loadSessions).toHaveBeenCalled();
	expect(h.deps.persistSession).toHaveBeenCalled();
	// Nothing is sent: no turn starts until the user speaks or types.
	expect(h.wsConn.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'query' }));
}

beforeEach(() => {
	post.mockReset();
	vi.mocked(feedController.start).mockClear();
	vi.mocked(generateBranchNameLocal).mockClear();
	composerPreferences.set('checkout', null);
});

describe('a local or chat compose', () => {
	it('creates the session with the composer picks and opens it', async () => {
		post.mockResolvedValueOnce({ session_id: 'new-1', mode: 'code' });
		const h = setup();

		await expect(h.sender.startSessionWithoutPrompt()).resolves.toBe('new-1');

		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions', expect.objectContaining({
			project_id: 'proj-1', profile_id: 'agent-a', mode: 'code',
			overrides: expect.objectContaining({ model: 'opus', permission_mode: 'acceptEdits', effort: 'high', fast: false }),
		}));
		expect(post.mock.calls[0][1].worktree_id).toBeUndefined();
		expectAdopted(h, 'new-1');
	});

	it('creates a projectless chat-lane conversation', async () => {
		post.mockResolvedValueOnce({ session_id: 'chat-1', mode: 'chat' });
		const h = setup({ mode: 'chat', project: '', permissionModeOverride: null });

		await h.sender.startSessionWithoutPrompt();

		const body = post.mock.calls[0][1];
		expect(body.mode).toBe('chat');
		expect(body.project_id).toBeUndefined();
		expect(body.overrides.workspace_project_ids).toBeUndefined();
	});

	it('names a remote-only project by its V2 id', async () => {
		post.mockResolvedValueOnce({ session_id: 'v2-1', mode: 'code' });
		const h = setup({ project: '', projectV2Id: 'pv2-9' });

		await h.sender.startSessionWithoutPrompt();

		expect(post.mock.calls[0][1]).toMatchObject({ project_v2_id: 'pv2-9' });
		expect(post.mock.calls[0][1].project_id).toBeUndefined();
	});

	it('runs a picked legacy worktree in that worktree', async () => {
		post.mockResolvedValueOnce({ session_id: 'wt-legacy', mode: 'code' });
		const h = setup({ worktreeIntent: 'worktree', worktreeMode: 'existing', worktreeId: 'wt-7' });

		await h.sender.startSessionWithoutPrompt();

		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions', expect.objectContaining({ worktree_id: 'wt-7' }));
	});

	it('shows a failed create where a failed send shows and opens nothing', async () => {
		post.mockRejectedValueOnce({ status: 400, detail: 'That agent no longer exists — pick another one.' });
		const h = setup();

		await expect(h.sender.startSessionWithoutPrompt()).resolves.toBeNull();

		expect(h.state().error).toBe('That agent no longer exists — pick another one.');
		expect(h.state().sessionId).toBeNull();
		expect(feedController.start).not.toHaveBeenCalled();
	});

	it('leaves the view alone when the user navigated away mid-create', async () => {
		let finish!: (value: unknown) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const h = setup();
		const pending = h.sender.startSessionWithoutPrompt();
		h.internal.worktreeCreationGeneration++;
		finish({ session_id: 'late', mode: 'code' });

		await expect(pending).resolves.toBeNull();
		expect(h.state().sessionId).toBeNull();
		expect(feedController.start).not.toHaveBeenCalled();
	});

	it('runs one create for overlapping presses and refuses a typed send meanwhile', async () => {
		let finish!: (value: unknown) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const h = setup();
		const first = h.sender.startSessionWithoutPrompt();
		const second = h.sender.startSessionWithoutPrompt();

		await expect(h.sender.sendMessage('hello')).resolves.toBe(false);
		expect(h.wsConn.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'query' }));
		expect(h.state().error).toMatch(/still being created/);

		finish({ session_id: 'once', mode: 'code' });
		await expect(first).resolves.toBe('once');
		await expect(second).resolves.toBe('once');
		expect(post).toHaveBeenCalledTimes(1);
	});

	it('refuses a picked runner on another device without creating anything', async () => {
		const h = setup({ deviceIntent: { id: 'worker-1', name: 'Studio PC' } });

		await expect(h.sender.startSessionWithoutPrompt()).resolves.toBeNull();

		expect(post).not.toHaveBeenCalled();
		expect(h.state().error).toMatch(/Switch the runner to Local.*Studio PC/);
	});
});

describe('a worktree compose', () => {
	const created = (branch: string) => ({ session_id: 'wt-1', branch_name: branch, base_branch: 'main', worktree_path: '/w', status: 'active', query_started: false });

	it('creates a staged branch through createWorktree, carries the picks and spends the one-time choice', async () => {
		const choice = { kind: 'branch' as const, projectId: 'proj-1', projectV2Id: null, branch: 'feat/picked', baseBranch: 'main', createNew: true };
		composerPreferences.set('project', { projectId: 'proj-1', projectV2Id: null });
		composerPreferences.set('checkout', choice);
		post.mockResolvedValueOnce(created('feat/picked'));
		const h = setup({ worktreeIntent: 'worktree', worktreeBranch: 'feat/picked', worktreeCreateNewBranch: true });

		await expect(h.sender.startSessionWithoutPrompt()).resolves.toBe('wt-1');

		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions/with-worktree', expect.objectContaining({
			project_id: 'proj-1', branch_name: 'feat/picked', create_new_branch: true,
			overrides: expect.objectContaining({ model: 'opus', permission_mode: 'acceptEdits' }),
		}));
		expect(post.mock.calls[0][1].prompt).toBeUndefined();
		expect(composerPreferences.current.checkout).toBeNull();
		expectAdopted(h, 'wt-1');
		expect(h.state().selectedWorktreeBranch).toBe('feat/picked');
	});

	it('names an Auto branch for the call, since there is no message to name it from', async () => {
		post.mockResolvedValueOnce(created('call/auto-x1'));
		const h = setup({ worktreeIntent: 'worktree' });

		await h.sender.startSessionWithoutPrompt();

		expect(generateBranchNameLocal).toHaveBeenCalledWith('', 'call');
		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions/with-worktree', expect.objectContaining({
			branch_name: 'call/auto-x1', create_new_branch: true,
		}));
		expectAdopted(h, 'wt-1');
	});

	it('puts the chip back to Auto and shows the error inline when the create fails', async () => {
		post.mockRejectedValueOnce({ status: 409, detail: 'This project isn\'t checked out on this device yet.' });
		const h = setup({ worktreeIntent: 'worktree' });

		await expect(h.sender.startSessionWithoutPrompt()).resolves.toBeNull();

		expect(h.state().worktreeBranch).toBe('');
		expect(h.state().worktreeCreateNewBranch).toBe(false);
		expect(h.state().worktreeError).toBeTruthy();
		expect(h.state().sessionId).toBeNull();
		expect(feedController.start).not.toHaveBeenCalled();
	});
});
