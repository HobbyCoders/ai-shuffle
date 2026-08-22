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
import { createBrowserStreamClient, type BrowserStreamClient } from './stream-client';

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
		vi.advanceTimersByTime(60_000);
		window.dispatchEvent(new Event('online'));

		const types = ws.sent.map((s) => JSON.parse(s).type);
		expect(types.every((t) => ['pong', 'input', 'navigate'].includes(t))).toBe(true);
	});

	it('probes a long-silent socket and closes it only if it stays silent', () => {
		client = newClient();
		client.connect();
		const ws = latest();
		ws.onopen?.();

		vi.advanceTimersByTime(60_000); // missed a 30 s server ping
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

		vi.advanceTimersByTime(60_000);
		document.dispatchEvent(new Event('visibilitychange'));
		ws.receive({ type: 'ping' }); // server is alive after all

		vi.advanceTimersByTime(10_000);
		expect(ws.closeCalls.filter((c) => c.code === 4000)).toHaveLength(0);
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
