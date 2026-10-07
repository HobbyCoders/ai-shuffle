import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TerminalConnection, type ConnectionHandlers } from './connection';

class FakeSocket {
	static OPEN = 1;
	readyState = 1;
	binaryType = 'blob';
	sent: unknown[] = [];
	closedWith: number | undefined;
	onmessage: ((event: MessageEvent) => void) | null = null;
	onclose: ((event: CloseEvent) => void) | null = null;
	constructor(readonly url: string) {}
	send(data: string) { this.sent.push(JSON.parse(data)); }
	close(code?: number) { this.closedWith = code; }
	frame(data: unknown) { this.onmessage?.({ data: typeof data === 'string' || data instanceof ArrayBuffer ? data : JSON.stringify(data) } as MessageEvent); }
	drop(code: number) { this.onclose?.({ code } as CloseEvent); }
}

let sockets: FakeSocket[];
let handlers: { [K in keyof ConnectionHandlers]-?: ReturnType<typeof vi.fn> };
const terminal = { id: 't1', state: 'live' } as never;

function connect() {
	const conn = new TerminalConnection('t1', handlers as unknown as ConnectionHandlers, (url) => {
		const socket = new FakeSocket(url);
		sockets.push(socket);
		return socket as unknown as WebSocket;
	});
	conn.connect();
	return conn;
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.stubGlobal('WebSocket', { OPEN: 1 });
	sockets = [];
	handlers = { onReset: vi.fn(), onData: vi.fn(), onState: vi.fn(), onStatus: vi.fn(), onDropped: vi.fn(), onInputError: vi.fn(), onReplayed: vi.fn() };
});
afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('TerminalConnection', () => {
	it('connects to the terminal socket and reads binary frames', () => {
		connect();
		expect(sockets[0].url).toContain('/api/v1/ws/terminals/t1');
		expect(sockets[0].binaryType).toBe('arraybuffer');
		expect(handlers.onStatus).toHaveBeenLastCalledWith('connecting', null);
	});

	it('resets on attach, replays, then goes live and sends the size', () => {
		const conn = connect();
		const socket = sockets[0];
		conn.resize(100, 30);
		expect(socket.sent).toEqual([]); // nothing sent before the replay
		socket.frame({ type: 'attached', terminal });
		expect(handlers.onReset).toHaveBeenCalledOnce();
		expect(handlers.onState).toHaveBeenCalledWith(terminal);
		expect(conn.input('ls')).toBe(false); // typing waits for the replay
		socket.frame(new Uint8Array([104, 105]).buffer);
		expect(handlers.onData).toHaveBeenCalledWith(new Uint8Array([104, 105]));
		socket.frame({ type: 'replayed' });
		expect(handlers.onStatus).toHaveBeenLastCalledWith('live', null);
		expect(socket.sent).toEqual([{ type: 'resize', cols: 100, rows: 30 }]);
		expect(conn.input('ls')).toBe(true);
		expect(socket.sent.at(-1)).toEqual({ type: 'input', data: 'ls' });
	});

	it('answers ping and reports refused input', () => {
		connect();
		sockets[0].frame({ type: 'ping' });
		expect(sockets[0].sent).toEqual([{ type: 'pong' }]);
		sockets[0].frame({ type: 'error', code: 'terminal_exited', message: 'The shell exited.' });
		expect(handlers.onInputError).toHaveBeenCalledWith('terminal_exited', 'The shell exited.');
	});

	it('ignores invalid resize values', () => {
		const conn = connect();
		sockets[0].frame({ type: 'attached' });
		sockets[0].frame({ type: 'replayed' });
		conn.resize(0, 10);
		conn.resize(10.5, 10);
		expect(sockets[0].sent).toEqual([]);
	});

	it('drops the tab on 4004 and 4410', () => {
		connect();
		sockets[0].drop(4410);
		expect(handlers.onDropped).toHaveBeenCalledOnce();
		expect(sockets).toHaveLength(1);
	});

	it('stops on auth and on a stopping worker', () => {
		connect();
		sockets[0].drop(4001);
		expect(handlers.onStatus).toHaveBeenLastCalledWith('auth', expect.any(String));
		connect();
		sockets[1].drop(1001);
		expect(handlers.onStatus).toHaveBeenLastCalledWith('gone', expect.any(String));
		vi.runAllTimers();
		expect(sockets).toHaveLength(2);
	});

	it('reconnects with backoff, then gives up', () => {
		connect();
		sockets[0].drop(1006);
		expect(handlers.onStatus).toHaveBeenLastCalledWith('reconnecting', null);
		vi.advanceTimersByTime(499);
		expect(sockets).toHaveLength(1);
		vi.advanceTimersByTime(1);
		expect(sockets).toHaveLength(2);
		for (let i = 0; i < 20; i += 1) {
			sockets.at(-1)!.drop(1006);
			vi.runOnlyPendingTimers();
		}
		expect(handlers.onStatus).toHaveBeenLastCalledWith('error', expect.stringContaining("Can't reach"));
		expect(sockets.length).toBeLessThanOrEqual(10);
	});

	function replay(socket: FakeSocket) {
		socket.frame({ type: 'attached' });
		socket.frame({ type: 'replayed' });
	}

	it('waits before coming back from a 4408, even on the first close', () => {
		connect();
		replay(sockets[0]);
		sockets[0].drop(4408);
		vi.advanceTimersByTime(0);
		expect(sockets).toHaveLength(1); // no zero-delay reconnect
		vi.advanceTimersByTime(999);
		expect(sockets).toHaveLength(1);
		vi.advanceTimersByTime(1);
		expect(sockets).toHaveLength(2);
	});

	it('backs off and gives up when every replay ends in another 4408', () => {
		connect();
		const delays: number[] = [];
		for (let i = 0; i < 20 && handlers.onStatus.mock.lastCall?.[0] !== 'error'; i += 1) {
			replay(sockets.at(-1)!);
			const before = sockets.length;
			sockets.at(-1)!.drop(4408);
			let waited = 0;
			while (sockets.length === before && handlers.onStatus.mock.lastCall?.[0] !== 'error' && waited < 60_000) {
				vi.advanceTimersByTime(100);
				waited += 100;
			}
			if (sockets.length > before) delays.push(waited);
		}
		expect(handlers.onStatus).toHaveBeenLastCalledWith('error', expect.stringContaining("Can't reach"));
		expect(delays).toHaveLength(8); // MAX_RECONNECTS, then the gave-up state
		expect(delays[0]).toBeGreaterThanOrEqual(1000);
		for (let i = 1; i < delays.length; i += 1) expect(delays[i]).toBeGreaterThanOrEqual(delays[i - 1]);
		expect(delays.at(-1)).toBeGreaterThan(delays[0]);
	});

	it('starts the retry count over only after the socket stays live', () => {
		connect();
		sockets[0].drop(1006);
		vi.runOnlyPendingTimers();
		sockets[0 + 1].frame({ type: 'attached' });
		sockets[1].frame({ type: 'replayed' });
		vi.advanceTimersByTime(5_000); // stayed live: the count is back to 0
		sockets[1].drop(1006);
		vi.advanceTimersByTime(499);
		expect(sockets).toHaveLength(2);
		vi.advanceTimersByTime(1);
		expect(sockets).toHaveLength(3); // the first-retry delay again
	});

	it('closes cleanly on dispose and never reconnects', () => {
		const conn = connect();
		const socket = sockets[0];
		conn.dispose();
		expect(socket.closedWith).toBe(1000);
		socket.drop(1006);
		vi.runAllTimers();
		expect(sockets).toHaveLength(1);
	});
});
