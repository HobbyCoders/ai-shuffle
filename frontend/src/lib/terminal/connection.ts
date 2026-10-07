/**
 * One terminal's socket (`/api/v1/ws/terminals/{id}`, contract in
 * docs/internal/terminals.md). Owns the handshake, the replay and the
 * reconnects; the emulator only sees reset, bytes and state.
 *
 * Bytes are handed straight to the emulator and never kept or logged.
 */
import { wsUrl } from '$lib/api/runtime';
import type { TerminalInfo } from '$lib/api/terminals';
import { closeAction, MAX_RECONNECTS, STABLE_AFTER } from './socket-policy';

export type ConnectionStatus = 'connecting' | 'live' | 'reconnecting' | 'gone' | 'auth' | 'error';

export interface ConnectionHandlers {
	/** Start of a replay: clear the screen before the scrollback arrives. */
	onReset(): void;
	onData(bytes: Uint8Array): void;
	onState(terminal: TerminalInfo): void;
	onStatus(status: ConnectionStatus, message: string | null): void;
	/** The terminal no longer exists (closed or unknown). */
	onDropped(): void;
	/** Input was refused (`terminal_exited`, `terminal_unresponsive`). */
	onInputError?(code: string, message: string): void;
	/** Called after the replay, so the size the panel shows wins. */
	onReplayed?(): void;
}

type SocketFactory = (url: string) => WebSocket;

export class TerminalConnection {
	private socket: WebSocket | null = null;
	private timer: ReturnType<typeof setTimeout> | null = null;
	/** Starts the retry count over once the socket has stayed live a while. */
	private stableTimer: ReturnType<typeof setTimeout> | null = null;
	private attempt = 0;
	private disposed = false;
	/** True between `replayed` and the next close: input and resizes go out only then. */
	private live = false;
	private size: { cols: number; rows: number } | null = null;

	constructor(
		private readonly id: string,
		private readonly handlers: ConnectionHandlers,
		private readonly makeSocket: SocketFactory = (url) => new WebSocket(url),
	) {}

	connect() {
		if (this.disposed) return;
		this.clearTimer();
		this.live = false;
		this.handlers.onStatus(this.attempt === 0 ? 'connecting' : 'reconnecting', null);
		const socket = this.makeSocket(wsUrl(`/api/v1/ws/terminals/${encodeURIComponent(this.id)}`));
		socket.binaryType = 'arraybuffer';
		this.socket = socket;
		socket.onmessage = (event) => this.message(event);
		socket.onclose = (event) => {
			if (this.socket !== socket) return;
			this.socket = null;
			this.live = false;
			this.clearStable();
			this.closed(event.code);
		};
	}

	private message(event: MessageEvent) {
		if (event.data instanceof ArrayBuffer) {
			this.handlers.onData(new Uint8Array(event.data));
			return;
		}
		if (typeof event.data !== 'string') return;
		let frame: { type?: string; terminal?: TerminalInfo; code?: string; message?: string };
		try { frame = JSON.parse(event.data); } catch { return; }
		switch (frame.type) {
			case 'attached':
				this.live = false;
				this.handlers.onReset();
				if (frame.terminal) this.handlers.onState(frame.terminal);
				break;
			case 'replayed':
				this.live = true;
				// Not at once: a socket closed again for being too far behind
				// (4408) right after its replay must keep backing off.
				this.clearStable();
				this.stableTimer = setTimeout(() => { this.stableTimer = null; this.attempt = 0; }, STABLE_AFTER);
				this.handlers.onStatus('live', null);
				if (this.size) this.send({ type: 'resize', ...this.size });
				this.handlers.onReplayed?.();
				break;
			case 'state':
				if (frame.terminal) this.handlers.onState(frame.terminal);
				break;
			case 'error':
				this.handlers.onInputError?.(frame.code ?? 'error', frame.message ?? 'The terminal refused that input.');
				break;
			case 'ping':
				this.send({ type: 'pong' });
				break;
		}
	}

	private closed(code: number) {
		if (this.disposed) return;
		const action = closeAction(code, this.attempt);
		switch (action.kind) {
			case 'drop':
				this.handlers.onDropped();
				return;
			case 'auth':
				this.handlers.onStatus('auth', 'Signed out. Sign in again to use this terminal.');
				return;
			case 'gone':
				this.handlers.onStatus('gone', 'The worker stopped, so this terminal ended.');
				return;
			case 'fatal':
				this.handlers.onStatus('error', action.message);
				return;
			case 'retry':
				if (this.attempt >= MAX_RECONNECTS) {
					this.handlers.onStatus('error', "Can't reach this terminal. Check the connection, then reload.");
					return;
				}
				this.attempt += 1;
				this.handlers.onStatus('reconnecting', null);
				this.timer = setTimeout(() => this.connect(), action.delay);
		}
	}

	private send(frame: Record<string, unknown>): boolean {
		const socket = this.socket;
		if (!socket || socket.readyState !== WebSocket.OPEN) return false;
		socket.send(JSON.stringify(frame));
		return true;
	}

	/** Keystrokes as typed. Dropped until the replay is done, and while offline. */
	input(data: string): boolean {
		if (!this.live || !data) return false;
		return this.send({ type: 'input', data });
	}

	resize(cols: number, rows: number) {
		if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) return;
		this.size = { cols, rows };
		if (this.live) this.send({ type: 'resize', cols, rows });
	}

	/** Reconnect now (after "Retry"). */
	retry() {
		if (this.disposed) return;
		this.attempt = 0;
		this.clearStable();
		this.socket?.close();
		this.socket = null;
		this.connect();
	}

	dispose() {
		this.disposed = true;
		this.clearTimer();
		this.clearStable();
		const socket = this.socket;
		this.socket = null;
		if (socket) {
			socket.onmessage = null;
			socket.onclose = null;
			socket.close(1000);
		}
	}

	private clearStable() {
		if (this.stableTimer) clearTimeout(this.stableTimer);
		this.stableTimer = null;
	}

	private clearTimer() {
		if (this.timer) clearTimeout(this.timer);
		this.timer = null;
	}
}
