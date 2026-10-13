import {
	isBrowserWorkspaceState,
	type BrowserWorkspaceState,
} from './workspace';
import { wsUrl } from '$lib/api/runtime';
/**
 * Browser screencast WebSocket client.
 *
 * Mirrors the chat ws-connection conventions (exponential-backoff reconnect with
 * jitter, document-visibility + `online` + BFCache `pageshow` revive, clean
 * intentional disconnect) but speaks the browser-stream protocol:
 *
 *   ws(s)://{host}/api/v1/ws/browser/{sessionId}?token=…
 *
 *   server→client: hello · status · page · frame · inspect-hit · error · ping
 *   client→server: input · navigate · command · inspect · viewport · pong
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
	'no_page' | 'connecting' | 'live' | 'disconnected' | 'browser_off';

export interface FrameMessageMetadata {
	runtime_id?: string;
	tab_id?: string;
	deviceWidth: number;
	deviceHeight: number;
	pageScaleFactor?: number;
	offsetTop?: number;
	scrollOffsetX?: number;
	scrollOffsetY?: number;
}

export interface BrowserStreamConfig {
	/** Session whose browser page is being streamed. */
	sessionId?: string;
	/** Dedicated provider credit-login stream, independent of chat sessions. */
	providerId?: string;
	/** Auth token from cookies (mirrors the chat WS client), or null if none. */
	getAuthToken: () => string | null;
	/** A `status` frame arrived. */
	onStatus: (state: StreamStatus, url: string | null) => void;
	/**
	 * Toolbar page state changed (main-frame URL / loading flag). Optional —
	 * absent on the very first `hello` exchange until the hub has tracked state.
	 */
	onPage?: (url: string | null, loading: boolean) => void;
	/** A `frame` (base64 JPEG + metadata) arrived. */
	onFrame: (dataBase64: string, metadata: FrameMessageMetadata) => void;
	/**
	 * An `inspect-hit` arrived: the element descriptor answering this client's
	 * `inspect` request (or null on a miss). `element` is raw page-provided
	 * data — parse with `parseElementHit` before use.
	 */
	onInspectHit?: (mode: 'hover' | 'select', element: unknown) => void;
	/** An `error` frame arrived. */
	onError?: (message: string) => void;
	/** The socket opened. */
	onOpen?: () => void;
	onHello?: (viewerId: string) => void;
	onWorkspace?: (state: BrowserWorkspaceState | null) => void;
	onCommandResult?: (id: string, ok: boolean, error?: string) => void;
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

