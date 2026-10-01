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
import { composerPreferences } from '../composer-preferences';
import { bindSessionOwner } from '../session-owner';

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
import { generateBranchName } from '$lib/api/git';

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
		pendingPeerMessages: [],
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
	const sendMessage = vi.fn<ReturnType<typeof createMessageSender>['sendMessage']>(async () => true);
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

describe('remembered permission on worktree creation', () => {
	it.each(['createWorktree', 'autoCreateWorktree'] as const)('carries the explicit preference through %s', async (entry) => {
		composerPreferences.set('permissionMode', 'bypassPermissions');
		post.mockResolvedValueOnce({ session_id: 'wt-1', branch_name: 'feat/picked', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
		const { sender, sendMessage, wsConn } = setup({ worktreeBranch: 'feat/picked', permissionModeOverride: composerPreferences.current.permissionMode });
		// Manual branch creation sends the prompt over WS after the POST; exercise
		// that real second leg rather than stopping at the fixture's delegate.
		sendMessage.mockImplementation((...args: Parameters<typeof sender.sendMessage>) => sender.sendMessage(...args));
		await sender[entry]('hello');
		if (entry === 'createWorktree') {
			expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({
				type: 'query', overrides: expect.objectContaining({ permission_mode: 'bypassPermissions' }),
			}));
		} else {
			expect(post).toHaveBeenCalledWith('/sessions/with-worktree', expect.objectContaining({
				overrides: expect.objectContaining({ permission_mode: 'bypassPermissions' }),
			}), { keepalive: true });
		}
	});
});

describe('one-time remembered new-branch choice (#1598 / #1528)', () => {
	const choice = { kind: 'branch' as const, projectId: 'proj-1', projectV2Id: null,
		branch: 'feat/picked', baseBranch: 'develop', createNew: true };
	const created = { session_id: 'wt-1', branch_name: 'feat/picked', base_branch: 'develop', worktree_path: '/w', status: 'ok' };

	beforeEach(() => {
		composerPreferences.set('checkout', choice);
		composerPreferences.set('project', { projectId: 'proj-1', projectV2Id: null });
		composerPreferences.set('model', 'preserved-model');
	});

	it.each([false, true])('consumes the choice only after successful creation (remote=%s)', async (remote) => {
		if (remote) composerPreferences.set('checkout', { ...choice, projectId: '', projectV2Id: 'shared-repo' });
		let finish!: (value: typeof created) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender, state } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true,
			...(remote ? { project: '', projectV2Id: 'shared-repo' } : {}) });
		const pending = sender.createWorktree();
		expect(composerPreferences.current.checkout?.branch).toBe(choice.branch);
		finish(created);
		await pending;
		expect(composerPreferences.current.checkout).toBeNull();
		expect(composerPreferences.current.model).toBe('preserved-model');
		expect(composerPreferences.current.project).toEqual({ projectId: 'proj-1', projectV2Id: null });
		expect(state()).toMatchObject({ sessionId: 'wt-1', worktreeBranch: choice.branch });
	});

	it('keeps the choice and draft when creation fails', async () => {
		post.mockRejectedValueOnce({ status: 409, detail: "Branch 'feat/picked' already exists" });
		const { sender, state } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true });
		await sender.createWorktree();
		expect(composerPreferences.current.checkout).toEqual(choice);
		expect(state()).toMatchObject({ sessionId: null, worktreeBranch: choice.branch });
	});

	it('does not clear a later composer selection when the first request completes', async () => {
		let finish!: (value: typeof created) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender, internal } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true });
		const pending = sender.createWorktree();
		internal.worktreeCreationGeneration++;
		const next = { ...choice, branch: 'feat/next-task' };
		composerPreferences.set('checkout', next);
		finish(created);
		await pending;
		expect(composerPreferences.current.checkout).toEqual(next);
	});

	it('consumes an unchanged choice even if the user navigated away', async () => {
		let finish!: (value: typeof created) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender, internal } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true });
		const pending = sender.createWorktree();
		internal.worktreeCreationGeneration++;
		finish(created);
		await pending;
		expect(composerPreferences.current.checkout).toBeNull();
	});

	it('does not consume a choice when creating on an existing branch', async () => {
		post.mockResolvedValueOnce(created);
		const { sender } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: false });
		await sender.createWorktree();
		expect(composerPreferences.current.checkout).toEqual(choice);
	});

	it('retains the choice when a response has no created session', async () => {
		post.mockResolvedValueOnce({ ...created, session_id: null });
		const { sender } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true });
		await sender.createWorktree();
		expect(composerPreferences.current.checkout).toEqual(choice);
	});

	it('fences the POST completion at an account boundary even without a checkout edit', async () => {
		let finish!: (value: typeof created) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender } = setup({ worktreeBranch: choice.branch, worktreeCreateNewBranch: true });
		const pending = sender.createWorktree();
		bindSessionOwner('owner-a', 'owner-b');
		// No checkout revision change: exercise the real owner epoch fence alone.
		finish(created);
		await pending;
		expect(composerPreferences.current.checkout).toEqual(choice);
	});

	it('does not consume a remembered choice when an automatic name happens to match', async () => {
		composerPreferences.set('checkout', { ...choice, branch: 'feat/auto-name' });
		post.mockResolvedValueOnce({ ...created, branch_name: 'feat/auto-name', query_started: true });
		const { sender } = setup();
		await sender.autoCreateWorktree('hello');
		expect(composerPreferences.current.checkout).toEqual({ ...choice, branch: 'feat/auto-name' });
	});
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

