import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import { assistantState } from '../assistant';
import type { AssistantState } from '$lib/api/assistant';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import { reopenWorktree } from '$lib/api/git';
import { api } from '$lib/api/client';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from '$lib/components/chat/attach-disable';
import { pendingQueue } from './pending-queue';
import { getPersistedDeviceIntent, setPersistedDeviceIntent } from '../worker-selection';
import { composerPreferences } from '../composer-preferences';
const voice = vi.hoisted(() => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn(() => true) }));
vi.mock('$lib/services/voiceCall', () => voice);
const context = vi.hoisted(() => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
vi.mock('$lib/services/voiceExecutionContext', () => context);

// message-sender pulls these from $lib/api/git at module load. Only reopenWorktree
// is exercised here (the best-effort reopen for cleaned-up worktree sessions); the
// rest are stubbed so the import binding resolves.
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'auto/x' })),
	generateBranchNameLocal: vi.fn(() => 'auto/x'),
}));

const apiMocks = vi.hoisted(() => ({
	post: vi.fn(),
}));

vi.mock('$lib/api/client', () => ({
	api: {
		post: apiMocks.post,
		get: vi.fn(),
		put: vi.fn(),
		delete: vi.fn(),
	},
}));

/**
 * Regression tests for the "work in main" worktree bug.
 *
 * A worktree-intended NEW session (worktreeIntent 'worktree', no sessionId yet)
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
		projectV2Id: null,
		messages: [],
		queuedMessages: [],
		pendingPeerMessages: [],
		isStreaming: false,
		worktreeIntent: 'local',
		worktreeId: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeMode: 'new',
		worktreeCreateNewBranch: false,
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
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
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
	};

	const wsConn = {
		isConnected: vi.fn(() => true),
		connect: vi.fn(),
		send: vi.fn((_data: Record<string, unknown>) => true),
		resetReconnectAttempts: vi.fn(),
	};

	const autoCreateWorktree = vi.fn(async () => true);
	const createWorktree = vi.fn(async (): Promise<string | null> => null);
	const innerSendMessage = vi.fn(async () => true);

	const deps: MessageSenderDeps = {
		update,
		getState,
		internal,
		wsConn,
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

	return { deps, wsConn, getState, internal, autoCreateWorktree, createWorktree };
}

describe('remembered permission on first send', () => {
	it.each([
		{}, { deviceIntent: { id: 'runner-1', name: 'Runner' } },
		{ worktreeIntent: 'worktree' as const, worktreeMode: 'existing' as const, worktreeId: 'wt-1' },
	])('carries the explicit preference through session creation (%j)', async (selection) => {
		composerPreferences.set('permissionMode', 'bypassPermissions');
		const { deps, wsConn } = makeDeps(baseState({ ...selection, permissionModeOverride: composerPreferences.current.permissionMode }));
		const originalActive = deps.getActiveSession();
		let sender: ReturnType<typeof createMessageSender>;
		deps.getActiveSession = () => ({ ...originalActive, sendMessage: (...args) => sender.sendMessage(...args) });
		sender = createMessageSender(deps);
		expect(await sender.sendMessage('hello')).toBe(true);
		expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({
			type: 'query', overrides: expect.objectContaining({ permission_mode: 'bypassPermissions' }),
		}));
	});
});

describe('remembered worker on send', () => {
	it('sends to the remembered worker, consumes the active intent but keeps the default', async () => {
		const pick = { id: 'runner-1', name: 'Unraid' };
		setPersistedDeviceIntent(pick);
		const { deps, wsConn, getState } = makeDeps(baseState({ deviceIntent: getPersistedDeviceIntent() }));
		try {
			expect(await createMessageSender(deps).sendMessage('hello worker')).toBe(true);
			expect(wsConn.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'query', target_instance_id: pick.id }));
			expect(deps.sessionEphemeralCache.remove).toHaveBeenCalledWith('__new_chat__');
			expect(getState().deviceIntent).toBeNull();
			expect(getPersistedDeviceIntent()).toEqual(pick);
		} finally {
			setPersistedDeviceIntent(null);
		}
	});
});

describe('message-sender voice ownership', () => {
	it('awaits streaming admission and retains the draft on an explicit rejection', async () => {
		voice.isCallActiveFor.mockReturnValueOnce(true);
		voice.sendVoiceText.mockRejectedValueOnce(new Error('This voice engine does not support text or attachments.'));
		const { deps, wsConn, getState } = makeDeps(baseState({ sessionId: 'owner' }));
		expect(await createMessageSender(deps).sendMessage('Read this', { pendingUploadPaths: ['/workspace/uploads/file.txt'] })).toBe(false);
		expect(getState().error).toContain('does not support');
		expect(wsConn.send).not.toHaveBeenCalled();
	});
	it('preserves attachment paths in the owning voice conversation', async () => {
		voice.isCallActiveFor.mockReturnValueOnce(true);
		const { deps, wsConn } = makeDeps(baseState({ sessionId: 'owner' }));
		expect(await createMessageSender(deps).sendMessage('Read @report.pdf', {
			pendingUploadPaths: ['/workspace/uploads/report.pdf'],
		})).toBe(true);
		expect(voice.sendVoiceText).toHaveBeenLastCalledWith('owner', expect.stringContaining('/workspace/uploads/report.pdf'));
		expect(wsConn.send).not.toHaveBeenCalled();
	});
	it('keeps the draft when context persistence fails', async () => {
		voice.isCallActiveFor.mockReturnValueOnce(true);
		context.persistVoiceContext.mockRejectedValueOnce(new Error('offline'));
		const { deps, getState } = makeDeps(baseState({ sessionId: 'owner' }));
		const priorCalls = voice.sendVoiceText.mock.calls.length;
		expect(await createMessageSender(deps).sendMessage('Keep this')).toBe(false);
		expect(voice.sendVoiceText.mock.calls).toHaveLength(priorCalls);
		expect(getState().error).toContain('Could not save voice execution settings');
	});
	it('sends typed text to the call without a competing WS query', async () => {
		voice.isCallActiveFor.mockReturnValueOnce(true);
		const { deps, wsConn } = makeDeps(baseState({ sessionId: 'owner', mode: 'voice' }));
		expect(await createMessageSender(deps).sendMessage('Use this branch')).toBe(true);
		expect(voice.sendVoiceText).toHaveBeenCalledWith('owner', 'Use this branch');
		expect(wsConn.send).not.toHaveBeenCalled();
	});
	it('returns false while connecting so the composer restores its draft', async () => {
		voice.isCallActiveFor.mockReturnValueOnce(true);
		voice.sendVoiceText.mockReturnValueOnce(false);
		const { deps, wsConn, getState } = makeDeps(baseState({ sessionId: 'owner' }));
		expect(await createMessageSender(deps).sendMessage('Keep my draft')).toBe(false);
		expect(getState().error).toContain('Voice call is not ready');
		expect(wsConn.send).not.toHaveBeenCalled();
	});
});

describe('message-sender worktree send backstop', () => {
	it('suppresses the send to main when worktree creation is still in flight', async () => {
		// worktreeIntent worktree, no sessionId, but an optimistic message is already
		// present → autoCreateWorktree is mid-flight.
		const { deps, wsConn, getState } = makeDeps(baseState({
			worktreeIntent: 'worktree',
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
			worktreeIntent: 'worktree',
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

	// #1636: a refused first worktree send must hand the text back to the
	// composer (false), never report it as sent.
	it.each([
		['the existing-worktree send', { worktreeMode: 'existing' as const, worktreeId: 'wt-1' }],
		['the staged-branch create', { worktreeBranch: 'live/1528-explicit' }],
		['the auto-named create', { worktreeBranch: '' }],
	])('reports a refusal of %s as not sent', async (_label, selection) => {
		const { deps, autoCreateWorktree, createWorktree } = makeDeps(baseState({
			worktreeIntent: 'worktree', sessionId: null, messages: [], ...selection,
		}));
		autoCreateWorktree.mockResolvedValueOnce(false);
		createWorktree.mockResolvedValueOnce(null);
		deps.getActiveSession = () => ({ sendMessage: vi.fn(async () => false), createWorktree, autoCreateWorktree });
		expect(await createMessageSender(deps).sendMessage('keep me')).toBe(false);
	});

	it('reports an existing-worktree send without a profile as refused, with the reason in the composer banner', async () => {
		const { deps, getState } = makeDeps(baseState({
			worktreeIntent: 'worktree', sessionId: null, messages: [], profile: '',
			worktreeMode: 'existing', worktreeId: 'wt-1',
		}));
		expect(await createMessageSender(deps).sendMessage('keep me')).toBe(false);
		expect(getState().worktreeError).toBe('Please select a profile');
	});

	it('counts a staged-branch create as sent when the user navigated away during it', async () => {
		const { deps, internal, createWorktree } = makeDeps(baseState({
			worktreeIntent: 'worktree', sessionId: null, messages: [], worktreeBranch: 'live/1528-explicit',
		}));
		createWorktree.mockImplementationOnce(async () => { internal.worktreeCreationGeneration++; return null; });
		expect(await createMessageSender(deps).sendMessage('mine')).toBe(true);
	});

	it('still sends a normal local first message over WS', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
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

describe('message-sender session mode', () => {
	it('stamps the lane on the query that creates the session', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			sessionId: null,
			worktreeIntent: 'local',
			project: '',
			mode: 'chat',
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('what is the weather');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.mode).toBe('chat');
	});

	it('omits the lane once the session row exists — the row already owns it', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			sessionId: 'ses-1',
			worktreeIntent: 'local',
			mode: 'chat',
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('follow-up');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.mode).toBeUndefined();
	});
});

describe('message-sender workspace folders', () => {
	it.each([{ ids: ['docs', 'api'] }, { ids: [] }])('sends the current folder list, including removal of all folders ($ids)', async ({ ids }) => {
		const { deps, wsConn } = makeDeps(baseState({ worktreeIntent: 'local', workspaceProjectIds: ids }));
		await createMessageSender(deps).sendMessage('use these folders');
		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides.workspace_project_ids).toEqual(ids);
	});
});

describe('message-sender fast-mode override', () => {
	it('sends both fast-mode changes on successive turns of an existing Astra session', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			sessionId: 'astra-session',
			modelOverride: 'prov:codex:gpt-6-astra',
			fastMode: false,
		}));
		const sender = createMessageSender(deps);
		for (const fastMode of [false, true, false]) {
			deps.update(s => ({ ...s, isStreaming: false, fastMode }));
			expect(await sender.sendMessage(fastMode ? 'go fast' : 'normal speed')).toBe(true);
		}
		const queries = wsConn.send.mock.calls.map(([frame]) => frame).filter(frame => frame.type === 'query');
		expect(queries).toHaveLength(3);
		expect(queries.map(frame => frame.session_id)).toEqual(['astra-session', 'astra-session', 'astra-session']);
		expect(queries.map(frame => frame.overrides)).toEqual([false, true, false].map(fast =>
			expect.objectContaining({ model: 'prov:codex:gpt-6-astra', fast })
		));
	});

	it('sends fast: true when the session has fast mode on', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: 'opus',
			fastMode: true,
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('go fast');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides).toMatchObject({ model: 'opus', fast: true });
	});

	it('sends fast: false when the session has fast mode off', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: 'opus',
			fastMode: false,
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('normal speed');

		// An absent key would let the backend fall back to the session row,
		// which still holds the earlier `true` — so "off" must be explicit.
		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides).toMatchObject({ model: 'opus', fast: false });
	});
});

describe('message-sender effort in the assistant home', () => {
	afterEach(() => assistantState.set(null));

	async function sentOverrides(sessionId: string) {
		const { deps, wsConn } = makeDeps(baseState({ sessionId, modelOverride: 'opus', effortLevelOverride: 'low' }));
		await createMessageSender(deps).sendMessage('hello');
		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		return frame.overrides;
	}

	it('omits the effort a home send would otherwise carry', async () => {
		assistantState.set({ home_session_id: 'home' } as AssistantState);
		const overrides = await sentOverrides('home');
		expect(overrides).toMatchObject({ model: 'opus' });
		expect(overrides).not.toHaveProperty('effort');
	});

	it('keeps the picked effort in an ordinary chat', async () => {
		assistantState.set({ home_session_id: 'home' } as AssistantState);
		expect(await sentOverrides('ordinary')).toMatchObject({ model: 'opus', effort: 'low' });
	});
});

describe('message-sender context-window override', () => {
	it('sends a numeric context_window that can force a 1M Claude model to normal 200k', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: 'sonnet',
			contextWindowChoice: 'standard'
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('use the normal window');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides).toMatchObject({ model: 'sonnet', context_window: 200_000 });
	});

	it('uses the displayed registry default when context is selected before a model override', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: null,
			contextWindowChoice: 'standard'
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('use the default model at its normal window');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		// The backend still resolves its normal default model; context is explicitly
		// applied to that same frontend-visible registry default.
		expect(frame.overrides).toEqual({ context_window: 200_000, workspace_project_ids: [] });
	});

	it('omits context_window for a model without a 1M choice', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: 'claude-haiku-4-5',
			contextWindowChoice: '1m'
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('small and fast');

		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides.model).toBe('claude-haiku-4-5');
		expect(frame.overrides.context_window).toBeUndefined();
	});

	// #1895: the worker keeps a newer pin when this tab's view of it is outdated.
	it.each([
		{ label: 'the acknowledged pin', pin: 1_000_000, base: 1_000_000 },
		{ label: '0 when no pin is saved', pin: null, base: 0 },
		{ label: 'nothing while the pin is unknown', pin: undefined, base: undefined },
	])('sends $label as context_window_base on an existing chat', async ({ pin, base }) => {
		const { deps, wsConn } = makeDeps(baseState({
			sessionId: 'ses-1',
			worktreeIntent: 'local',
			modelOverride: 'sonnet',
			contextWindowChoice: 'standard',
			contextWindowPin: pin,
		}));
		await createMessageSender(deps).sendMessage('follow-up');
		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides.context_window).toBe(200_000);
		expect(frame.overrides.context_window_base).toBe(base);
	});

	it('sends no context_window_base when the send creates the chat', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			worktreeIntent: 'local',
			modelOverride: 'sonnet',
			contextWindowChoice: 'standard',
			contextWindowPin: 1_000_000,
		}));
		await createMessageSender(deps).sendMessage('first');
		const frame = wsConn.send.mock.calls.find(([message]) => (message as any).type === 'query')?.[0] as any;
		expect(frame.overrides.context_window).toBe(200_000);
		expect(frame.overrides).not.toHaveProperty('context_window_base');
	});
});

describe('message-sender new-session adoption token (hijack fix)', () => {
	function querySends(wsConn: { send: ReturnType<typeof vi.fn> }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
	}

	it('attaches a request_id to a NEW-session query and records it on internal', async () => {
		const { deps, wsConn, internal } = makeDeps(baseState({
			worktreeIntent: 'local',
			sessionId: null,
			messages: [],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('hello new');

		const sends = querySends(wsConn);
		expect(sends).toHaveLength(1);
		const frame = sends[0][0] as any;
		// The outgoing query carries the adoption token so the backend can echo it.
		expect(typeof frame.request_id).toBe('string');
		expect(frame.request_id).toBeTruthy();
		// The same token is recorded so the session_created/start handlers can match it.
		expect(internal.pendingRequestId).toBe(frame.request_id);
		expect(internal.awaitingNewSession).toBe(true);
	});

	it('does NOT attach a request_id to an EXISTING-session query', async () => {
		const { deps, wsConn, internal } = makeDeps(baseState({
			worktreeIntent: 'local',
			sessionId: 'existing-session',
			title: 'Existing',
			messages: [{ id: 'user-1', role: 'user', content: 'earlier' } as any],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('follow up');

		const sends = querySends(wsConn);
		expect(sends).toHaveLength(1);
		const frame = sends[0][0] as any;
		// An existing session routes its start by the known id — no token needed, and
		// awaitingNewSession must NOT be set (it's gated on the absence of a sessionId).
		expect(frame.request_id).toBeUndefined();
		expect(internal.pendingRequestId).toBeNull();
		expect(internal.awaitingNewSession).toBe(false);
	});
});

/**
 * Sending while a turn is live: there is no interrupt handshake any more. The
 * prompt goes out as a plain `query` (the backend decides whether it steers the
 * live turn or starts the next one), and the optimistic echo lands on
 * `queuedMessages` — the pending bubble below the transcript — instead of on the
 * transcript itself. A failed send rolls that echo back.
 */
