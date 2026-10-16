/**
 * The Phone panel's screen socket:
 *
 *   ws(s)://{host}/api/v1/emulator/devices/{id}/stream?token=…
 *
 *   server→client: frame · state   (ping is answered with pong)
 *   client→server: tap · swipe · text · key · rotate
 *
 * Reconnects with exponential backoff while it is wanted, and immediately when
 * the page comes back online or into view. Auth closes stop it for good.
 */
import { wsUrl } from '$lib/api/runtime';
import {
	encodeClientMessage,
	parseServerMessage,
	reconnectDelay,
	type ClientMessage,
	type DeviceState,
	type FrameMessage,
} from './protocol';

export type LinkState = 'connecting' | 'live' | 'reconnecting' | 'denied';

export interface EmulatorStreamConfig {
	deviceId: string;
	getAuthToken: () => string | null;
	onFrame: (frame: FrameMessage) => void;
	onState?: (state: DeviceState) => void;
	onLink?: (state: LinkState, retryInMs?: number) => void;
	/** Tests inject a fake socket. */
	WebSocketImpl?: typeof WebSocket;
	random?: () => number;
}

export interface EmulatorStreamClient {
	connect: () => void;
	send: (msg: ClientMessage) => boolean;
	isOpen: () => boolean;
	destroy: () => void;
}

const AUTH_CLOSE_CODES = new Set([1008, 4001, 4003, 4401, 4403]);

export function streamPath(deviceId: string): string {
	return `/api/v1/emulator/devices/${encodeURIComponent(deviceId)}/stream`;
}

export function createEmulatorStream(config: EmulatorStreamConfig): EmulatorStreamClient {
	const WS = config.WebSocketImpl ?? WebSocket;
	let ws: WebSocket | null = null;
	let attempts = 0;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;
	let denied = false;

	const revive = () => {
		if (destroyed || denied) return;
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
		if (destroyed || denied || timer) return;
		const delay = reconnectDelay(attempts, config.random);
		attempts += 1;
		config.onLink?.('reconnecting', delay);
		timer = setTimeout(() => {
			timer = null;
			open();
		}, delay);
	}

	function open() {
		if (destroyed || denied) return;
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
			else config.onState?.(msg.state);
		};
		socket.onclose = (event: CloseEvent) => {
			if (socket !== ws) return;
			ws = null;
			if (destroyed) return;
			if (AUTH_CLOSE_CODES.has(event.code)) {
				denied = true;
				config.onLink?.('denied');
				return;
			}
			schedule();
		};
		socket.onerror = () => { /* onclose follows and schedules the retry */ };
	}

	return {
		connect() {
			if (destroyed || denied) return;
			if (ws || timer) return;
			open();
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