describe('back-to-back worktree creation keeps each session independent (#1528)', () => {
	const exists = (branch: string) => ({
		status: 409,
		detail: `Branch '${branch}' already exists, so it can't be created as a new branch for this session. Select it as an existing branch, or choose a different name.`,
	});

	it('mints a distinct branch when the generated name already exists, instead of sharing its checkout', async () => {
		post.mockRejectedValueOnce(exists('feat/auto-name'));
		post.mockImplementationOnce(async (_url: string, body: { branch_name: string }) => ({
			session_id: 'wt-2', branch_name: body.branch_name, base_branch: 'develop', worktree_path: '/w2', status: 'ok', query_started: true,
		}));
		const { sender, state } = setup();

		await sender.autoCreateWorktree('Live session effort and Fast mode');

		expect(post).toHaveBeenCalledTimes(2);
		const first = post.mock.calls[0][1] as { branch_name: string };
		const second = post.mock.calls[1][1] as { branch_name: string; create_new_branch: boolean; prompt: string };
		expect(first.branch_name).toBe('feat/auto-name');
		expect(second.branch_name).toMatch(/^feat\/auto-name-[a-z0-9]{4}$/);
		expect(second.create_new_branch).toBe(true);
		expect(second.prompt).toBe('Live session effort and Fast mode');
		expect(state()).toMatchObject({
			sessionId: 'wt-2', worktreeBranch: second.branch_name, selectedWorktreeBranch: second.branch_name, worktreeError: null,
		});
	});

	it('a branch name generated after navigation is never staged on the next compose', async () => {
		let resolveName!: (v: { branch_name: string }) => void;
		vi.mocked(generateBranchName).mockReturnValueOnce(new Promise((r) => { resolveName = r; }) as never);
		post.mockResolvedValueOnce({ session_id: 'wt-1', branch_name: 'fix/desktop-update', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
		const { sender, internal, state, deps } = setup();

		const pending = sender.autoCreateWorktree('Fix desktop update feedback');
		await vi.waitFor(() => expect(generateBranchName).toHaveBeenCalled());
		// The user starts the next task before the first one's name arrives.
		internal.worktreeCreationGeneration++;
		deps.update((s) => ({ ...s, messages: [], isCreatingWorktree: false, worktreeBranch: '', worktreeCreateNewBranch: false }));
		resolveName({ branch_name: 'fix/desktop-update' });
		await pending;

		// The first task still gets its own branch and session...
		expect(post).toHaveBeenCalledExactlyOnceWith('/sessions/with-worktree', expect.objectContaining({
			branch_name: 'fix/desktop-update', prompt: 'Fix desktop update feedback',
		}), { keepalive: true });
		// ...and the next compose inherits neither the name nor the session.
		expect(state()).toMatchObject({ sessionId: null, worktreeBranch: '', worktreeCreateNewBranch: false });
	});

	it('does not retry other conflicts', async () => {
		post.mockRejectedValueOnce({ status: 409, detail: "This project isn't checked out on this device yet." });
		const { sender, state } = setup();
		await sender.autoCreateWorktree('task');
		expect(post).toHaveBeenCalledOnce();
		expect(state().sessionId).toBeNull();
		expect(state().worktreeError).toContain("isn't checked out");
	});

	it('explains an explicit new branch that already exists, without retrying', async () => {
		post.mockRejectedValueOnce(exists('fix/desktop-update-feedback-and-fresh-ui'));
		const { sender, state } = setup({ worktreeBranch: 'fix/desktop-update-feedback-and-fresh-ui', worktreeCreateNewBranch: true });
		await sender.createWorktree('Live session effort and Fast mode');
		expect(post).toHaveBeenCalledOnce();
		expect(state().sessionId).toBeNull();
		expect(state().worktreeError).toBe(
			"Branch 'fix/desktop-update-feedback-and-fresh-ui' already exists. Select it as an existing branch, or choose a different name.",
		);
	});

	it.each(['createWorktree', 'autoCreateWorktree'] as const)(
		'%s finishing after navigation leaves the next compose\'s device pick alone',
		async (entry) => {
			let resolvePost!: (v: unknown) => void;
			post.mockReturnValueOnce(new Promise((r) => { resolvePost = r; }));
			const { sender, internal, state, deps } = setup({
				worktreeBranch: entry === 'createWorktree' ? 'fix/first-task' : '',
				deviceIntent: { id: 'runner-a', name: 'A' },
			} as Partial<ActiveSessionState>);

			const pending = sender[entry]('first task');
			await vi.waitFor(() => expect(post).toHaveBeenCalled());
			// Next compose: the user navigated and picked another device for task two.
			internal.worktreeCreationGeneration++;
			deps.update((s) => ({ ...s, deviceIntent: { id: 'runner-b', name: 'B' }, worktreeBranch: '' }));
			resolvePost({ session_id: 'wt-1', branch_name: 'fix/first-task', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
			await pending;

			expect(state().deviceIntent).toEqual({ id: 'runner-b', name: 'B' });
			expect(state().sessionId).toBeNull();
			expect(state().worktreeBranch).toBe('');
		},
	);

	it('carries effort/Fast overrides when navigation happens during the WS connect wait', async () => {
		post.mockResolvedValueOnce({ session_id: 'wt-1', branch_name: 'fix/first-task', base_branch: 'develop', worktree_path: '/w', status: 'ok' });
		const { sender, internal, wsConn } = setup({ worktreeBranch: 'fix/first-task', effortLevelOverride: 'high', fastMode: true } as Partial<ActiveSessionState>);
		let connected = false;
		wsConn.isConnected.mockImplementation(() => connected);
		wsConn.connect.mockImplementation(() => { internal.worktreeCreationGeneration++; connected = true; });

		await sender.createWorktree('first task');

		expect(wsConn.send).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
			type: 'query', session_id: 'wt-1', prompt: 'first task',
			overrides: expect.objectContaining({ effort: 'high', fast: true }),
		}));
	});
});