describe('message-sender send during a live turn', () => {
	function streamingState() {
		return baseState({
			worktreeIntent: 'local',
			sessionId: 'ses-1',
			isStreaming: true,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		});
	}

	it('sends a plain query with no interrupt handshake or follow-up flag', async () => {
		const { deps, wsConn } = makeDeps(streamingState());
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('answer this too');

		expect(result).toBe(true);
		const interruptSends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'interrupt_for_query');
		expect(interruptSends).toHaveLength(0);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).interrupt_followup).toBeUndefined();
		expect((querySends[0][0] as any).prompt).toBe('answer this too');
	});

	it('echoes the prompt onto queuedMessages, not the transcript', async () => {
		const { deps, getState } = makeDeps(streamingState());
		const sender = createMessageSender(deps);

		await sender.sendMessage('answer this too');

		const queued = getState().queuedMessages;
		expect(queued).toHaveLength(1);
		expect(queued[0].content).toBe('answer this too');
		expect(queued[0].id).toMatch(/^queued-local-/);
		// The transcript is untouched — the model hasn't seen the prompt yet.
		expect(getState().messages).toHaveLength(1);
	});

	it('rolls the pending echo back when the query never goes out', async () => {
		const { deps, wsConn, getState } = makeDeps(streamingState());
		wsConn.send.mockImplementation((data: Record<string, unknown>) => data?.type !== 'query');
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('answer this too');

		expect(result).toBe(false);
		expect(getState().queuedMessages).toHaveLength(0);
		// The live turn is NOT ours to end — only the send failed.
		expect(getState().isStreaming).toBe(true);
		expect(getState().error).toMatch(/connection lost/i);
	});

	it('appends to the transcript when nothing is streaming', async () => {
		const { deps, wsConn, getState } = makeDeps(baseState({
			worktreeIntent: 'local',
			sessionId: 'ses-1',
			isStreaming: false,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		}));
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('no rush');

		expect(result).toBe(true);
		expect(getState().queuedMessages).toHaveLength(0);
		expect(getState().messages).toHaveLength(2);
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		// Nothing parked: the send opens its own turn.
		expect(getState().messages[1].turnId).toBeUndefined();
	});

	it('carries the parked turn id on a send while background work runs', async () => {
		const { deps, getState } = makeDeps(baseState({
			worktreeIntent: 'local',
			sessionId: 'ses-1',
			isStreaming: false,
			liveTurnId: null,
			turnParked: true,
			activeTasks: [{ id: 'task-1', status: 'running', toolUseId: 'monitor-1' }],
			title: 'Existing chat',
			messages: [
				{ id: 'u0', role: 'user', content: 'watch the deploy', turnId: 'turn-1' } as any,
				{ id: 'a0', role: 'assistant', type: 'tool_use', toolName: 'Monitor', toolId: 'monitor-1', content: '' } as any,
			],
		} as Partial<ActiveSessionState>));
		const sender = createMessageSender(deps);

		expect(await sender.sendMessage('ping me when done')).toBe(true);

		const appended = getState().messages.at(-1)!;
		expect(appended.content).toBe('ping me when done');
		expect(appended.turnId).toBe('turn-1');
	});
});

