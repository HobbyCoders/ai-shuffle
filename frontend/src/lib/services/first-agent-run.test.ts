import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: { get: mocks.get, post: mocks.post },
}));
vi.mock('$lib/stores/session/v2/sse-feed', () => ({ createSseFeed: vi.fn() }));
import {
	ABORT_GRACE_MS, ensureStarterAgent, FIRST_RUN_PROMPT, LOOP_GUARD_CODE, pickFirstRunModel, SETUP_REQUIRED_CODE,
	startFirstAgentRun, type FirstRunModelInput, type FirstRunState
} from './first-agent-run';
import type { ModelProvider } from '$lib/api/model-providers';
import type { ModelOption, ProviderEntry } from '$lib/constants/models.svelte';

class FakeSocket {
	sent: Record<string, unknown>[] = [];
	closed = false;
	onopen: (() => void) | null = null;
	onmessage: ((e: MessageEvent) => void) | null = null;
	onerror: (() => void) | null = null;
	onclose: ((e: CloseEvent) => void) | null = null;
	constructor(public url: string) {}
	send(data: string) { this.sent.push(JSON.parse(data)); }
	close() { this.closed = true; }
	receive(frame: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent); }
}

let socket: FakeSocket;
let feedEvents: ((event: Record<string, unknown>) => void) | null;
let feedStopped: boolean;

function start(timeoutMs?: number, model?: string | null) {
	const updates: FirstRunState[] = [];
	const handle = startFirstAgentRun({
		profileId: 'assistant', timeoutMs, model,
		onUpdate: (s) => updates.push(s),
		openSocket: (url) => { socket = new FakeSocket(url); return socket as unknown as WebSocket; },
		openFeed: (_id, onEvent) => { feedEvents = onEvent; return { stop: () => { feedStopped = true; } }; },
	});
	return { handle, updates };
}

beforeEach(() => { vi.resetAllMocks(); feedEvents = null; feedStopped = false; });
afterEach(() => vi.useRealTimers());

describe('first agent run', () => {
	it('starts a chat-mode query and streams the reply from the event feed', async () => {
		const { handle, updates } = start();
		expect(socket.url).toContain('/api/v1/ws/chat');
		socket.onopen?.();
		const query = socket.sent[0];
		expect(query).toMatchObject({ type: 'query', prompt: FIRST_RUN_PROMPT, profile: 'assistant', mode: 'chat' });
		socket.receive({ type: 'session_created', session_id: 'other', request_id: 'someone-else' });
		expect(feedEvents).toBeNull();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: query.request_id });
		feedEvents!({ type: 'message.user', content: FIRST_RUN_PROMPT });
		feedEvents!({ type: 'assistant.text_delta', text: 'Hello' });
		feedEvents!({ type: 'assistant.text_delta', text: ' there.' });
		expect(updates.at(-1)).toMatchObject({ phase: 'streaming', text: 'Hello there.', sessionId: 's1' });
		feedEvents!({ type: 'turn.completed' });
		expect(await handle.done).toEqual({ phase: 'done', text: 'Hello there.', sessionId: 's1', error: null, code: null });
		expect(socket.closed).toBe(true); expect(feedStopped).toBe(true);
	});

	it('reports an uncoded error frame with the worker message', async () => {
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'error', message: 'Empty prompt' });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: 'Empty prompt', code: null });
	});

	it('recognises a turn the credential gate refused', async () => {
		// The frame app/core/credential_gate.py refusal_event() yields, tagged
		// with the session by the runtime's broadcast.
		const reason = 'Agents cannot start: Claude Code is set to API key mode and no Anthropic API key is stored on this worker.';
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		socket.receive({ type: 'error', session_id: 's1', message: reason, code: SETUP_REQUIRED_CODE });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: reason, code: 'agent_setup_required' });
	});

	it('recognises a turn the loop guard held', async () => {
		const reason = 'Loop guard: this automation turn is waiting for your confirmation.';
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		socket.receive({ type: 'error', session_id: 's1', message: reason, code: LOOP_GUARD_CODE });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: reason, code: 'loop_guard_confirm' });
	});

	it('keeps the code when the feed reports the refusal first', async () => {
		vi.useFakeTimers();
		const reason = 'Agents cannot start: the Claude Code CLI is not installed on this worker.';
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		feedEvents!({ type: 'turn.aborted', reason: 'error', message: reason });
		vi.advanceTimersByTime(ABORT_GRACE_MS / 2);
		socket.receive({ type: 'error', session_id: 's1', message: reason, code: SETUP_REQUIRED_CODE });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: reason, code: 'agent_setup_required' });
	});

	it('reports an aborted turn from the feed', async () => {
		vi.useFakeTimers();
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		feedEvents!({ type: 'turn.aborted', message: 'New agent runs are paused until you add a license key.' });
		vi.advanceTimersByTime(ABORT_GRACE_MS);
		expect(await handle.done).toMatchObject({ phase: 'failed', error: 'New agent runs are paused until you add a license key.', code: null });
	});

	it('stops and says so when the agent asks for a decision', async () => {
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		socket.receive({ type: 'permission_request', session_id: 's1', tool_name: 'Bash' });
		expect(await handle.done).toMatchObject({ phase: 'needs-input' });
	});

	it('answers pings and gives up after the timeout', async () => {
		vi.useFakeTimers();
		const { handle } = start(1000);
		socket.onopen?.();
		socket.receive({ type: 'ping' });
		expect(socket.sent.at(-1)).toEqual({ type: 'pong' });
		vi.advanceTimersByTime(1001);
		expect(await handle.done).toMatchObject({ phase: 'failed', error: expect.stringContaining('No answer arrived in time') });
	});

	it('treats a completed turn without text as a failure', async () => {
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		feedEvents!({ type: 'turn.completed' });
		expect(await handle.done).toMatchObject({ phase: 'failed' });
	});
});

