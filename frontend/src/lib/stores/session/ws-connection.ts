/**
 * WebSocket Connection Module — Manages the WebSocket lifecycle for the active session store.
 *
 * Extracted from active-session.ts to isolate connection concerns:
 * connecting, disconnecting, reconnecting with exponential backoff,
 * ping/pong keep-alive, visibility change handling, and connection health monitoring.
 */

import { wsUrl } from '$lib/api/runtime';

// ─── Constants ───────────────────────────────────────────────────────────────

const CONNECTION_HEALTH_TIMEOUT_MS = 90_000;
const STREAMING_HEALTH_TIMEOUT_MS = 45_000;
const TOOL_EXECUTION_TIMEOUT_MS = 120_000;
const MAX_RECONNECT_ATTEMPTS = 50;
const MAX_RECONNECT_DELAY_MS = 30_000;
const PING_INTERVAL_MS = 30_000;
const CONNECT_TIMEOUT_MS = 15_000;
// The server sends heartbeats every 30 seconds and does not reply to client
// pongs. Allow a full heartbeat interval plus scheduling/network delay on resume.
const RESUME_HEALTH_TIMEOUT_MS = PING_INTERVAL_MS + 5_000;

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface WsConnectionConfig {
	/** Returns the auth token from cookies, or null if none. */
	getAuthToken: () => string | null;
	/** Returns the device ID for identifying this client. */
	getDeviceId: () => string;
	/** Called when a parsed JSON message is received from the server. */
	onMessage: (data: Record<string, unknown>) => void;
	/** Called when the WebSocket connection is established. */
	onConnected: () => void;
	/**
	 * Called when the WebSocket connection is closed.
	 * @param code - The WebSocket close code.
	 * @param reason - The close reason string.
	 * @param wasClean - Whether the close was clean (intentional disconnect).
	 */
	onDisconnected: (code: number, reason: string, wasClean: boolean) => void;
	/** Returns whether the active session is currently streaming. */
	isStreaming: () => boolean;
	/** Returns whether there are pending tool executions. */
	hasPendingTools: () => boolean;
	/** Called when max reconnect attempts have been exhausted. */
	onMaxReconnectAttempts?: () => void;
	/** Called when the tab becomes hidden — allows the consumer to pause polling, etc. */
	onVisibilityHidden?: () => void;
	/** Called when the tab becomes visible — allows the consumer to resume polling, etc. */
	onVisibilityVisible?: () => void;
}