/**
 * Worktree sessions resume like local sessions: they only need their initial
 * working directory to still exist on disk (enforced backend-side on the query
 * path). The frontend must NOT try to reopen/recreate the worktree before
 * sending. `worktreeDeleted` is now a purely informational "dir missing on disk"
 * badge and no longer gates the send — the message goes out (carrying
 * worktree_id) untouched, and no reopen call is made.
 */
describe('message-sender worktreeDeleted no longer triggers a pre-send reopen', () => {
	beforeEach(() => {
		vi.mocked(reopenWorktree).mockReset();
		vi.mocked(reopenWorktree).mockResolvedValue(undefined as never);
	});

	function deletedWorktreeState() {
		return baseState({
			worktreeIntent: 'worktree',
			sessionId: 'ses-1', // existing session → reaches the WS query path
			worktreeId: 'wt-1',
			worktreeBranch: 'feat/login',
			worktreeDeleted: true,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		});
	}

	it('sends the query without any reopen/recreate call', async () => {
		const { deps, wsConn, getState } = makeDeps(deletedWorktreeState());
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('keep going');

		expect(result).toBe(true);
		// CRITICAL regression: the removed status-driven recreate machinery must
		// never fire on send.
		expect(reopenWorktree).not.toHaveBeenCalled();
		expect(getState().error).toBeNull();
		// The query went out carrying worktree_id; the backend resolves the cwd.
		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		expect((querySends[0][0] as any).worktree_id).toBe('wt-1');
	});
});

