/**
 * Revive-path behaviour of the browser screencast client.
 *
 * The panel used to blink every time the tab regained focus: the revive handler
 * closed the OPEN socket unconditionally, and each reconnect replayed the
 * server's connecting → live handshake, which unmounted the canvas. These tests
 * pin the current contract — a healthy socket survives focus changes, and only a
 * socket that has already gone silent past a server ping interval is torn down.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
	createBrowserStreamClient,
	type BrowserStreamClient,
} from './stream-client';

class FakeWebSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSING = 2;
	static CLOSED = 3;
	static instances: FakeWebSocket[] = [];

	readyState = FakeWebSocket.OPEN;
	sent: string[] = [];
	closeCalls: Array<{ code?: number; reason?: string }> = [];

	onopen: (() => void) | null = null;
	onclose: ((e: { code: number; target: unknown }) => void) | null = null;
	onerror: ((e: unknown) => void) | null = null;
	onmessage: ((e: { data: string }) => void) | null = null;

	constructor(public url: string) {
		FakeWebSocket.instances.push(this);
	}

	send(data: string) {
		this.sent.push(data);
	}

	close(code?: number, reason?: string) {
		this.closeCalls.push({ code, reason });
		this.readyState = FakeWebSocket.CLOSED;
	}

	/** Test helper: deliver a server frame. */
	receive(msg: unknown) {
		this.onmessage?.({ data: JSON.stringify(msg) });
	}
}

const originalWebSocket = globalThis.WebSocket;

function newClient(): BrowserStreamClient {
	return createBrowserStreamClient({
		sessionId: 's1',
		getAuthToken: () => 'tok',
		onStatus: () => {},
		onFrame: () => {},
	});
}

function latest(): FakeWebSocket {
	return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

describe('browser stream client revive path', () => {
	let client: BrowserStreamClient | null = null;
	beforeEach(() => {
		vi.useFakeTimers();
		FakeWebSocket.instances = [];
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		globalThis.WebSocket = FakeWebSocket as any;
	});

	afterEach(() => {
		client?.destroy();
		client = null;
		vi.useRealTimers();
		globalThis.WebSocket = originalWebSocket;
	});

	it('uses an encoded provider endpoint without requiring a chat session', () => {
		client = createBrowserStreamClient({ providerId: 'provider/one', getAuthToken: () => 'tok', onStatus: () => {}, onFrame: () => {} });
		client.connect();
		expect(latest().url).toContain('/api/v1/model-providers/provider%2Fone/openai-billing/browser?token=tok');
	});

	it('keeps the existing chat endpoint unchanged', () => {
		client = newClient(); client.connect();
		expect(latest().url).toContain('/api/v1/ws/browser/s1?token=tok');
	});

	it('leaves a healthy OPEN socket alone when the tab becomes visible', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();

		document.dispatchEvent(new Event('visibilitychange'));
		vi.advanceTimersByTime(10_000);

		expect(ws.closeCalls).toHaveLength(0);
		expect(FakeWebSocket.instances).toHaveLength(1);
	});

	it('never sends a frame type the server rejects as a protocol violation', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();

		// Go silent past the stale threshold, then trigger the revive path.
		vi.advanceTimersByTime(45_000);
		window.dispatchEvent(new Event('online'));

		const types = ws.sent.map((s) => JSON.parse(s).type);
		expect(types.every((t) => ['pong', 'input', 'navigate'].includes(t))).toBe(
			true,
		);
	});

	it('probes a long-silent socket and closes it only if it stays silent', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();

		vi.advanceTimersByTime(45_000); // missed a 30 s server ping
		document.dispatchEvent(new Event('visibilitychange'));
		expect(ws.closeCalls).toHaveLength(0); // probe armed, nothing closed yet

		vi.advanceTimersByTime(3_500);
		expect(ws.closeCalls).toHaveLength(1);
		expect(ws.closeCalls[0].code).toBe(4000);
	});

	it('spares a silent socket that answers the probe', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();

		vi.advanceTimersByTime(45_000);
		document.dispatchEvent(new Event('visibilitychange'));
		ws.receive({ type: 'ping' }); // server is alive after all

		vi.advanceTimersByTime(10_000);
		expect(ws.closeCalls.filter((c) => c.code === 4000)).toHaveLength(0);
	});

	it('does not revive an intentional disconnect on its health timer', () => {
		client = newClient();
		client.connect();
		client.disconnect();
		vi.advanceTimersByTime(120_000);
		window.dispatchEvent(new Event('online'));
		expect(FakeWebSocket.instances).toHaveLength(1);
	});
	it('keeps a static page healthy while server heartbeats arrive', () => {
		client = newClient();
		client.connect();
		latest().onopen?.();
		for (let i = 0; i < 6; i++) {
			vi.advanceTimersByTime(20_000);
			latest().receive({ type: 'ping' });
		}
		expect(latest().closeCalls).toHaveLength(0);
	});
	it('reconnects when the socket is already CLOSED', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();
		ws.readyState = FakeWebSocket.CLOSED;

		document.dispatchEvent(new Event('visibilitychange'));
		expect(FakeWebSocket.instances).toHaveLength(2);
	});
});