describe('starter agent', () => {
	it('uses an existing agent', async () => {
		mocks.get.mockResolvedValue([{ id: 'coder', name: 'Coder' }]);
		expect(await ensureStarterAgent()).toMatchObject({ id: 'coder' });
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('skips the built-in agents and uses one the owner made', async () => {
		mocks.get.mockResolvedValue([
			{ id: 'general-purpose', name: 'General Purpose', is_builtin: true },
			{ id: 'coder', name: 'Coder', is_builtin: false },
		]);
		expect(await ensureStarterAgent()).toMatchObject({ id: 'coder' });
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('creates an Assistant when only the built-in agents exist', async () => {
		mocks.get.mockResolvedValue(['general-purpose', 'frontend-designer']
			.map((id) => ({ id, name: id, is_builtin: true })));
		mocks.post.mockResolvedValue({ id: 'assistant', name: 'Assistant' });
		expect(await ensureStarterAgent()).toMatchObject({ id: 'assistant' });
		expect(mocks.post).toHaveBeenCalledWith('/agents', expect.objectContaining({ name: 'Assistant', config: {} }));
	});

	it('creates an Assistant on a fresh install', async () => {
		mocks.get.mockResolvedValue([]);
		mocks.post.mockResolvedValue({ id: 'assistant', name: 'Assistant' });
		expect(await ensureStarterAgent()).toMatchObject({ id: 'assistant' });
		expect(mocks.post).toHaveBeenCalledWith('/agents', expect.objectContaining({ name: 'Assistant', config: {} }));
	});
});

const row = (over: Partial<ModelProvider>): ModelProvider => ({
	id: 'r', name: 'Row', provider_type: 'openai', base_url: '', auth_token_set: true, auth_token_masked: '', models: [],
	default_model: null, small_fast_model: null, extra_env: {}, context_window: null, max_output_tokens: null, enabled: true,
	...over,
} as ModelProvider);
const entry = (over: Partial<ProviderEntry>): ProviderEntry => ({
	id: 'r', name: 'Row', provider_type: 'openai', models: [], default_model: null, auth_kind: 'api', plan_type: null, ...over,
});
const claudeModels = [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet' }, { value: 'opus', label: 'Opus', family: 'opus' }] as ModelOption[];
const ROWS = [
	row({ id: 'codex', name: 'ChatGPT', provider_type: 'custom', auth_mode: 'chatgpt' } as Partial<ModelProvider>),
	row({ id: 'xai', name: 'Grok', provider_type: 'xai' }),
	row({ id: 'gw1', name: 'Together', provider_type: 'custom' }),
];
const REGISTRY = [
	entry({ id: 'codex', name: 'ChatGPT', provider_type: 'custom', auth_kind: 'subscription', models: [{ id: 'gpt-5', label: 'GPT-5' }], default_model: 'gpt-5' }),
	entry({ id: 'xai', name: 'Grok', provider_type: 'xai', models: [{ id: 'grok-4', label: 'Grok 4' }] }),
	entry({ id: 'gw1', name: 'Together', provider_type: 'custom', models: [{ id: 'llama', label: 'Llama' }] }),
];
const input = (over: Partial<FirstRunModelInput>): FirstRunModelInput => ({
	ready: [], preferred: null, rows: ROWS, registry: REGISTRY, defaultModel: 'sonnet', claudeModels, ...over,
});

describe('first run model', () => {
	it('sends the chosen model as an override, and nothing extra without one', () => {
		start(undefined, 'prov:codex:gpt-5');
		socket.onopen?.();
		expect(socket.sent[0]).toMatchObject({ type: 'query', overrides: { model: 'prov:codex:gpt-5' } });
		start();
		socket.onopen?.();
		expect(socket.sent[0].overrides).toBeUndefined();
	});

	it('does not use a Claude default when Claude is not ready', () => {
		expect(pickFirstRunModel(input({ ready: ['openai'] }))).toEqual({
			providerId: 'openai', model: 'prov:codex:gpt-5', providerName: 'OpenAI', modelLabel: 'GPT-5',
		});
	});

	it('keeps the Claude default when Claude is ready', () => {
		expect(pickFirstRunModel(input({ ready: ['openai', 'claude'] }))).toMatchObject({ providerId: 'claude', model: 'sonnet', modelLabel: 'Sonnet' });
	});

	it('prefers the provider connected in the wizard', () => {
		expect(pickFirstRunModel(input({ ready: ['claude', 'grok'], preferred: 'grok' }))).toMatchObject({
			providerId: 'grok', model: 'prov:xai:grok-4', providerName: 'xAI Grok', modelLabel: 'Grok 4',
		});
	});

	it('names a gateway by its row and uses a ready default composite', () => {
		expect(pickFirstRunModel(input({ ready: ['gw:gw1'] }))).toMatchObject({ providerId: 'gw:gw1', model: 'prov:gw1:llama', providerName: 'Together' });
		expect(pickFirstRunModel(input({ ready: ['openai', 'grok'], defaultModel: 'prov:xai:grok-4' }))).toMatchObject({ providerId: 'grok' });
	});

	it('picks a Claude model when Claude is ready but the default is another provider', () => {
		expect(pickFirstRunModel(input({ ready: ['claude'], defaultModel: 'prov:xai:grok-4' }))).toMatchObject({ providerId: 'claude', model: 'sonnet' });
	});

	it('returns null when nothing ready has a model', () => {
		expect(pickFirstRunModel(input({ ready: [] }))).toBeNull();
		expect(pickFirstRunModel(input({ ready: ['openrouter'] }))).toBeNull();
	});
});
