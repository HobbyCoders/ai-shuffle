import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createEmulatorStream, streamPath } from './stream-client';

class FakeSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSING = 2;
	static CLOSED = 3;
	static all: FakeSocket[] = [];
	readyState = FakeSocket.CONNECTING;
	sent: string[] = [];
	onopen: (() => void) | null = null;
	onclose: ((e: { code: number }) => void) | null = null;
	onerror: (() => void) | null = null;
	onmessage: ((e: { data: string }) => void) | null = null;
	constructor(public url: string) { FakeSocket.all.push(this); }
	send(d: string) { this.sent.push(d); }
	close() { this.readyState = FakeSocket.CLOSED; }
	open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
	drop(code = 1006) { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code }); }
	emit(msg: unknown) { this.onmessage?.({ data: typeof msg === 'string' ? msg : JSON.stringify(msg) }); }
}

function make() {
	const calls = { frames: [] as unknown[], states: [] as string[], links: [] as string[], errors: [] as unknown[], failures: [] as (string | undefined)[] };
	const client = createEmulatorStream({
		deviceId: 'pixel',
		getAuthToken: () => 'tok',
		WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
		random: () => 0.5,
		onFrame: (f) => calls.frames.push(f),
		onState: (s) => calls.states.push(s.state),
		onError: (e) => calls.errors.push(e),
		onLink: (s, d) => { calls.links.push(s); if (s === 'failed') calls.failures.push(d?.failure); },
	});
	return { client, calls };
}

beforeEach(() => { FakeSocket.all = []; vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('emulator stream client', () => {
	it('connects to the device stream with the auth token', () => {
		const { client } = make();
		client.connect();
		expect(FakeSocket.all).toHaveLength(1);
		expect(FakeSocket.all[0].url).toContain(streamPath('pixel'));
		expect(FakeSocket.all[0].url).toContain('token=tok');
		client.destroy();
	});

	it('delivers frames and states, answers pings, and sends encoded input', () => {
		const { client, calls } = make();
		client.connect();
		const ws = FakeSocket.all[0];
		expect(client.send({ type: 'rotate' })).toBe(false); // not open yet
		ws.open();
		ws.emit({ type: 'frame', format: 'jpeg', data: 'AAA', width: 10, height: 20, seq: 1 });
		ws.emit({ type: 'state', state: 'running' });
		ws.emit({ type: 'ping' });
		ws.emit('garbage');
		expect(calls.frames).toHaveLength(1);
		expect(calls.states).toEqual(['running']);
		expect(client.send({ type: 'tap', x: 1, y: 2 })).toBe(true);
		expect(ws.sent).toEqual(['{"type":"pong"}', '{"type":"tap","x":1,"y":2}']);
		expect(calls.links).toEqual(['connecting', 'live']);
		client.destroy();
	});

	it('reconnects with backoff after a drop, and resets after success', () => {
		const { client, calls } = make();
		client.connect();
		FakeSocket.all[0].open();
		FakeSocket.all[0].drop();
		expect(calls.links.at(-1)).toBe('reconnecting');
		vi.advanceTimersByTime(499);
		expect(FakeSocket.all).toHaveLength(1);
		vi.advanceTimersByTime(1);
		expect(FakeSocket.all).toHaveLength(2);
		FakeSocket.all[1].drop();
		vi.advanceTimersByTime(999);
		expect(FakeSocket.all).toHaveLength(2);
		vi.advanceTimersByTime(1);
		expect(FakeSocket.all).toHaveLength(3);
		FakeSocket.all[2].open();
		expect(calls.links.at(-1)).toBe('live');
		FakeSocket.all[2].drop();
		vi.advanceTimersByTime(500);
		expect(FakeSocket.all).toHaveLength(4);
		client.destroy();
	});

	it('passes input errors on without dropping the socket', () => {
		const { client, calls } = make();
		client.connect();
		const ws = FakeSocket.all[0];
		ws.open();
		ws.emit({ type: 'error', message: 'Point is off the screen', code: 'bad_input' });
		expect(calls.errors).toEqual([{ type: 'error', message: 'Point is off the screen', code: 'bad_input' }]);
		expect(client.isOpen()).toBe(true);
		client.destroy();
	});

	it.each([4004, 4400])('treats close %i as terminal until retry()', (code) => {
		const { client, calls } = make();
		client.connect();
		FakeSocket.all[0].open();
		FakeSocket.all[0].drop(code);
		expect(calls.links.at(-1)).toBe('failed');
		expect(calls.failures[0]).toBeTruthy();
		vi.advanceTimersByTime(60_000);
		window.dispatchEvent(new Event('online'));
		client.connect();
		expect(FakeSocket.all).toHaveLength(1);
		client.retry();
		expect(FakeSocket.all).toHaveLength(2);
		FakeSocket.all[1].open();
		expect(calls.links.at(-1)).toBe('live');
		client.destroy();
	});

	it('keeps reconnecting after a server error close (1011)', () => {
		const { client, calls } = make();
		client.connect();
		FakeSocket.all[0].drop(1011);
		expect(calls.links.at(-1)).toBe('reconnecting');
		vi.advanceTimersByTime(500);
		expect(FakeSocket.all).toHaveLength(2);
		client.destroy();
	});

	it('stops for good on an auth close and after destroy', () => {
		const { client, calls } = make();
		client.connect();
		FakeSocket.all[0].drop(4403);
		expect(calls.links.at(-1)).toBe('failed');
		vi.advanceTimersByTime(60_000);
		expect(FakeSocket.all).toHaveLength(1);

		const second = make();
		second.client.connect();
		FakeSocket.all[1].open();
		second.client.destroy();
		FakeSocket.all[1].drop();
		vi.advanceTimersByTime(60_000);
		expect(FakeSocket.all).toHaveLength(2);
	});
});