/**
 * stopGeneration: a Stop clicked on a brand-new session before the backend has
 * minted its id can't be addressed yet (no session_id). It must be DEFERRED —
 * recorded so the session_created/start handler can send it once the id arrives
 * — instead of being silently dropped (the original "message saved even though I
 * stopped before output" bug). Once a sessionId exists, the stop goes out
 * immediately.
 */
describe('message-sender pending-message Interrupt', () => {
	it.each([true, false])('preserves the queue when streaming=%s', (isStreaming) => {
		const queuedMessages = [{ id: 'turn-q', content: 'answer this', queuedAt: 1 }];
		const { deps, wsConn, getState } = makeDeps(baseState({
			sessionId: 'ses-1', isStreaming, liveTurnId: 'turn-parked', queuedMessages,
		}));
		createMessageSender(deps).interruptForQueuedMessages();
		expect(wsConn.send).toHaveBeenCalledExactlyOnceWith({
			type: 'interrupt_for_query', session_id: 'ses-1', turn_id: 'turn-parked',
		});
		expect(getState().queuedMessages).toEqual(queuedMessages);
		expect(getState().isStreaming).toBe(isStreaming);
	});

	it('does nothing when the pending message has already been delivered', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: 'ses-1' }));
		createMessageSender(deps).interruptForQueuedMessages();
		expect(wsConn.send).not.toHaveBeenCalled();
	});
});

