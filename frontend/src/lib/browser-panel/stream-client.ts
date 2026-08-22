/**
 * Browser screencast WebSocket client.
 *
 * Mirrors the chat ws-connection conventions (exponential-backoff reconnect with
 * jitter, document-visibility + `online` + BFCache `pageshow` revive, clean
 * intentional disconnect) but speaks the browser-stream protocol:
 *
 *   ws(s)://{host}/api/v1/ws/browser/{sessionId}?token=…
 *
 *   server→client: hello · status · frame · error · ping
 *   client→server: input · navigate · pong
 *
 * The caller creates ONE client per (open panel, session). `connect()` is a
 * no-op while already connected/connecting; `destroy()` tears down the socket
 * and removes the global revive listeners.
 */

// ─── Constants ───────────────────────────────────────────────────────────────

const MAX_RECONNECT_ATTEMPTS = 50;
const MAX_RECONNECT_DELAY_MS = 30_000;
/**
 * The server pings every 30 s, so a healthy socket is never silent for longer
 * than that. Anything past this window is treated as possibly half-open and
 * gets probed rather than trusted.
 */
const STALE_SILENCE_MS = 45_000;
/** Grace period after a keepalive probe before declaring the socket dead. */
const PROBE_TIMEOUT_MS = 3_000;

// ─── Protocol types ──────────────────────────────────────────────────────────

export type StreamStatus =
	| 'no_page'
	| 'connecting'
	| 'live'
	| 'disconnected'
	| 'browser_off';

export interface FrameMessageMetadata {
	deviceWidth: number;
	deviceHeight: number;
	pageScaleFactor?: number;
	offsetTop?: number;
	scrollOffsetX?: number;
	scrollOffsetY?: number;
}

export interface BrowserStreamConfig {
	/** Session whose browser page is being streamed. */
	sessionId: string;
	/** Auth token from cookies (mirrors the chat WS client), or null if none. */
	getAuthToken: () => string | null;
	/** A `status` frame arrived. */
	onStatus: (state: StreamStatus, url: string | null) => void;
	/** A `frame` (base64 JPEG + metadata) arrived. */
	onFrame: (dataBase64: string, metadata: FrameMessageMetadata) => void;
	/** An `error` frame arrived. */
	onError?: (message: string) => void;
	/** The socket opened. */
	onOpen?: () => void;
	/** The socket closed (clean or not). */
	onClose?: (wasClean: boolean) => void;
}

