/**
 * WebSocket Connection Module — Manages the WebSocket lifecycle for the active session store.
 *
 * Extracted from active-session.ts to isolate connection concerns:
 * connecting, disconnecting, reconnecting with exponential backoff,
 * ping/pong keep-alive, visibility change handling, and connection health monitoring.
 */

// ─── Constants ───────────────────────────────────────────────────────────────

const CONNECTION_HEALTH_TIMEOUT_MS = 90_000;
const STREAMING_HEALTH_TIMEOUT_MS = 45_000;
const TOOL_EXECUTION_TIMEOUT_MS = 120_000;
const MAX_RECONNECT_ATTEMPTS = 50;
const MAX_RECONNECT_DELAY_MS = 30_000;
const PING_INTERVAL_MS = 30_000;

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
	/** Establish the WebSocket connection. No-op if already connected or connecting. */
	connect: () => void;
	/** Intentionally disconnect (no auto-reconnect). Clears all timers. */
	disconnect: () => void;
	/** Send a JSON-serializable payload. Returns false if not connected. */
	send: (data: unknown) => boolean;
	/** Whether the WebSocket is currently in OPEN state. */
	isConnected: () => boolean;
	/** Update lastMessageTime to now (called externally when messages are sent). */
	resetLastMessageTime: () => void;
	/** Reset reconnect attempt counter (e.g., before a manual reconnect). */
	resetReconnectAttempts: () => void;
	/** Full cleanup — disconnect + remove event listeners. */
	destroy: () => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getWsBaseUrl(): string {
	const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const host = window.location.host;
	return `${protocol}//${host}/api/v1/ws/chat`;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createWsConnection(config: WsConnectionConfig): WsConnection {
	let ws: WebSocket | null = null;
	let pingTimer: ReturnType<typeof setInterval> | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectAttempts = 0;
	let intentionalDisconnect = false;
	let lastMessageTime = 0;
	let destroyed = false;

	// Visibility change handler reference (for cleanup)
	let visibilityHandler: (() => void) | null = null;

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
					connect();
					return;
				}

				let healthTimeout: number;
				if (config.hasPendingTools()) healthTimeout = TOOL_EXECUTION_TIMEOUT_MS;
				else if (config.isStreaming()) healthTimeout = STREAMING_HEALTH_TIMEOUT_MS;
				else healthTimeout = CONNECTION_HEALTH_TIMEOUT_MS;

				const timeSinceLastMessage = Date.now() - lastMessageTime;
				if (timeSinceLastMessage > healthTimeout) {
					if (config.isStreaming()) {
						// If streaming, the poller may be active — WS is secondary.
						// Let the consumer decide via the callback whether to skip force-close.
						// For now, replicate original behavior: skip force-close if streaming.
					} else {
						console.warn(`[WsConnection] Connection stale (${Math.round(timeSinceLastMessage / 1000)}s), forcing reconnect`);
						ws.close(4000, 'Connection health timeout');
					}
				}
			}
		}, PING_INTERVAL_MS);
	}

	// ── Connect ──────────────────────────────────────────────────────────

	function connect() {
		if (destroyed) return;
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

		const deviceId = config.getDeviceId();
		let url = `${getWsBaseUrl()}?device_id=${encodeURIComponent(deviceId)}`;

		const token = config.getAuthToken();
		if (token) url = `${url}&token=${encodeURIComponent(token)}`;

		ws = new WebSocket(url);

		ws.onopen = () => {
			reconnectAttempts = 0;
			config.onConnected();
			startPing();
		};

		ws.onclose = (event) => {
			// Guard: ignore stale close events from a previous WebSocket instance.
			// Without this, an old WS's onclose can call stopPing() and kill the
			// health-check timer that belongs to the new, active connection.
			if (event.target !== ws && ws !== null) {
				return;
			}

			stopPing();

			const wasClean = intentionalDisconnect;
			config.onDisconnected(event.code, event.reason || '', wasClean);

			if (event.code === 4001) {
				// Auth failure — consumer handles redirect, no reconnect
				console.warn('[WsConnection] WebSocket auth failed (4001)');
			} else if (intentionalDisconnect) {
				intentionalDisconnect = false;
			} else {
				// Unexpected close — reconnect with exponential backoff + jitter
				if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
					console.error('[WsConnection] Max reconnect attempts reached');
					config.onMaxReconnectAttempts?.();
					return;
				}

				if (reconnectTimer) clearTimeout(reconnectTimer);
				const baseDelay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
				const jitter = Math.random() * 1000;
				reconnectAttempts++;
				reconnectTimer = setTimeout(() => connect(), baseDelay + jitter);
			}
		};

		ws.onerror = (error) => {
			console.error('[WsConnection] WebSocket error:', error);
		};

		ws.onmessage = (event) => {
			lastMessageTime = Date.now();
			try {
				config.onMessage(JSON.parse(event.data));
			} catch (e) {
				console.error('[WsConnection] Failed to parse message:', e);
			}
		};
	}

	// ── Disconnect ───────────────────────────────────────────────────────

	function disconnect() {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		stopPing();

		intentionalDisconnect = true;
		if (ws) {
			ws.close(1000);
			ws = null;
		}
	}

	// ── Send ─────────────────────────────────────────────────────────────

	function send(data: unknown): boolean {
		if (!ws || ws.readyState !== WebSocket.OPEN) return false;
		try {
			ws.send(JSON.stringify(data));
			return true;
		} catch (e) {
			console.warn('[WsConnection] Failed to send message:', e);
			return false;
		}
	}

	// ── Visibility Change ────────────────────────────────────────────────

	function setupVisibilityHandler() {
		if (typeof document === 'undefined') return;

		visibilityHandler = () => {
			if (destroyed) return;

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
					// Don't blindly reset lastMessageTime — the connection may be
					// half-open (browser reports OPEN but TCP is dead). Instead,
					// send a pong and verify we get a server response within 5s.
					const preVisibilityTime = lastMessageTime;
					try {
						ws.send(JSON.stringify({ type: 'pong' }));
					} catch {
						reconnectAttempts = 0;
						connect();
						return;
					}
					// If no server message arrives within 5s, force reconnect
					setTimeout(() => {
						if (lastMessageTime === preVisibilityTime && ws?.readyState === WebSocket.OPEN) {
							console.warn('[WsConnection] No server response after tab return, forcing reconnect');
							ws.close(4000, 'Half-open connection detected');
						}
					}, 5000);
				}
			}
		};

		document.addEventListener('visibilitychange', visibilityHandler);
	}

	// Set up visibility handler immediately
	setupVisibilityHandler();

	// ── Public API ───────────────────────────────────────────────────────

	return {
		connect,
		disconnect,
		send,

		isConnected(): boolean {
			return ws !== null && ws.readyState === WebSocket.OPEN;
		},

		resetLastMessageTime() {
			lastMessageTime = Date.now();
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
		},
	};
}