describe('message-sender stopGeneration deferral', () => {
	function stopSends(wsConn: { send: { mock: { calls: unknown[][] } } }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'stop');
	}

	it('sends the stop immediately when a sessionId already exists', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: 'sess-1', isStreaming: true }));
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		const sends = stopSends(wsConn);
		expect(sends).toHaveLength(1);
		expect((sends[0][0] as any).session_id).toBe('sess-1');
		expect(deps.internal.stopRequestedBeforeSession).toBe(false);
	});

	it('carries the live turn id when one is known (#585)', () => {
		const { deps, wsConn } = makeDeps(
			baseState({ sessionId: 'sess-1', isStreaming: true, liveTurnId: 'turn-9' })
		);
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		const sends = stopSends(wsConn);
		expect(sends).toHaveLength(1);
		expect((sends[0][0] as any).turn_id).toBe('turn-9');
	});

	it('omits turn_id when no live turn id is known (legacy session-scoped, #585)', () => {
		const { deps, wsConn } = makeDeps(
			baseState({ sessionId: 'sess-1', isStreaming: true, liveTurnId: null })
		);
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		const sends = stopSends(wsConn);
		expect(sends).toHaveLength(1);
		expect('turn_id' in (sends[0][0] as any)).toBe(false);
	});

	it('defers the stop when no sessionId yet but a new session is awaited', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: null, isStreaming: true }));
		deps.internal.awaitingNewSession = true;
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		// Nothing addressable yet → must NOT send, but MUST record the intent.
		expect(stopSends(wsConn)).toHaveLength(0);
		expect(deps.internal.stopRequestedBeforeSession).toBe(true);
	});

	it('does nothing when there is no session and none is being created', () => {
		const { deps, wsConn } = makeDeps(baseState({ sessionId: null }));
		// awaitingNewSession stays false (no send in flight).
		const sender = createMessageSender(deps);

		sender.stopGeneration();

		expect(stopSends(wsConn)).toHaveLength(0);
		expect(deps.internal.stopRequestedBeforeSession).toBe(false);
	});
});