export interface BrowserStreamClient {
	/** Establish the connection. No-op if already open/connecting. */
	connect: () => void;
	/** Intentionally disconnect (no auto-reconnect). */
	disconnect: () => void;
	/** Send a JSON-serialisable payload. Returns false if not open. */
	send: (data: unknown) => boolean;
	/** Whether the socket is OPEN. */
	isConnected: () => boolean;
	/** Full teardown: disconnect + remove revive listeners. */
	destroy: () => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function buildUrl(sessionId: string, token: string | null): string {
	const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const host = window.location.host;
	let url = `${protocol}//${host}/api/v1/ws/browser/${encodeURIComponent(sessionId)}`;
	if (token) url += `?token=${encodeURIComponent(token)}`;
	return url;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createBrowserStreamClient(config: BrowserStreamConfig): BrowserStreamClient {
	let ws: WebSocket | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectAttempts = 0;
	let intentionalDisconnect = false;
	let destroyed = false;
	let lastMessageAt = 0;
	let probeTimer: ReturnType<typeof setTimeout> | null = null;

	function clearProbe() {
		if (probeTimer) {
			clearTimeout(probeTimer);
			probeTimer = null;
		}
	}

	let visibilityHandler: (() => void) | null = null;
	let onlineHandler: (() => void) | null = null;
	let pageShowHandler: ((e: PageTransitionEvent) => void) | null = null;

	// ── Connect ──────────────────────────────────────────────────────────

	function connect() {
		if (destroyed) return;
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

		intentionalDisconnect = false;
		const url = buildUrl(config.sessionId, config.getAuthToken());
		ws = new WebSocket(url);

		ws.onopen = () => {
			reconnectAttempts = 0;
			lastMessageAt = Date.now();
			clearProbe();
			config.onOpen?.();
		};

		ws.onclose = (event) => {
			// Ignore stale close events from a superseded socket instance.
			if (event.target !== ws && ws !== null) return;

			const wasClean = intentionalDisconnect;
			config.onClose?.(wasClean);

			if (event.code === 4001) {
				// Auth failure — no reconnect.
				console.warn('[BrowserStream] auth failed (4001)');
			} else if (intentionalDisconnect) {
				intentionalDisconnect = false;
			} else {
				scheduleReconnect();
			}
		};

		ws.onerror = (error) => {
			console.warn('[BrowserStream] socket error:', error);
		};

		ws.onmessage = (event) => {
			// Traffic of ANY kind proves the socket is alive: cancel a pending probe.
			lastMessageAt = Date.now();
			clearProbe();
			let msg: Record<string, unknown>;
			try {
				msg = JSON.parse(event.data as string);
			} catch (e) {
				console.warn('[BrowserStream] failed to parse message:', e);
				return;
			}
			handleMessage(msg);
		};
	}

	function handleMessage(msg: Record<string, unknown>) {
		switch (msg.type) {
			case 'hello':
				// Protocol handshake — nothing to do beyond noting we're talking.
				break;
			case 'status':
				config.onStatus(
					(msg.state as StreamStatus) ?? 'disconnected',
					typeof msg.url === 'string' ? msg.url : null,
				);
				break;
			case 'frame':
				if (typeof msg.data === 'string') {
					config.onFrame(msg.data, (msg.metadata ?? {}) as FrameMessageMetadata);
				}
				break;
			case 'error':
				config.onError?.(typeof msg.message === 'string' ? msg.message : 'Stream error');
				break;
			case 'ping':
				send({ type: 'pong' });
				break;
			default:
				// Unknown/forward-compatible frame — ignore.
				break;
		}
	}

	// ── Reconnect (exponential backoff + jitter) ───────────────────────────

	function scheduleReconnect() {
		if (destroyed) return;
		if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
			console.warn('[BrowserStream] max reconnect attempts reached');
			return;
		}
		if (reconnectTimer) clearTimeout(reconnectTimer);
		const baseDelay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
		const jitter = Math.random() * 1000;
		reconnectAttempts++;
		reconnectTimer = setTimeout(() => connect(), baseDelay + jitter);
	}

	function forceReconnectIfNeeded(reason: string) {
		if (destroyed) return;
		if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
			reconnectAttempts = 0;
			connect();
			return;
		}
		if (ws.readyState !== WebSocket.OPEN) return;

		// An OPEN socket is presumed healthy. Tearing it down on every tab focus
		// or `online` event was the panel's main flicker source: each close cost a
		// full reconnect, and the reconnect re-ran the server's connecting → live
		// handshake. Only a socket that has already missed a server ping is
		// suspicious enough to probe.
		if (Date.now() - lastMessageAt < STALE_SILENCE_MS) return;
		if (probeTimer) return; // a probe is already in flight

		// Keepalive write. `pong` is the only unsolicited client frame the server
		// tolerates — anything else (including `ping`) is a protocol violation and
		// gets the socket closed with 4400, which is exactly what we're avoiding.
		send({ type: 'pong' });

		probeTimer = setTimeout(() => {
			probeTimer = null;
			if (destroyed || !ws || ws.readyState !== WebSocket.OPEN) return;
			if (Date.now() - lastMessageAt < STALE_SILENCE_MS) return;
			// Silent through the probe window: really half-open. Close and let the
			// reconnect path re-establish.
			console.warn(`[BrowserStream] force reconnect: ${reason}`);
			try {
				ws.close(4000, reason);
			} catch {
				/* ignore */
			}
		}, PROBE_TIMEOUT_MS);
	}

	// ── Disconnect / Send ──────────────────────────────────────────────────

	function disconnect() {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		clearProbe();
		intentionalDisconnect = true;
		if (ws) {
			try {
				ws.close(1000);
			} catch {
				/* ignore */
			}
			ws = null;
		}
	}

	function send(data: unknown): boolean {
		if (!ws || ws.readyState !== WebSocket.OPEN) return false;
		try {
			ws.send(JSON.stringify(data));
			return true;
		} catch (e) {
			console.warn('[BrowserStream] failed to send:', e);
			return false;
		}
	}

	// ── Revive listeners ───────────────────────────────────────────────────

	function setupReviveListeners() {
		if (typeof document !== 'undefined') {
			visibilityHandler = () => {
				if (destroyed || document.hidden) return;
				forceReconnectIfNeeded('tab visible');
			};
			document.addEventListener('visibilitychange', visibilityHandler);
		}
		if (typeof window !== 'undefined') {
			onlineHandler = () => forceReconnectIfNeeded('network online');
			window.addEventListener('online', onlineHandler);

			pageShowHandler = (e: PageTransitionEvent) => {
				if (e.persisted) forceReconnectIfNeeded('pageshow persisted');
			};
			window.addEventListener('pageshow', pageShowHandler);
		}
	}

	setupReviveListeners();

	// ── Public API ───────────────────────────────────────────────────────

	return {
		connect,
		disconnect,
		send,
		isConnected: () => ws !== null && ws.readyState === WebSocket.OPEN,
		destroy() {
			destroyed = true;
			disconnect();
			if (visibilityHandler && typeof document !== 'undefined') {
				document.removeEventListener('visibilitychange', visibilityHandler);
				visibilityHandler = null;
			}
			if (typeof window !== 'undefined') {
				if (onlineHandler) {
					window.removeEventListener('online', onlineHandler);
					onlineHandler = null;
				}
				if (pageShowHandler) {
					window.removeEventListener('pageshow', pageShowHandler);
					pageShowHandler = null;
				}
			}
		},
	};
}
