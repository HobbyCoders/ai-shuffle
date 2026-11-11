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
import { bumpInitGeneration } from './session-init';

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

function setup(overrides: Partial<ActiveSessionState> = {}, options: { realActiveSession?: boolean } = {}) {
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
	const wsConn = { isConnected: vi.fn(() => true), connect: vi.fn(), send: vi.fn((_frame: Record<string, unknown>) => true), resetReconnectAttempts: vi.fn() };
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
		// realActiveSession: the store's own entry points, as activeSession has them.
		getActiveSession: () => options.realActiveSession ? sender
			: ({ sendMessage, createWorktree: vi.fn(), autoCreateWorktree: vi.fn() }),
	};
	const sender = createMessageSender(deps);
	return { sender, deps, internal, wsConn, sendMessage, state: () => state };
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
		const project = remote ? {projectId:'',projectV2Id:'shared-repo'} : {projectId:'proj-1',projectV2Id:null};
		if (remote) {
			composerPreferences.set('checkout', {...choice, ...project});
			composerPreferences.set('project', project);
		}
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
		expect(composerPreferences.current.project).toEqual(project);
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

describe('remembered existing-branch pick is one-time (#1528)', () => {
	// The New branch modal creates the branch up front, so it and a branch picked
	// from the list both send create_new_branch=false. The first session spends it.
	const created = { kind: 'branch' as const, projectId: 'proj-1', projectV2Id: null,
		branch: 'live/1528-explicit', baseBranch: 'main', createNew: false };
	const ok = { session_id: 'wt-c', branch_name: created.branch, base_branch: 'main', worktree_path: '/w', status: 'ok' };
	const staged = { worktreeBranch: created.branch, worktreeBaseBranch: 'main', worktreeCreateNewBranch: false };

	beforeEach(() => {
		composerPreferences.set('model', 'preserved-model');
		composerPreferences.set('checkout', created);
	});

	it('claims the choice while the POST is in flight and consumes it after success', async () => {
		let finish!: (value: typeof ok) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender, state } = setup(staged);
		const pending = sender.createWorktree();
		expect(post).toHaveBeenCalledWith('/sessions/with-worktree', expect.objectContaining({
			branch_name: created.branch, create_new_branch: false }));
		expect(composerPreferences.checkoutClaimed).toBe(true);
		expect(composerPreferences.current.checkout).toEqual(created);
		finish(ok);
		await pending;
		expect(composerPreferences.checkoutClaimed).toBe(false);
		expect(composerPreferences.current.checkout).toBeNull();
		expect(composerPreferences.current.model).toBe('preserved-model');
		expect(state()).toMatchObject({ sessionId: 'wt-c', worktreeBranch: created.branch });
	});

	it('keeps the choice and the draft branch when creation fails', async () => {
		post.mockRejectedValueOnce({ status: 500, detail: 'worktree add failed' });
		const { sender, state } = setup(staged);
		await sender.createWorktree();
		expect(composerPreferences.checkoutClaimed).toBe(false);
		expect(composerPreferences.current.checkout).toEqual(created);
		expect(state()).toMatchObject({ sessionId: null, worktreeBranch: created.branch, isCreatingWorktree: false });
		expect(state().worktreeError).toBeTruthy();
	});

	it('does not consume the choice for a request on a different branch', async () => {
		post.mockResolvedValueOnce({ ...ok, branch_name: 'feature/other' });
		const { sender } = setup({ ...staged, worktreeBranch: 'feature/other' });
		await sender.createWorktree();
		expect(composerPreferences.current.checkout).toEqual(created);
	});
});