/**
 * Restored-session race guard (e2e Bug A).
 *
 * init() adopts a persisted sessionId BEFORE its history has rendered — the UI
 * shows the "New Chat" empty state while _state.sessionId silently points at an
 * old session. A send fired in that window continues the invisible old session.
 * sendMessage must wait for the restore's initial history load to settle
 * (isRestoringSession → false) before targeting the session, and refuse with a
 * clear error if it never settles.
 */
describe('message-sender restored-session race guard', () => {
	function querySends(wsConn: { send: ReturnType<typeof vi.fn> }) {
		return wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
	}

	it('waits for the restore to settle, then sends into the restored session', async () => {
		vi.useFakeTimers();
		try {
			const { deps, wsConn } = makeDeps(baseState({
				sessionId: 'restored-1',
				messages: [],
				isRestoringSession: true,
			} as any));
			const sender = createMessageSender(deps);

			const pending = sender.sendMessage('hello restored');

			// Still restoring → the query must NOT have gone out yet.
			await vi.advanceTimersByTimeAsync(300);
			expect(querySends(wsConn)).toHaveLength(0);

			// History lands (poll applied) and the restore settles.
			deps.update(s => ({
				...s,
				isRestoringSession: false,
				messages: [{ id: 'm1', role: 'user', content: 'earlier' } as any],
			}));
			await vi.advanceTimersByTimeAsync(200);

			const result = await pending;
			expect(result).toBe(true);
			const sends = querySends(wsConn);
			expect(sends).toHaveLength(1);
			expect((sends[0][0] as any).session_id).toBe('restored-1');
		} finally {
			vi.useRealTimers();
		}
	});

	it('refuses the send when the restore never settles', async () => {
		vi.useFakeTimers();
		try {
			const { deps, wsConn, getState } = makeDeps(baseState({
				sessionId: 'restored-1',
				messages: [],
				isRestoringSession: true,
			} as any));
			const sender = createMessageSender(deps);

			const pending = sender.sendMessage('hello stuck');
			await vi.advanceTimersByTimeAsync(6000);

			const result = await pending;
			expect(result).toBe(false);
			expect(querySends(wsConn)).toHaveLength(0);
			expect(getState().error).toMatch(/loading/i);
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('createWorktree remote-only compose', () => {
	beforeEach(() => {
		apiMocks.post.mockReset();
		apiMocks.post.mockResolvedValue({
			session_id: 'ses-wt',
			branch_name: 'feat/x',
			base_branch: 'main',
			worktree_path: '/remote/wt',
			status: 'ok',
		});
	});

	it('posts project_v2_id and omits pending_upload_paths', async () => {
		const { deps } = makeDeps(baseState({
			project: '',
			projectV2Id: 'pv2-remote',
			worktreeBranch: 'feat/x',
			worktreeIntent: 'worktree',
		}));
		const sender = createMessageSender(deps);

		await sender.createWorktree('hello');

		expect(api.post).toHaveBeenCalledTimes(1);
		const [path, body] = apiMocks.post.mock.calls[0];
		expect(path).toBe('/sessions/with-worktree');
		expect(body.project_v2_id).toBe('pv2-remote');
		expect(body.project_id).toBeUndefined();
		expect(body.pending_upload_paths).toBeUndefined();
	});

	it('refuses uploads before POSTing when there is no local project', async () => {
		const { deps, getState } = makeDeps(baseState({
			project: '',
			projectV2Id: 'pv2-remote',
			worktreeBranch: 'feat/x',
			worktreeIntent: 'worktree',
		}));
		const sender = createMessageSender(deps);

		await sender.createWorktree('hello', ['/tmp/upload.txt']);

		expect(apiMocks.post).not.toHaveBeenCalled();
		expect(getState().worktreeError).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
	});

	it('still posts project_id for a local project and may include uploads', async () => {
		const { deps } = makeDeps(baseState({
			project: 'proj-1',
			projectV2Id: 'pv2-local',
			worktreeBranch: 'feat/x',
			worktreeIntent: 'worktree',
		}));
		const sender = createMessageSender(deps);

		await sender.createWorktree('hello', ['/tmp/upload.txt']);

		const body = apiMocks.post.mock.calls[0][1];
		expect(body.project_id).toBe('proj-1');
		expect(body.project_v2_id).toBeUndefined();
		expect(body.pending_upload_paths).toEqual(['/tmp/upload.txt']);
	});

	// #1636: the created session's first message can still be refused (no
	// connection); the send reports that so the composer keeps the text.
	it('resolves null when the first message on the created session is refused', async () => {
		const { deps, getState } = makeDeps(baseState({
			project: 'proj-1', worktreeBranch: 'feat/x', worktreeIntent: 'worktree',
		}));
		const inner = deps.getActiveSession().sendMessage as ReturnType<typeof vi.fn>;
		inner.mockResolvedValueOnce(false);
		expect(await createMessageSender(deps).createWorktree('hello')).toBeNull();
		expect(inner).toHaveBeenCalledWith('hello', expect.objectContaining({ skipWorktreeSetup: true }));
		expect(getState().sessionId).toBe('ses-wt');
	});
});


/**
 * A first send from an unbound compose (no sessionId) must invalidate the
 * shared '__new_chat__' ephemeral entry — it holds the new-chat draft, and a
 * stale one made the NEXT New Chat restore the previous chat's prompt into the
 * composer. The invalidation has to fire on EVERY creation path, including the
 * worktree branches that return before the plain WS query path.
 */
describe('sendMessage clears the new-chat compose cache on first send', () => {
	const firstSendCases: Array<[string, Partial<ActiveSessionState>]> = [
		['plain local query', { worktreeIntent: 'local', sessionId: null, messages: [] }],
		['auto worktree creation', {
			worktreeIntent: 'worktree', sessionId: null, worktreeBranch: '', messages: [],
		}],
		['existing-worktree creation', {
			worktreeIntent: 'worktree', sessionId: null, messages: [],
			worktreeMode: 'existing', worktreeId: 'wt-1',
		}],
	];
	it.each(firstSendCases)('clears __new_chat__ for %s', async (_name, overrides) => {
		const { deps } = makeDeps(baseState({ ...overrides }));
		const sender = createMessageSender(deps);

		await sender.sendMessage('first message');

		expect(deps.sessionEphemeralCache.remove).toHaveBeenCalledWith('__new_chat__');
	});

	it('does NOT clear __new_chat__ on a follow-up send to an existing session', async () => {
		const { deps } = makeDeps(baseState({
			worktreeIntent: 'local',
			sessionId: 'existing-session',
			title: 'Existing',
			messages: [{ id: 'user-1', role: 'user', content: 'earlier' } as any],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('follow up');

		expect(deps.sessionEphemeralCache.remove).not.toHaveBeenCalled();
	});
});

describe('sendMessage remote-only compose', () => {
	it('sends project_v2_id and never upload_paths', async () => {
		const { deps, wsConn } = makeDeps(baseState({
			project: '',
			projectV2Id: 'pv2-remote',
			worktreeIntent: 'local',
			sessionId: null,
			messages: [],
		}));
		const sender = createMessageSender(deps);

		await sender.sendMessage('hello remote', { pendingUploadPaths: ['/tmp/a.txt'] });

		const querySends = wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query');
		expect(querySends).toHaveLength(1);
		const frame = querySends[0][0] as any;
		expect(frame.project_v2_id).toBe('pv2-remote');
		expect(frame.project).toBeUndefined();
		expect(frame.upload_paths).toBeUndefined();
	});
});


/**
 * The optimistic pending bubble and the × that unsends it. Both edges exist
 * because the local `queued-local-*` id is private to this tab: the server has
 * never heard of it, and its own replace-all `queue_updated` snapshots are the
 * only thing that can retire it.
 */
describe('message-sender pending-bubble edges', () => {
	function streamingState() {
		return baseState({
			worktreeIntent: 'local',
			sessionId: 'ses-1',
			isStreaming: true,
			title: 'Existing chat',
			messages: [{ id: 'u0', role: 'user', content: 'earlier' } as any],
		});
	}

	beforeEach(() => {
		pendingQueue.reset('ses-1');
	});

	it('skips the optimistic echo when the server already described the queue', async () => {
		// The authoritative snapshot for this send lands BEFORE the local append
		// gets to run — a local row added on top of it is a second copy that no
		// later frame is guaranteed to replace.
		const { deps, getState, wsConn } = makeDeps(streamingState());
		// Deliver after sendMessage records dispatchedAt, while it reads state.
		// Preloading before send only passed when both Date.now calls happened
		// in the same millisecond; an older snapshot must not suppress a new send.
		let delivered = false;
		deps.getState = () => {
			if (!delivered) {
				delivered = true;
				pendingQueue.noteAuthoritative('ses-1', [
					{ id: 'turn-q', content: 'answer this too', queuedAt: 1 },
				]);
			}
			return getState();
		};
		const sender = createMessageSender(deps);

		const result = await sender.sendMessage('answer this too');

		expect(result).toBe(true);
		expect(getState().queuedMessages).toHaveLength(0);
		// Nor on the transcript (#2016): the turn is live, so the steer's own
		// `message.user` is the only copy that may land there.
		expect(getState().messages.map((m) => m.id)).toEqual(['u0']);
		// The send itself still goes out.
		expect(wsConn.send.mock.calls.filter(([m]) => (m as any)?.type === 'query')).toHaveLength(1);
	});

	it('does not suppress a new send with an older authoritative snapshot', async () => {
		const clock = vi.spyOn(Date, 'now').mockReturnValue(1000);
		try {
			const { deps, getState } = makeDeps(streamingState());
			pendingQueue.noteAuthoritative('ses-1', [
				{ id: 'earlier-turn', content: 'older prompt', queuedAt: 1 },
			]);
			clock.mockReturnValue(1001);
			const sender = createMessageSender(deps);
			expect(await sender.sendMessage('answer this too')).toBe(true);
			expect(getState().queuedMessages).toHaveLength(1);
			expect(getState().queuedMessages[0].content).toBe('answer this too');
		} finally {
			clock.mockRestore();
		}
	});

	it('unsends a server row by naming it', async () => {
		const { deps, wsConn, getState } = makeDeps(streamingState());
		deps.update((s) => ({
			...s,
			queuedMessages: [{ id: 'turn-q', content: 'nope', queuedAt: 1 }],
		}));
		const sender = createMessageSender(deps);

		expect(sender.removeQueuedMessage('turn-q')).toBe('nope');

		expect(getState().queuedMessages).toHaveLength(0);
		expect(wsConn.send).toHaveBeenCalledWith({
			type: 'remove_queued_message',
			message_id: 'turn-q',
			session_id: 'ses-1',
		});
	});

	it('drops a local echo on × instead of naming an id the server cannot know', async () => {
		const { deps, wsConn, getState } = makeDeps(streamingState());
		const sender = createMessageSender(deps);
		await sender.sendMessage('take it back');
		const localId = getState().queuedMessages[0].id;
		wsConn.send.mockClear();

		sender.removeQueuedMessage(localId);

		// Gone from view, and no frame naming the local id (which would only make
		// the server rebroadcast the row straight back).
		expect(getState().queuedMessages).toHaveLength(0);
		expect(wsConn.send).not.toHaveBeenCalled();
		// The removal is re-aimed at the real row when the snapshot arrives.
		const rows = [{ id: 'turn-q', content: 'take it back', queuedAt: 2 }];
		pendingQueue.noteAuthoritative('ses-1', rows);
		expect(pendingQueue.reconcile('ses-1', rows)).toEqual([]);
		expect(pendingQueue.drainRemovalRequests()).toEqual(['turn-q']);
	});

	it('ignores an × for a message that is no longer pending', () => {
		const { deps, wsConn } = makeDeps(streamingState());
		const sender = createMessageSender(deps);

		expect(sender.removeQueuedMessage('turn-gone')).toBeNull();

		expect(wsConn.send).not.toHaveBeenCalled();
	});
});