describe('page frames (toolbar url + loading)', () => {
	beforeEach(() => {
		FakeWebSocket.instances = [];
		globalThis.WebSocket = FakeWebSocket as any;
	});

	afterEach(() => {
		globalThis.WebSocket = originalWebSocket;
	});

	it('routes a page frame to onPage with url + loading', () => {
		const seen: Array<{ url: string | null; loading: boolean }> = [];
		const client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => 'tok',
			onStatus: () => {},
			onFrame: () => {},
			onPage: (url, loading) => seen.push({ url, loading }),
		});
		try {
			client.connect();
			latest().receive({ type: 'page', url: 'https://ex/path', loading: true });
			expect(seen).toEqual([{ url: 'https://ex/path', loading: true }]);
		} finally {
			client.destroy();
		}
	});

	it('routes inspect-hit frames to onInspectHit (element passed through raw)', () => {
		const seen: Array<{ mode: string; element: unknown }> = [];
		const client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => 'tok',
			onStatus: () => {},
			onFrame: () => {},
			onInspectHit: (mode, element) => seen.push({ mode, element }),
		});
		try {
			client.connect();
			latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
			const el = { selector: '#cta', tag: 'button' };
			latest().receive({ type: 'inspect-hit', mode: 'select', element: el });
			latest().receive({ type: 'inspect-hit', mode: 'hover', element: null });
			// Unknown modes are dropped, not forwarded.
			latest().receive({ type: 'inspect-hit', mode: 'weird', element: el });
			expect(seen).toEqual([
				{ mode: 'select', element: el },
				{ mode: 'hover', element: null },
			]);
		} finally {
			client.destroy();
		}
	});

	it('coerces missing/invalid page fields to (null, false)', () => {
		const seen: Array<{ url: string | null; loading: boolean }> = [];
		const client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => 'tok',
			onStatus: () => {},
			onFrame: () => {},
			onPage: (url, loading) => seen.push({ url, loading }),
		});
		try {
			client.connect();
			latest().receive({ type: 'page' });
			expect(seen).toEqual([{ url: null, loading: false }]);
		} finally {
			client.destroy();
		}
	});
});
describe('workspace protocol', () => {
	it.each(['input', 'inspect'])('requires protocol 2 workspace identity for agent %s', type => {
		client = newClient(); client.connect();
		latest().receive({ type: 'workspace', state });
		const payload = { type, id: 'c1', runtime_id: 'r1', tab_id: 'tab' };
		expect(client.send(payload)).toBe(false);
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		expect(client.send(payload)).toBe(false);
		latest().receive({ type: 'workspace', state });
		expect(client.send({ type })).toBe(false);
		expect(client.send({ ...payload, tab_id: 'other' })).toBe(false);
		expect(client.send(payload)).toBe(true);
		latest().receive({ type: 'hello', protocol: 1 });
		expect(client.send({ ...payload, id: 'c2' })).toBe(false);
	});
	it.each(['input', 'command', 'viewport'])('allows provider protocol 1 %s without workspace identity', type => {
		client = createBrowserStreamClient({ providerId: 'p1', getAuthToken: () => null, onStatus: () => {}, onFrame: () => {} });
		client.connect();
		expect(client.send({ type })).toBe(false);
		latest().receive({ type: 'hello', protocol: 1 });
		expect(client.send({ type })).toBe(true);
		expect(latest().sent.map(value => JSON.parse(value))).toEqual([{ type }]);
	});
	it('never grants provider workspace access or exposes workspace URLs', () => {
		const onWorkspace = vi.fn(), onHello = vi.fn(), onInspectHit = vi.fn(), onStatus = vi.fn(), onPage = vi.fn(), onFrame = vi.fn();
		client = createBrowserStreamClient({ providerId: 'p1', getAuthToken: () => null, onStatus, onPage, onFrame, onWorkspace, onHello, onInspectHit });
		client.connect();
		latest().receive({ type: 'hello', protocol: 1 });
		latest().receive({ type: 'workspace', state });
		latest().receive({ type: 'inspect-hit', mode: 'select', element: {} });
		latest().receive({ type: 'status', state: 'live', url: 'https://private.test' });
		latest().receive({ type: 'page', url: 'https://private.test' });
		for (const type of ['navigate', 'inspect', 'workspace-command', 'unknown']) {
			expect(client.send({ type, id: 'c1', runtime_id: 'r1', tab_id: 'tab', op: 'tabs', args: {} })).toBe(false);
		}
		expect(onWorkspace).not.toHaveBeenCalled();
		expect(onHello).not.toHaveBeenCalled();
		expect(onInspectHit).not.toHaveBeenCalled();
		expect(onStatus).toHaveBeenCalledWith('live', null);
		expect(onPage).toHaveBeenCalledWith(null, false);
		latest().receive({ type: 'frame', data: 'jpeg', metadata: { deviceWidth: 800, deviceHeight: 600 } });
		latest().receive({ type: 'frame', data: 'workspace', metadata: { runtime_id: 'r1', tab_id: 'tab' } });
		expect(onFrame).toHaveBeenCalledExactlyOnceWith('jpeg', { deviceWidth: 800, deviceHeight: 600 });
		expect(latest().sent).toEqual([]);
	});
	it.each([null, [], {}, { protocol: '1' }, { protocol: 2, viewer_id: 'viewer' }].map(hello => ({ hello })))('rejects malformed or workspace hellos on provider streams: $hello', ({ hello }) => {
		client = createBrowserStreamClient({ providerId: 'p1', getAuthToken: () => null, onStatus: () => {}, onFrame: () => {} });
		client.connect();
		latest().receive(hello && !Array.isArray(hello) ? { type: 'hello', ...hello } : hello);
		latest().receive({ type: 'workspace', state });
		expect(client.send({ type: 'input' })).toBe(false);
		expect(client.send({ type: 'workspace-command', id: 'c1', runtime_id: 'r1', tab_id: 'tab' })).toBe(false);
	});
	it.each([{ protocol: 1 }, { protocol: 2 }, { protocol: 2, viewer_id: '' }, { protocol: 2, viewer_id: 42 }, { protocol: '2', viewer_id: 'viewer' }])('never downgrades normal agent streams: %j', hello => {
		client = newClient(); client.connect();
		latest().receive({ type: 'hello', ...hello });
		latest().receive({ type: 'workspace', state });
		for (const type of ['input', 'command', 'viewport', 'workspace-command']) {
			expect(client.send({ type, id: 'c1', runtime_id: 'r1', tab_id: 'tab' })).toBe(false);
		}
	});
	it('revokes provider authority after a wrong hello or reconnect', () => {
		client = createBrowserStreamClient({ providerId: 'p1', getAuthToken: () => null, onStatus: () => {}, onFrame: () => {} });
		client.connect();
		latest().receive({ type: 'hello', protocol: 1 });
		expect(client.send({ type: 'input' })).toBe(true);
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		expect(client.send({ type: 'input' })).toBe(false);
		latest().receive({ type: 'hello', protocol: 1 });
		client.disconnect(); client.connect();
		expect(client.send({ type: 'viewport', width: 800, height: 600 })).toBe(false);
	});
	it('rejects an ambiguous session and provider configuration', () => {
		expect(() => createBrowserStreamClient({ sessionId: 's1', providerId: 'p1', getAuthToken: () => null, onStatus: () => {}, onFrame: () => {} })).toThrow('not both');
	});

	it('rejects malformed state without replacing the last valid workspace', () => {
		const onWorkspace = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame: () => {},
			onWorkspace,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		latest().receive({ type: 'workspace', state });
		for (const patch of [
			{ control: null },
			{ viewport: {} },
			{ revision: null },
			{ tabs: [{ id: 'tab' }] },
			{ available: 'yes' },
		]) {
			expect(() =>
				latest().receive({ type: 'workspace', state: { ...state, ...patch } }),
			).not.toThrow();
		}
		expect(onWorkspace).toHaveBeenCalledTimes(1);
	});
	it('rejects duplicate pending IDs without overwriting the original timer', () => {
		const result = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame: () => {},
			onCommandResult: result,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		latest().receive({ type: 'workspace', state });
		const command = {
			type: 'workspace-command',
			id: 'duplicate',
			runtime_id: 'r1',
			tab_id: 'tab',
			op: 'takeover',
			args: {},
		};
		expect(client.send(command)).toBe(true);
		vi.advanceTimersByTime(10000);
		expect(client.send(command)).toBe(false);
		expect(latest().sent).toHaveLength(1);
		vi.advanceTimersByTime(20000);
		expect(result).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(10000);
		expect(result).toHaveBeenCalledTimes(1);
	});
	it('clears timers after failed sends and permits retrying that ID', () => {
		const result = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame: () => {},
			onCommandResult: result,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		latest().receive({ type: 'workspace', state });
		const command = {
			type: 'workspace-command',
			id: 'retry',
			runtime_id: 'r1',
			tab_id: 'tab',
			op: 'takeover',
			args: {},
		};
		const timerCount = vi.getTimerCount();
		const send = vi.spyOn(latest(), 'send').mockImplementationOnce(() => {
			throw new Error('closed');
		});
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		expect(client.send(command)).toBe(false);
		expect(vi.getTimerCount()).toBe(timerCount);
		expect(client.send(command)).toBe(true);
		latest().receive({ type: 'command-result', id: 'retry', ok: true });
		vi.advanceTimersByTime(30000);
		expect(result).toHaveBeenCalledExactlyOnceWith('retry', true, undefined);
		send.mockRestore();
		warn.mockRestore();
	});

	let client: BrowserStreamClient;
	beforeEach(() => {
		vi.useFakeTimers();
		FakeWebSocket.instances = [];
		globalThis.WebSocket = FakeWebSocket as any;
	});
	afterEach(() => {
		client?.destroy();
		vi.useRealTimers();
		globalThis.WebSocket = originalWebSocket;
	});
	const state = {
		runtime_id: 'r1',
		revision: 1,
		available: true,
		target_id: 'target',
		active_tab_id: 'tab',
		tabs: [{ id: 'tab', url: 'about:blank', title: 'Tab', current: true }],
		viewport: { mode: 'fixed', width: 1280, height: 800 },
		control: { owner: 'agent', viewer_id: null, pending_viewer_id: null },
		busy: false,
	};
	it('learns viewer identity and rejects old revisions and mismatched frames', () => {
		const onHello = vi.fn(),
			onWorkspace = vi.fn(),
			onFrame = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame,
			onHello,
			onWorkspace,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		latest().receive({ type: 'workspace', state });
		latest().receive({ type: 'workspace', state: { ...state, revision: 0 } });
		latest().receive({
			type: 'frame',
			data: 'old',
			metadata: { runtime_id: 'r1', tab_id: 'other' },
		});
		latest().receive({
			type: 'frame',
			data: 'new',
			metadata: { runtime_id: 'r1', tab_id: 'tab' },
		});
		expect(onHello).toHaveBeenCalledWith('viewer');
		expect(onWorkspace).toHaveBeenCalledTimes(1);
		expect(onFrame).toHaveBeenCalledTimes(1);
	});
	it('does not send without authoritative identity and never replays interrupted commands', () => {
		const result = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame: () => {},
			onCommandResult: result,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		const command = {
			type: 'workspace-command',
			id: 'c1',
			runtime_id: 'r1',
			tab_id: 'tab',
			op: 'takeover',
			args: {},
		};
		expect(client.send(command)).toBe(false);
		latest().receive({ type: 'workspace', state });
		expect(client.send(command)).toBe(true);
		expect(client.send({ type: 'navigate', url: 'https://example.test' })).toBe(
			false,
		);
		expect(client.send({ ...command, id: 'wrong', tab_id: 'other' })).toBe(
			false,
		);
		const old = latest();
		old.readyState = FakeWebSocket.CLOSED;
		old.onclose?.({ code: 1006, target: old });
		expect(result).toHaveBeenCalledWith(
			'c1',
			false,
			expect.stringContaining('not replayed'),
		);
		vi.advanceTimersByTime(3000);
		expect(latest().sent).toEqual([]);
		expect(client.send({ ...command, id: 'c2' })).toBe(false);
	});
	it('surfaces timeout and denial and accepts creating an empty first tab', () => {
		const result = vi.fn();
		client = createBrowserStreamClient({
			sessionId: 's1',
			getAuthToken: () => null,
			onStatus: () => {},
			onFrame: () => {},
			onCommandResult: result,
		});
		client.connect();
		latest().receive({ type: 'hello', protocol: 2, viewer_id: 'viewer' });
		latest().receive({
			type: 'workspace',
			state: { ...state, active_tab_id: null, target_id: null, tabs: [] },
		});
		expect(
			client.send({
				type: 'workspace-command',
				id: 'create',
				runtime_id: 'r1',
				tab_id: null,
				op: 'tabs',
				args: { action: 'create' },
			}),
		).toBe(true);
		latest().receive({
			type: 'command-result',
			id: 'create',
			ok: false,
			error: 'Take control first',
		});
		expect(result).toHaveBeenCalledWith('create', false, 'Take control first');
		client.send({
			type: 'workspace-command',
			id: 'slow',
			runtime_id: 'r1',
			tab_id: null,
			op: 'takeover',
			args: {},
		});
		vi.advanceTimersByTime(30000);
		expect(result).toHaveBeenCalledWith(
			'slow',
			false,
			expect.stringContaining('timed out'),
		);
	});
});
