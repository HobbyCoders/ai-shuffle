import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: { get: mocks.get, post: mocks.post },
}));
vi.mock('$lib/stores/session/v2/sse-feed', () => ({ createSseFeed: vi.fn() }));
import { ensureStarterAgent, FIRST_RUN_PROMPT, startFirstAgentRun, type FirstRunState } from './first-agent-run';

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

function start(timeoutMs?: number) {
	const updates: FirstRunState[] = [];
	const handle = startFirstAgentRun({
		profileId: 'assistant', timeoutMs,
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
		expect(await handle.done).toEqual({ phase: 'done', text: 'Hello there.', sessionId: 's1', error: null });
		expect(socket.closed).toBe(true); expect(feedStopped).toBe(true);
	});

	it('reports a refused turn with the worker message', async () => {
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'error', message: 'No credential is set up. Sign in to the Claude Code CLI or add an API key.' });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: 'No credential is set up. Sign in to the Claude Code CLI or add an API key.' });
	});

	it('reports an aborted turn from the feed', async () => {
		const { handle } = start();
		socket.onopen?.();
		socket.receive({ type: 'session_created', session_id: 's1', request_id: socket.sent[0].request_id });
		feedEvents!({ type: 'turn.aborted', message: 'New agent runs are paused until you add a license key.' });
		expect(await handle.done).toMatchObject({ phase: 'failed', error: 'New agent runs are paused until you add a license key.' });
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

	it('creates an Assistant on a fresh install', async () => {
		mocks.get.mockResolvedValue([]);
		mocks.post.mockResolvedValue({ id: 'assistant', name: 'Assistant' });
		expect(await ensureStarterAgent()).toMatchObject({ id: 'assistant' });
		expect(mocks.post).toHaveBeenCalledWith('/agents', expect.objectContaining({ name: 'Assistant', config: {} }));
	});
});