function buildUrl(sessionId: string | undefined, token: string | null, providerId?: string): string {
	if (!providerId && !sessionId) throw new Error('A browser session or provider is required');
	const path = providerId
		? `/api/v1/model-providers/${encodeURIComponent(providerId)}/openai-billing/browser`
		: `/api/v1/ws/browser/${encodeURIComponent(sessionId!)}`;
	return wsUrl(path, { token });
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createBrowserStreamClient(
	config: BrowserStreamConfig,
): BrowserStreamClient {
	let ws: WebSocket | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectAttempts = 0;
	let intentionalDisconnect = false;
	let destroyed = false;
	let accessDenied = false;
	let stopped = false;
	let lastMessageAt = 0;
	// A provider endpoint is a login-only legacy stream, never a workspace.
	if (config.providerId && config.sessionId)
		throw new Error('Choose a browser session or provider, not both');
	const providerLogin = Boolean(config.providerId);
	let protocolReady = false;
	let workspace: BrowserWorkspaceState | null = null;
	const pending = new Map<string, ReturnType<typeof setTimeout>>();
	function settle(id: string, ok: boolean, error?: string) {
		const timer = pending.get(id);
		if (!timer) return;
		clearTimeout(timer);
		pending.delete(id);
		config.onCommandResult?.(id, ok, error);
	}
	function reset() {
		protocolReady = false;
		workspace = null;
		config.onWorkspace?.(null);
		for (const id of pending.keys())
			settle(
				id,
				false,
				'Connection changed. Command outcome unknown; it was not replayed.',
			);
	}

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
		if (destroyed || accessDenied) return;
		if (
			ws &&
			(ws.readyState === WebSocket.OPEN ||
				ws.readyState === WebSocket.CONNECTING)
		)
			return;

		intentionalDisconnect = false;
		stopped = false;
		const url = buildUrl(config.sessionId, config.getAuthToken(), config.providerId);
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

			reset();
			const wasClean = intentionalDisconnect;
			config.onClose?.(wasClean);

			if ([4001, 4003, 4401, 4403, 1008].includes(event.code)) {
				// Auth failure — no reconnect.
				accessDenied = true;
				config.onError?.(
					'Access denied. Reopen the panel after signing in with access to this session.',
				);
			} else if (intentionalDisconnect) {
				intentionalDisconnect = false;
			} else {
				scheduleReconnect();
			}
		};

		ws.onerror = (error) => {
			console.warn('[BrowserStream] socket error:', error);
		};

		const socket = ws;
		ws.onmessage = (event) => {
			if (socket !== ws) return;
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
		if (!msg || typeof msg !== 'object' || Array.isArray(msg)) return;
		if (msg.type === 'hello') {
			const valid = providerLogin
				? msg.protocol === 1
				: msg.protocol === 2 && typeof msg.viewer_id === 'string' && Boolean(msg.viewer_id.trim());
			if (!valid) {
				reset();
				return;
			}
			protocolReady = true;
			if (!providerLogin) config.onHello?.(msg.viewer_id as string);
			return;
		}
		// Login streams cannot deliver workspace state, inspection, or workspace URLs.
		if (providerLogin && !['status', 'page', 'frame', 'error', 'ping'].includes(String(msg.type))) return;
		if (['workspace', 'frame', 'inspect-hit'].includes(String(msg.type)) && !protocolReady) return;
		switch (msg.type) {
			case 'workspace': {
				const next = msg.state;
				if (!isBrowserWorkspaceState(next)) break;
				if (
					workspace?.runtime_id === next.runtime_id &&
					workspace.revision > next.revision
				)
					break;
				if (workspace && workspace.runtime_id !== next.runtime_id) {
					for (const id of pending.keys())
						settle(
							id,
							false,
							'Browser runtime replaced. Command was not replayed.',
						);
				}
				if (!next.available)
					for (const id of pending.keys())
						settle(
							id,
							false,
							'Browser runtime unavailable. Command outcome unknown; it was not replayed.',
						);
				workspace = next;
				config.onWorkspace?.(next);
				break;
			}
			case 'command-result':
				if (typeof msg.id === 'string')
					settle(
						msg.id,
						msg.ok === true,
						typeof msg.error === 'string' ? msg.error : undefined,
					);
				break;
			case 'status':
				config.onStatus(
					(msg.state as StreamStatus) ?? 'disconnected',
					!providerLogin && typeof msg.url === 'string' ? msg.url : null,
				);
				break;
			case 'page':
				config.onPage?.(
					!providerLogin && typeof msg.url === 'string' ? msg.url : null,
					msg.loading === true,
				);
				break;
			case 'frame':
				const meta = (msg.metadata ?? {}) as FrameMessageMetadata;
				if (
					(meta.runtime_id && meta.runtime_id !== workspace?.runtime_id) ||
					(meta.tab_id && meta.tab_id !== workspace?.active_tab_id)
				)
					break;
				if (typeof msg.data === 'string') {
					config.onFrame(
						msg.data,
						(msg.metadata ?? {}) as FrameMessageMetadata,
					);
				}
				break;
			case 'inspect-hit':
				if (
					(msg.runtime_id && msg.runtime_id !== workspace?.runtime_id) ||
					(msg.tab_id && msg.tab_id !== workspace?.active_tab_id)
				)
					break;
				if (msg.mode === 'hover' || msg.mode === 'select') {
					config.onInspectHit?.(msg.mode, msg.element ?? null);
				}
				break;
			case 'error':
				if (typeof msg.id === 'string')
					settle(
						msg.id,
						false,
						typeof msg.message === 'string' ? msg.message : 'Command failed.',
					);
				config.onError?.(
					typeof msg.message === 'string' ? msg.message : 'Stream error',
				);
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
		if (destroyed || accessDenied) return;
		if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
			console.warn('[BrowserStream] max reconnect attempts reached');
			return;
		}
		if (reconnectTimer) clearTimeout(reconnectTimer);
		const baseDelay = Math.min(
			1000 * Math.pow(2, reconnectAttempts),
			MAX_RECONNECT_DELAY_MS,
		);
		const jitter = Math.random() * 1000;
		reconnectAttempts++;
		reconnectTimer = setTimeout(() => connect(), baseDelay + jitter);
	}

	function forceReconnectIfNeeded(reason: string) {
		if (destroyed || accessDenied || stopped) return;
		if (
			!ws ||
			ws.readyState === WebSocket.CLOSED ||
			ws.readyState === WebSocket.CLOSING
		) {
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
		stopped = true;
		reset();
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
		let pendingId: string | null = null;
		try {
			const payload = data as Record<string, unknown>;
			if (providerLogin) {
				if (!payload || !['pong', 'input', 'command', 'viewport'].includes(String(payload.type))) return false;
				if (payload.type !== 'pong' && !protocolReady) return false;
				ws.send(JSON.stringify(data));
				return true;
			}
			if (
				!payload ||
				!['pong', 'workspace-command', 'input', 'inspect'].includes(
					String(payload.type),
				)
			)
				return false;
			if (
				['workspace-command', 'input', 'inspect'].includes(String(payload.type))
			) {
				if (
					!protocolReady ||
					!workspace?.available ||
					payload.runtime_id !== workspace.runtime_id ||
					payload.tab_id !== workspace.active_tab_id
				)
					return false;
				if (
					typeof payload.id !== 'string' ||
					!payload.id ||
					pending.has(payload.id) ||
					pending.size >= 128
				)
					return false;
				if (payload.type !== 'inspect') {
					const id = payload.id;
					pendingId = id;
					pending.set(
						id,
						setTimeout(
							() =>
								settle(
									id,
									false,
									'Command timed out. Outcome unknown; it was not replayed.',
								),
							30_000,
						),
					);
				}
			}
			ws.send(JSON.stringify(data));
			return true;
		} catch (e) {
			if (pendingId !== null) {
				clearTimeout(pending.get(pendingId));
				pending.delete(pendingId);
			}
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
	const healthTimer = setInterval(
		() => forceReconnectIfNeeded('heartbeat missing'),
		15_000,
	);

	// ── Public API ───────────────────────────────────────────────────────

	return {
		connect,
		disconnect,
		send,
		isConnected: () => ws !== null && ws.readyState === WebSocket.OPEN,
		destroy() {
			destroyed = true;
			clearInterval(healthTimer);
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
