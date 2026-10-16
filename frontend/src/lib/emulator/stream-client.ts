/**
 * The Phone panel's screen socket:
 *
 *   ws(s)://{host}/api/v1/emulator/devices/{id}/stream?token=…
 *
 *   server→client: state · frame · error   (ping is answered with pong)
 *   client→server: tap · swipe · text · key · rotate
 *
 * Reconnects with exponential backoff while it is wanted, and immediately when
 * the page comes back online or into view. A close that reconnecting cannot
 * fix (unknown device 4004, malformed input 4400, not allowed 4001/4403…) is
 * terminal: the client stops and reports why, until `retry()`.
 */
import { wsUrl } from '$lib/api/runtime';
import {
	encodeClientMessage,
	parseServerMessage,
	reconnectDelay,
	terminalCloseMessage,
	type ClientMessage,
	type ErrorMessage,
	type FrameMessage,
	type StateMessage,
} from './protocol';

export type LinkState = 'connecting' | 'live' | 'reconnecting' | 'failed';

export interface EmulatorStreamConfig {
	deviceId: string;
	getAuthToken: () => string | null;
	onFrame: (frame: FrameMessage) => void;
	onState?: (state: StateMessage) => void;
	/** An input the server could not apply; the socket stays open. */
	onError?: (error: ErrorMessage) => void;
	/** `failure` explains a terminal stop (state `failed`). */
	onLink?: (state: LinkState, detail?: { retryInMs?: number; failure?: string; code?: number }) => void;
	/** Tests inject a fake socket. */
	WebSocketImpl?: typeof WebSocket;
	random?: () => number;
}

export interface EmulatorStreamClient {
	connect: () => void;
	/** Leave a terminal stop and connect again. */
	retry: () => void;
	send: (msg: ClientMessage) => boolean;
	isOpen: () => boolean;
	destroy: () => void;
}

export function streamPath(deviceId: string): string {
	return `/api/v1/emulator/devices/${encodeURIComponent(deviceId)}/stream`;
}

export function createEmulatorStream(config: EmulatorStreamConfig): EmulatorStreamClient {
	const WS = config.WebSocketImpl ?? WebSocket;
	let ws: WebSocket | null = null;
	let attempts = 0;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;
	let failed = false;

	const revive = () => {
		if (destroyed || failed) return;
		if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
		if (ws && (ws.readyState === WS.OPEN || ws.readyState === WS.CONNECTING)) return;
		if (timer) clearTimeout(timer);
		timer = null;
		attempts = 0;
		open();
	};
	if (typeof window !== 'undefined') {
		window.addEventListener('online', revive);
		document.addEventListener('visibilitychange', revive);
	}

	function schedule() {
		if (destroyed || failed || timer) return;
		const delay = reconnectDelay(attempts, config.random);
		attempts += 1;
		config.onLink?.('reconnecting', { retryInMs: delay });
		timer = setTimeout(() => {
			timer = null;
			open();
		}, delay);
	}

	function open() {
		if (destroyed || failed) return;
		config.onLink?.(attempts ? 'reconnecting' : 'connecting');
		const socket = new WS(wsUrl(streamPath(config.deviceId), { token: config.getAuthToken() }));
		ws = socket;
		socket.onopen = () => {
			if (socket !== ws) return;
			attempts = 0;
			config.onLink?.('live');
		};
		socket.onmessage = (event: MessageEvent) => {
			if (socket !== ws) return;
			if (typeof event.data === 'string' && event.data.includes('"ping"')) {
				try {
					if (JSON.parse(event.data)?.type === 'ping') {
						socket.send(JSON.stringify({ type: 'pong' }));
						return;
					}
				} catch { /* not JSON: fall through and drop */ }
			}
			const msg = parseServerMessage(event.data);
			if (!msg) return;
			if (msg.type === 'frame') config.onFrame(msg);
			else if (msg.type === 'state') config.onState?.(msg);
			else config.onError?.(msg);
		};
		socket.onclose = (event: CloseEvent) => {
			if (socket !== ws) return;
			ws = null;
			if (destroyed) return;
			const failure = terminalCloseMessage(event.code);
			if (failure) {
				failed = true;
				config.onLink?.('failed', { failure, code: event.code });
				return;
			}
			schedule();
		};
		socket.onerror = () => { /* onclose follows and decides */ };
	}

	return {
		connect() {
			if (destroyed || failed) return;
			if (ws || timer) return;
			open();
		},
		retry() {
			if (destroyed) return;
			failed = false;
			attempts = 0;
			if (timer) clearTimeout(timer);
			timer = null;
			if (!ws) open();
		},
		send(msg) {
			if (!ws || ws.readyState !== WS.OPEN) return false;
			ws.send(encodeClientMessage(msg));
			return true;
		},
		isOpen: () => !!ws && ws.readyState === WS.OPEN,
		destroy() {
			destroyed = true;
			if (timer) clearTimeout(timer);
			timer = null;
			if (typeof window !== 'undefined') {
				window.removeEventListener('online', revive);
				document.removeEventListener('visibilitychange', revive);
			}
			const socket = ws;
			ws = null;
			if (socket && (socket.readyState === WS.OPEN || socket.readyState === WS.CONNECTING)) socket.close(1000);
		},
	};
}