describe('autoCreateWorktree', () => {
	// #1636: the outcome decides whether the composer keeps the text.
	it('resolves false for a refusal before any request and for a failed create', async () => {
		const noProject = setup({ project: '', projectV2Id: null });
		expect(await noProject.sender.autoCreateWorktree('hi')).toBe(false);
		expect(noProject.state().worktreeError).toBe('Please select a project');
		expect(post).not.toHaveBeenCalled();

		post.mockRejectedValueOnce({ status: 500, detail: 'Internal server error' });
		const failed = setup();
		expect(await failed.sender.autoCreateWorktree('hi')).toBe(false);
		expect(failed.state().messages).toEqual([]);
	});

	it('resolves true once the backend accepted the create', async () => {
		post.mockResolvedValueOnce({ session_id: 'wt-ok', branch_name: 'b', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
		expect(await setup().sender.autoCreateWorktree('hi')).toBe(true);
	});

	it('resolves false and drops the echo when the fallback first turn is refused', async () => {
		post.mockResolvedValueOnce({ session_id: 'wt-fb', branch_name: 'b', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: false });
		const { sender, sendMessage, state } = setup();
		sendMessage.mockResolvedValueOnce(false);
		expect(await sender.autoCreateWorktree('hi')).toBe(false);
		expect(state().messages).toEqual([]);
	});

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

	it('sends the local row\'s own command id with the create, so the delivered row reconciles by id (#1351)', async () => {
		// The server stores the prompt with each upload's checkout path, so the
		// delivered message.user text differs from this row: only the id matches.
		post.mockResolvedValueOnce({ session_id: 'wt-id', branch_name: 'b', base_branch: 'develop', worktree_path: '/w', status: 'ok', query_started: true });
		const { sender, state } = setup();

		await sender.autoCreateWorktree('look at @/ws/proj/uploads/a.txt', ['/ws/proj/uploads/a.txt']);

		const body = post.mock.calls[0][1] as { client_message_id?: string };
		expect(body.client_message_id).toMatch(/^client-[0-9a-f]{32}$/);
		expect(state().messages).toHaveLength(1);
		expect(state().messages[0].commandId).toBe(body.client_message_id);
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

describe('a second send while the first send creates the worktree (#1444)', () => {
	const ok = { session_id: 'wt-c', branch_name: 'live/staged', base_branch: 'main', worktree_path: '/w', status: 'ok' };
	const staged = { worktreeBranch: 'live/staged', worktreeBaseBranch: 'main', worktreeCreateNewBranch: false };

	it('waits for the create and goes to the chat it created, never starting a second create', async () => {
		let finish!: (value: typeof ok) => void;
		post.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		const { sender, wsConn } = setup(staged, { realActiveSession: true });
		const first = sender.sendMessage('first');
		const second = sender.sendMessage('second');
		await new Promise(resolve => setTimeout(resolve, 20));
		finish(ok);
		expect(await first).toBe(true);
		expect(await second).toBe(true);
		expect(post.mock.calls.filter(([path]) => path === '/sessions/with-worktree')).toHaveLength(1);
		const queries = wsConn.send.mock.calls.map(([frame]) => frame).filter(frame => frame.type === 'query');
		expect(queries.map(frame => [frame.prompt, frame.session_id])).toEqual([['first', 'wt-c'], ['second', 'wt-c']]);
	});

	it('keeps the second send unsent when the create fails', async () => {
		let fail!: (reason: unknown) => void;
		post.mockReturnValueOnce(new Promise((_, reject) => { fail = reject; }));
		const { sender, wsConn } = setup(staged, { realActiveSession: true });
		const first = sender.sendMessage('first');
		const second = sender.sendMessage('second');
		await new Promise(resolve => setTimeout(resolve, 20));
		fail({ status: 500, detail: 'worktree add failed' });
		expect(await first).toBe(false);
		expect(await second).toBe(false);
		expect(post).toHaveBeenCalledTimes(1);
		expect(wsConn.send.mock.calls.filter(([frame]) => frame.type === 'query')).toEqual([]);
	});

	// Review round 3: the outcome is read when the queued send's turn comes,
	// so a creation that starts after the send was queued still counts.
	it('keeps a send queued before the first send started its create unsent when that create fails', async () => {
		post.mockRejectedValue({ status: 500, detail: 'worktree add failed' });
		const { sender } = setup(staged, { realActiveSession: true });
		// The composer accepted the first send but is still doing its own waits.
		const turn = sender.reserveSend();
		const second = sender.sendMessage('second');
		const first = sender.sendMessage('first', { turn });
		expect(await first).toBe(false);
		expect(await second).toBe(false);
		expect(post.mock.calls.filter(([path]) => path === '/sessions/with-worktree')).toHaveLength(1);
	});

	// Review round 1 (S2): each create's own first send runs in its own
	// create's turn, whichever create ends first.
	it('sends each create\'s first query when two creates from different composes end oldest first', async () => {
		vi.useFakeTimers();
		try {
			let finishA!: (value: typeof ok) => void;
			let finishB!: (value: typeof ok) => void;
			post.mockReturnValueOnce(new Promise(resolve => { finishA = resolve; }))
				.mockReturnValueOnce(new Promise(resolve => { finishB = resolve; }));
			const { sender, deps, internal, wsConn } = setup(staged, { realActiveSession: true });
			const a = sender.sendMessage('task a');
			await vi.advanceTimersByTimeAsync(10);
			expect(post).toHaveBeenCalledTimes(1);
			// The user opens another New chat and starts a second staged create.
			bumpInitGeneration();
			internal.worktreeCreationGeneration++;
			deps.update(s => ({ ...s, messages: [], sessionId: null, isCreatingWorktree: false, worktreeBranch: 'live/second' }));
			const b = sender.sendMessage('task b');
			await vi.advanceTimersByTimeAsync(10);
			expect(post).toHaveBeenCalledTimes(2);
			finishA({ ...ok, session_id: 'wt-a' });
			expect(await a).toBe(true);
			finishB({ ...ok, session_id: 'wt-b', branch_name: 'live/second' });
			await vi.advanceTimersByTimeAsync(16_000);
			expect(await b).toBe(true);
			const queries = wsConn.send.mock.calls.map(([frame]) => frame).filter(frame => frame.type === 'query');
			expect(queries.map(frame => [frame.prompt, frame.session_id])).toContainEqual(['task b', 'wt-b']);
		} finally { vi.clearAllTimers(); vi.useRealTimers(); }
	});
});

describe('a place in line is the only way to send (#1444)', () => {
	const prompts = (wsConn: ReturnType<typeof setup>['wsConn']) =>
		wsConn.send.mock.calls.map(([frame]) => frame).filter(frame => frame.type === 'query').map(frame => frame.prompt);

	it('sends nothing on a cancelled place, and frees the line for the next send', async () => {
		const { sender, wsConn } = setup({ sessionId: 'old-chat', worktreeIntent: 'local' }, { realActiveSession: true });
		const turn = sender.reserveSend();
		const later = sender.sendMessage('later');
		turn.cancel();
		turn.cancel();
		expect(await later).toBe(true);
		// The cancelled submission's continuation reaches the store after all.
		expect(await sender.sendMessage('called off', { turn })).toBe(false);
		expect(prompts(wsConn)).toEqual(['later']);
	});

	it('sends nothing on a place already used', async () => {
		const { sender, wsConn } = setup({ sessionId: 'old-chat', worktreeIntent: 'local' }, { realActiveSession: true });
		const turn = sender.reserveSend();
		expect(await sender.sendMessage('first', { turn })).toBe(true);
		expect(await sender.sendMessage('again', { turn })).toBe(false);
		expect(prompts(wsConn)).toEqual(['first']);
	});

	it('frees the line when a used place throws before its frame', async () => {
		const { sender, wsConn } = setup({ sessionId: 'old-chat', worktreeIntent: 'local' }, { realActiveSession: true });
		const turn = sender.reserveSend();
		const first = sender.sendMessage('first', { turn, notesForm: { pathPrompt: 'path first', runsHere: async () => { throw new Error('ownership check failed'); } } });
		const rejected = expect(first).rejects.toThrow('ownership check failed');
		const later = sender.sendMessage('later');
		await rejected;
		expect(await later).toBe(true);
		expect(prompts(wsConn)).toEqual(['later']);
	});

	it('writes an ordinary single send synchronously', async () => {
		const { sender, wsConn } = setup({ sessionId: 'old-chat', worktreeIntent: 'local' }, { realActiveSession: true });
		const sent = sender.sendMessage('plain');
		expect(prompts(wsConn)).toEqual(['plain']);
		expect(await sent).toBe(true);
	});

	it('keeps a new-chat send queued during reconnect unsent when the first send is refused', async () => {
		vi.useFakeTimers();
		try {
			const { sender, deps, wsConn } = setup({ worktreeIntent: 'local' }, { realActiveSession: true });
			wsConn.isConnected.mockReturnValue(false);
			const first = sender.sendMessage('first');
			const second = sender.sendMessage('second');
			wsConn.isConnected.mockReturnValue(true);
			await vi.advanceTimersByTimeAsync(100);
			expect(await first).toBe(true);
			expect(prompts(wsConn)).toEqual(['first']);
			// The correlated refusal withdraws the first send's echo.
			deps.update(s => ({ ...s, messages: [] }));
			await vi.advanceTimersByTimeAsync(100);
			expect(await second).toBe(false);
			expect(prompts(wsConn)).toEqual(['first']);
		} finally { vi.clearAllTimers(); vi.useRealTimers(); }
	});

	// Review round 1 (S1): a chat adopted from a compose keeps that compose's
	// lane while a send of it is open, across a leave and reopen.
	it('keeps a send queued in a new chat ahead of one made after leaving and reopening the chat it created', async () => {
		vi.useFakeTimers();
		try {
			const { sender, deps, wsConn } = setup({ worktreeIntent: 'local' }, { realActiveSession: true });
			const first = sender.sendMessage('first');
			const second = sender.sendMessage('second');
			expect(prompts(wsConn)).toEqual(['first']);
			// The socket drops, then the chat is adopted: the first send's turn
			// ends and the second waits for the socket in its turn.
			wsConn.isConnected.mockReturnValue(false);
			deps.update(s => ({ ...s, sessionId: 'chat-s' }));
			await vi.advanceTimersByTimeAsync(60);
			// Leave the chat and reopen it; the socket opens and a third send is made.
			bumpInitGeneration();
			wsConn.isConnected.mockReturnValue(true);
			const third = sender.sendMessage('third');
			await vi.advanceTimersByTimeAsync(200);
			expect(await first).toBe(true);
			expect(await second).toBe(true);
			expect(await third).toBe(true);
			expect(prompts(wsConn)).toEqual(['first', 'second', 'third']);
		} finally { vi.clearAllTimers(); vi.useRealTimers(); }
	});

	it('still sends a retry made after the first new-chat send was refused', async () => {
		const { sender, deps, wsConn } = setup({ worktreeIntent: 'local' }, { realActiveSession: true });
		expect(await sender.sendMessage('first')).toBe(true);
		deps.update(s => ({ ...s, messages: [] }));
		expect(await sender.sendMessage('retry')).toBe(true);
		expect(prompts(wsConn)).toEqual(['first', 'retry']);
	});
});