export interface WsConnection {
	/** Re-arm a destroyed connection for an explicit owner lifecycle remount. */
	activate: () => void;
	/** Establish the WebSocket connection. No-op if already connected or connecting. */
	connect: () => void;
	/** Replace the current socket immediately, including a stuck OPEN/CONNECTING socket. */
	reconnect: () => void;
	/** Intentionally disconnect (no auto-reconnect). Clears all timers. */
	disconnect: () => void;
	/** Send a JSON-serializable payload. Returns false if not connected. */
	send: (data: unknown) => boolean;
	/** Whether the WebSocket is currently in OPEN state. */
	isConnected: () => boolean;
	/** Reset reconnect attempt counter (e.g., before a manual reconnect). */
	resetReconnectAttempts: () => void;
	/** Full cleanup — disconnect + remove event listeners. */
	destroy: () => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const WS_CHAT_PATH = '/api/v1/ws/chat';

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createWsConnection(config: WsConnectionConfig): WsConnection {
	let ws: WebSocket | null = null;
	let pingTimer: ReturnType<typeof setInterval> | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let connectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectAttempts = 0;
	let enabled = true;
	let lastMessageTime = 0;
	let destroyed = false;

	// Server-side sequence tracking for delivery gap diagnostics
	let lastSeq = 0;
	let totalGaps = 0;

	// Visibility change handler reference (for cleanup)
	let visibilityHandler: (() => void) | null = null;
	let onlineHandler: (() => void) | null = null;
	let pageShowHandler: ((e: PageTransitionEvent) => void) | null = null;

	// Track visibility health-check timeout so it can be cancelled
	let visibilityCheckTimer: ReturnType<typeof setTimeout> | null = null;

	function stopVisibilityCheck() {
		if (visibilityCheckTimer !== null) {
			clearTimeout(visibilityCheckTimer);
			visibilityCheckTimer = null;
		}
	}

	function stopConnectTimer() {
		if (connectTimer !== null) clearTimeout(connectTimer);
		connectTimer = null;
	}

	function stopReconnectTimer() {
		if (reconnectTimer !== null) clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}

	// Retire ownership before closing. TCP close can stall or arrive after a
	// replacement has opened; neither may affect the new socket's state/timers.
	function retireSocket(code: number, reason: string) {
		const socket = ws;
		ws = null;
		stopPing();
		stopVisibilityCheck();
		stopConnectTimer();
		if (socket && socket.readyState !== WebSocket.CLOSED) {
			try { socket.close(code, reason); } catch { /* Already unusable. */ }
		}
		return socket;
	}

	function scheduleReconnect() {
		if (destroyed || !enabled) return;
		stopReconnectTimer();
		if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
			console.error('[WsConnection] Max reconnect attempts reached');
			config.onMaxReconnectAttempts?.();
			return;
		}
		const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
		reconnectAttempts++;
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			connect();
		}, delay + Math.random() * 1000);
	}

	function connectionLost(code: number, reason: string) {
		retireSocket(code, reason);
		console.warn(`[WsConnection] Connection lost: code=${code}, reason=${reason || '(none)'}`);
		if (code !== 4001) scheduleReconnect();
		config.onDisconnected(code, reason, false);
	}

	// ── Ping / Health Check ──────────────────────────────────────────────

	function stopPing() {
		if (pingTimer) {
			clearInterval(pingTimer);
			pingTimer = null;
		}
	}

	function startPing() {
		stopPing();
		lastMessageTime = Date.now();

		pingTimer = setInterval(() => {
			if (ws?.readyState === WebSocket.OPEN) {
				try {
					ws.send(JSON.stringify({ type: 'pong' }));
				} catch (e) {
					console.warn('[WsConnection] Failed to send ping:', e);
					reconnectAttempts = 0;
					connectionLost(4000, 'Keepalive failed');
					return;
				}

				let healthTimeout: number;
				if (config.hasPendingTools()) healthTimeout = TOOL_EXECUTION_TIMEOUT_MS;
				else if (config.isStreaming()) healthTimeout = STREAMING_HEALTH_TIMEOUT_MS;
				else healthTimeout = CONNECTION_HEALTH_TIMEOUT_MS;

				const timeSinceLastMessage = Date.now() - lastMessageTime;
				if (timeSinceLastMessage > healthTimeout) {
					// Transcript streaming uses SSE and says nothing about control
					// socket health. Server heartbeats continue during tools/turns.
					connectionLost(4000, 'Connection health timeout');
				}
			}
		}, PING_INTERVAL_MS);
	}

	// ── Connect ──────────────────────────────────────────────────────────

	function connect() {
		if (destroyed) return;
		enabled = true;
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
		stopReconnectTimer();
		retireSocket(4000, 'Replacing closed connection');

		const deviceId = config.getDeviceId();
		// Web UI: same-origin socket, cookie auth (plus an explicit token when the
		// caller has one). Bundled mobile UI: worker origin with `token=` added by
		// the runtime helper.
		const url = wsUrl(WS_CHAT_PATH, { device_id: deviceId, token: config.getAuthToken() });

		const socket = new WebSocket(url);
		ws = socket;
		connectTimer = setTimeout(() => {
			if (socket === ws) connectionLost(4000, 'Connection attempt timed out');
		}, CONNECT_TIMEOUT_MS);

		ws.onopen = () => {
			if (destroyed || socket !== ws) return;
			stopConnectTimer();
			reconnectAttempts = 0;
			lastSeq = 0; // Reset sequence tracking for new connection
			startPing();
			config.onConnected();
		};

		ws.onclose = (event) => {
			if (destroyed || socket !== ws) return;
			connectionLost(event.code, event.reason || '');
		};

		ws.onerror = (error) => {
			if (destroyed || socket !== ws) return;
			console.error('[WsConnection] WebSocket error:', error);
		};

		ws.onmessage = (event) => {
			if (destroyed || socket !== ws) return;
			lastMessageTime = Date.now();
			stopVisibilityCheck();
			try {
				const data = JSON.parse(event.data);

				// Track server-assigned sequence numbers for gap detection
				const seq = data._seq;
				if (typeof seq === 'number') {
					if (lastSeq > 0 && seq > lastSeq + 1) {
						const gap = seq - lastSeq - 1;
						totalGaps += gap;
						console.warn(
							`[WsConnection] Sequence gap: expected ${lastSeq + 1}, got ${seq} (${gap} missed, ${totalGaps} total)`
						);
					}
					lastSeq = seq;
				}

				config.onMessage(data);
			} catch (e) {
				console.error('[WsConnection] Failed to parse message:', e);
			}
		};
	}

	// ── Disconnect ───────────────────────────────────────────────────────

	function disconnect() {
		enabled = false;
		stopReconnectTimer();
		const socket = retireSocket(1000, '');
		if (socket && !destroyed) config.onDisconnected(1000, '', true);
	}

	function reconnect() {
		if (destroyed) return;
		enabled = true;
		reconnectAttempts = 0;
		stopReconnectTimer();
		const socket = retireSocket(4000, 'Manual reconnect');
		if (socket) config.onDisconnected(4000, 'Manual reconnect', false);
		connect();
	}

	// ── Send ─────────────────────────────────────────────────────────────

	function send(data: unknown): boolean {
		if (!ws || ws.readyState !== WebSocket.OPEN) return false;
		try {
			ws.send(JSON.stringify(data));
			return true;
		} catch (e) {
			console.warn('[WsConnection] Failed to send message:', e);
			connectionLost(4000, 'Send failed');
			return false;
		}
	}

	// ── Visibility Change ────────────────────────────────────────────────

	function setupVisibilityHandler() {
		if (typeof document === 'undefined') return;

		visibilityHandler = () => {
			if (destroyed || !enabled) return;

			// Cancel any pending health-check from a previous visibility change
			stopVisibilityCheck();

			if (document.hidden) {
				// Tab is hidden — pause pings to reduce idle CPU.
				// The backend ping will keep the connection alive.
				stopPing();
				config.onVisibilityHidden?.();
			} else {
				// Tab is visible again — resume pings and check connection health
				if (ws?.readyState === WebSocket.OPEN) {
					startPing();
				}

				config.onVisibilityVisible?.();

				if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
					reconnectAttempts = 0;
					connect();
				} else if (ws.readyState === WebSocket.OPEN) {
					// Check this socket against the next scheduled server heartbeat.
					// A successful send alone does not prove that TCP is still alive.
					const resumedSocket = ws;
					try {
						resumedSocket.send(JSON.stringify({ type: 'pong' }));
					} catch {
						reconnectAttempts = 0;
						connectionLost(4000, 'Resume keepalive failed');
						return;
					}
					visibilityCheckTimer = setTimeout(() => {
						visibilityCheckTimer = null;
						if (ws === resumedSocket && resumedSocket.readyState === WebSocket.OPEN) {
							console.warn('[WsConnection] No server response after tab return, forcing reconnect');
							connectionLost(4000, 'Half-open connection detected');
						}
					}, RESUME_HEALTH_TIMEOUT_MS);
				}
			}
		};

		document.addEventListener('visibilitychange', visibilityHandler);
	}

	// ── Network / BFCache Resume Handlers ────────────────────────────────

	function forceReconnectIfNeeded(reason: string) {
		if (destroyed || !enabled) return;
		// Notify consumer so it can trigger an immediate HTTP poll in parallel
		// with the WS reconnect. This is the key latency win on phone unlock:
		// fresh messages render before the socket handshake completes.
		config.onVisibilityVisible?.();

		if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
			reconnectAttempts = 0;
			connect();
			return;
		}
		if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
			// Retire immediately: a close handshake on a dead network may stall.
			console.warn(`[WsConnection] Force reconnect: ${reason}`);
			connectionLost(4000, reason);
		}
	}

	function setupNetworkHandlers() {
		if (typeof window === 'undefined') return;

		onlineHandler = () => {
			forceReconnectIfNeeded('network online');
		};
		window.addEventListener('online', onlineHandler);

		// pageshow with persisted=true fires on BFCache restore (common on
		// mobile Safari when unlocking). visibilitychange may not fire in
		// that case, so we need a dedicated hook.
		pageShowHandler = (e: PageTransitionEvent) => {
			if (e.persisted) forceReconnectIfNeeded('pageshow persisted');
		};
		window.addEventListener('pageshow', pageShowHandler);
	}

	// Set up visibility handler immediately
	setupVisibilityHandler();
	setupNetworkHandlers();

	// ── Public API ───────────────────────────────────────────────────────

	return {
		activate() {
			if (!destroyed) return;
			destroyed = false;
			enabled = true;
			reconnectAttempts = 0;
			setupVisibilityHandler();
			setupNetworkHandlers();
		},
		connect,
		reconnect,
		disconnect,
		send,

		isConnected(): boolean {
			return ws !== null && ws.readyState === WebSocket.OPEN;
		},

		resetReconnectAttempts() {
			reconnectAttempts = 0;
		},

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
