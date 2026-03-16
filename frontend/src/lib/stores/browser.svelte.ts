/**
 * Browser State Store
 *
 * Manages the shared browser session between AI agent and user.
 * Both the AI (via MCP proxy) and user (via browser WebSocket) operate on
 * the same Playwright browser instance in the main process.
 *
 * The user sees a live CDP screencast streamed via a dedicated browser
 * WebSocket (/ws/browser/{sessionId}). The AI receives accessibility tree
 * snapshots (not screenshots) for token-efficient page understanding.
 *
 * Browser state is per-session: switching sessions saves/restores
 * each session's browser content while keeping panel width as a global pref.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $browserState auto-subscription in .svelte files.
 */
import { sessionNotifier } from './session/session-notifier';
import { createBrowserWs, type BrowserWsConnection } from './browser-ws';
import { untrack } from 'svelte';

export interface BrowserState {
	/** Whether the browser panel is open */
	isOpen: boolean;
	/** Current page URL */
	url: string;
	/** Current page title */
	title: string;
	/** Whether a browser session is active */
	isActive: boolean;
	/** Browser panel width in pixels */
	panelWidth: number;
	/** Whether we're in a loading state (e.g., navigating) */
	isLoading: boolean;
	/** The session ID for API calls */
	sessionId: string | null;
	/** Whether live screencast is streaming */
	isStreaming: boolean;
	/** Display mode: 'screencast' (CDP frames) or 'novnc' (iframe) */
	displayMode: 'screencast' | 'novnc' | null;
	/** noVNC WebSocket URL for iframe mode */
	novncUrl: string | null;
}

/** Per-session browser content (saved/restored on session switch) */
interface BrowserSessionContent {
	isOpen: boolean;
	url: string;
	title: string;
	isActive: boolean;
	sessionId: string | null;
}

const DEFAULT_STATE: BrowserState = {
	isOpen: false,
	url: '',
	title: '',
	isActive: false,
	panelWidth: 480,
	isLoading: false,
	sessionId: null,
	isStreaming: false,
	displayMode: null,
	novncUrl: null
};

const EMPTY_CONTENT: BrowserSessionContent = {
	isOpen: false,
	url: '',
	title: '',
	isActive: false,
	sessionId: null
};

const PANEL_WIDTH_KEY = 'browser-panel-width';

function _loadPanelWidth(): number {
	try {
		const stored = localStorage.getItem(PANEL_WIDTH_KEY);
		if (stored) {
			const w = parseInt(stored, 10);
			if (w >= 300 && w <= 1400) return w;
		}
	} catch {
		/* SSR or storage error */
	}
	return 480;
}

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<BrowserState>({ ...DEFAULT_STATE, panelWidth: _loadPanelWidth() });

// ============================================================================
// Subscribe shim
// ============================================================================

type Subscriber = (value: BrowserState) => void;

const _subscribers = new Set<Subscriber>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: BrowserState) => BrowserState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

function _set(newState: BrowserState) {
	_state = newState;
	_notifySubscribers();
}

// ============================================================================
// Internal state (not reactive, not exposed)
// ============================================================================

/** LRU cache of per-session browser content (max 50 entries) */
const _sessionCache = new Map<string, BrowserSessionContent>();
const MAX_CACHE = 50;

function _saveToCache(chatSessionId: string) {
	_sessionCache.delete(chatSessionId); // re-insert at end
	_sessionCache.set(chatSessionId, {
		isOpen: _state.isOpen,
		url: _state.url,
		title: _state.title,
		isActive: _state.isActive,
		sessionId: _state.sessionId
	});
	// Evict oldest
	while (_sessionCache.size > MAX_CACHE) {
		const oldest = _sessionCache.keys().next().value;
		if (oldest !== undefined) _sessionCache.delete(oldest);
	}
}

function _restoreFromCache(chatSessionId: string): BrowserSessionContent {
	const cached = _sessionCache.get(chatSessionId);
	if (cached) {
		_sessionCache.delete(chatSessionId);
		_sessionCache.set(chatSessionId, cached);
		return cached;
	}
	return { ...EMPTY_CONTENT };
}

// Browser WebSocket connection
let _browserWs: BrowserWsConnection | null = null;
let _frameCallback: ((data: ArrayBuffer) => void) | null = null;

function _ensureBrowserWs(): BrowserWsConnection {
	if (!_browserWs) {
		_browserWs = createBrowserWs({
			onFrame: (data) => {
				if (!_state.isStreaming) {
					_update((s) => ({ ...s, isStreaming: true }));
				}
				if (_frameCallback) _frameCallback(data);
			},
			onStateUpdate: (url, title) => {
				_update((s) => ({ ...s, url, title }));
			},
			onError: (message) => {
				console.warn('[browser-ws] Error:', message);
			},
			onConnected: () => {
				// isStreaming deferred to first frame arrival
			},
			onDisconnected: () => {
				_update((s) => ({ ...s, isStreaming: false }));
			}
		});
	}
	return _browserWs;
}

// ============================================================================
// Exported Store
// ============================================================================

export const browserState = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

	/**
	 * Register a frame callback -- called by BrowserPanel when canvas is ready.
	 */
	setFrameCallback(callback: ((data: ArrayBuffer) => void) | null) {
		_frameCallback = callback;
	},

	/**
	 * Get the browser WebSocket connection (for sending input events).
	 */
	getBrowserWs(): BrowserWsConnection {
		return _ensureBrowserWs();
	},

	/**
	 * Connect the screencast WebSocket to a session.
	 * First queries the display mode endpoint to determine rendering strategy.
	 */
	connectScreencast(sessionId: string, width: number, height: number) {
		// Query display mode from backend (fire-and-forget to avoid
		// async store updates re-triggering the calling $effect)
		fetch(`/api/v1/browser/${sessionId}/display`, { credentials: 'include' })
			.then((r) => r.ok ? r.json() : null)
			.then((info) => {
				if (info?.mode === 'novnc' && info.novnc_port) {
					const host = window.location.hostname;
					const novncUrl = `http://${host}:${info.novnc_port}/vnc.html?autoconnect=true&resize=scale`;
					_update((s) => ({ ...s, displayMode: 'novnc', novncUrl }));
				} else {
					// CDP screencast mode — connect WebSocket
					_update((s) => ({ ...s, displayMode: 'screencast', novncUrl: null }));
					const ws = _ensureBrowserWs();
					ws.connect(sessionId, width, height);
				}
			})
			.catch(() => {
				// Fallback to screencast
				_update((s) => ({ ...s, displayMode: 'screencast', novncUrl: null }));
				const ws = _ensureBrowserWs();
				ws.connect(sessionId, width, height);
			});
	},

	/**
	 * Disconnect the screencast WebSocket.
	 */
	disconnectScreencast() {
		if (_browserWs) {
			_browserWs.disconnect();
		}
		_update((s) => ({ ...s, isStreaming: false }));
	},

	/**
	 * Update browser state from a tool result.
	 */
	updateFromToolResult(toolName: string, result: string, sessionId?: string) {
		try {
			const data = JSON.parse(result);
			if (!data.success) return;

			_update((s) => {
				const newState = { ...s };

				if (data.url !== undefined) newState.url = data.url;
				if (data.title !== undefined) newState.title = data.title;
				if (sessionId) newState.sessionId = sessionId;
				newState.isActive = true;

				if (!s.isOpen && toolName.startsWith('browser_')) {
					newState.isOpen = true;
				}

				if (toolName === 'browser_close') {
					newState.isActive = false;
					newState.isStreaming = false;
					newState.url = '';
					newState.title = '';
					queueMicrotask(() => {
						if (_browserWs) _browserWs.disconnect();
					});
				}

				return newState;
			});
		} catch {
			// Non-JSON result, ignore
		}
	},

	/** Toggle the browser panel open/closed. */
	togglePanel() {
		_update((s) => ({ ...s, isOpen: !s.isOpen }));
	},

	/** Open the browser panel. */
	openPanel() {
		_update((s) => ({ ...s, isOpen: true }));
	},

	/** Close the browser panel UI and destroy the backend Playwright session. */
	async closePanel() {
		if (_browserWs) {
			_browserWs.disconnect();
		}

		const sid = _state.sessionId;
		if (sid) {
			try {
				await fetch(`/api/v1/browser/${sid}`, {
					method: 'DELETE',
					credentials: 'include'
				});
			} catch {
				// Best-effort cleanup
			}
			_sessionCache.delete(sid);
		}
		_set({ ...DEFAULT_STATE, panelWidth: _state.panelWidth });
	},

	/** Set panel width (for resizing). */
	setPanelWidth(width: number) {
		const clamped = Math.max(300, Math.min(1400, width));
		_update((s) => ({ ...s, panelWidth: clamped }));
		try {
			localStorage.setItem(PANEL_WIDTH_KEY, String(clamped));
		} catch {
			/* ignore */
		}
	},

	/** Set loading state. */
	setLoading(loading: boolean) {
		_update((s) => ({ ...s, isLoading: loading }));
	},

	/** Reset the store to default state. */
	reset() {
		if (_browserWs) {
			_browserWs.disconnect();
		}
		_set({ ...DEFAULT_STATE });
	},

	/**
	 * Switch browser context between chat sessions.
	 */
	switchSession(leavingSessionId: string | null, arrivingSessionId: string | null) {
		if (_browserWs) {
			_browserWs.disconnect();
		}

		if (leavingSessionId) {
			_saveToCache(leavingSessionId);
		}

		const content = arrivingSessionId
			? _restoreFromCache(arrivingSessionId)
			: { ...EMPTY_CONTENT };

		_update((s) => ({
			...s,
			isOpen: content.isOpen,
			url: content.url,
			title: content.title,
			isActive: content.isActive,
			sessionId: content.sessionId,
			isLoading: false,
			isStreaming: false
		}));
	},

	// =====================================================================
	// User-driven browser actions (via REST API for intentional navigation)
	// =====================================================================

	/** Create a browser session via REST API. */
	async createSession(sessionId: string): Promise<boolean> {
		_update((s) => ({ ...s, isLoading: true }));
		try {
			const response = await fetch(`/api/v1/browser/${sessionId}/create`, {
				method: 'POST',
				credentials: 'include',
				headers: { 'Content-Type': 'application/json' }
			});

			if (!response.ok) {
				_update((s) => ({ ...s, isLoading: false }));
				return false;
			}

			_update((s) => ({
				...s,
				sessionId,
				isActive: true,
				isLoading: false
			}));
			return true;
		} catch {
			_update((s) => ({ ...s, isLoading: false }));
			return false;
		}
	},

	/** Ensure a browser session exists, creating one if needed. */
	async _ensureSession(): Promise<string | null> {
		if (_state.sessionId) return _state.sessionId;
		const tempId = 'user-browser-' + Date.now();
		const ok = await browserState.createSession(tempId);
		return ok ? tempId : null;
	},

	/** Execute a browser action via REST API and update state from response. */
	async _executeAction(
		sessionId: string,
		endpoint: string,
		body?: Record<string, unknown>
	): Promise<boolean> {
		_update((s) => ({ ...s, isLoading: true }));
		try {
			const response = await fetch(`/api/v1/browser/${sessionId}/${endpoint}`, {
				method: 'POST',
				credentials: 'include',
				headers: { 'Content-Type': 'application/json' },
				body: body ? JSON.stringify(body) : '{}'
			});

			if (!response.ok) {
				_update((s) => ({ ...s, isLoading: false }));
				return false;
			}

			const data = await response.json();
			_update((s) => ({
				...s,
				url: data.url ?? s.url,
				title: data.title ?? s.title,
				isActive: true,
				isLoading: false
			}));
			return true;
		} catch {
			_update((s) => ({ ...s, isLoading: false }));
			return false;
		}
	},

	/** Navigate to a URL. Auto-creates session if needed. */
	async navigate(url: string) {
		let sid = _state.sessionId;
		if (!sid) {
			sid = await browserState._ensureSession();
			if (!sid) return false;
		}
		return browserState._executeAction(sid, 'navigate', { url });
	},

	/** Go back in history. */
	async goBack() {
		const sid = _state.sessionId;
		if (!sid) return false;
		return browserState._executeAction(sid, 'back');
	},

	/** Go forward in history. */
	async goForward() {
		const sid = _state.sessionId;
		if (!sid) return false;
		return browserState._executeAction(sid, 'forward');
	},

	/** Refresh the current page. */
	async refresh() {
		const sid = _state.sessionId;
		if (!sid) return false;
		return browserState._executeAction(sid, 'refresh');
	}
};

// ============================================================================
// Event listener for browser tool results from WebSocket
// ============================================================================

sessionNotifier.onEvent((event) => {
	const ev = event as Record<string, unknown>;

	let toolName: string | undefined;
	let content: string | undefined;
	let sessionId: string | undefined = event.session_id ?? undefined;

	if (ev.type === 'tool_result') {
		toolName = ev.name as string | undefined;
		content = ev.output as string | undefined;
	} else if (ev.event_type === 'stream_chunk') {
		const data = ev.data as Record<string, unknown> | undefined;
		if (data?.chunk_type !== 'tool_result') return;
		toolName = data.tool_name as string | undefined;
		content = data.content as string | undefined;
		if (!sessionId) sessionId = ev.session_id as string | undefined;
	} else {
		return;
	}

	if (
		toolName &&
		(toolName.startsWith('browser_') || toolName.includes('browser-tools__browser_')) &&
		content
	) {
		const normalizedName = toolName.includes('__browser_')
			? 'browser_' + toolName.split('__browser_').pop()
			: toolName;
		browserState.updateFromToolResult(normalizedName, content, sessionId);
	}
});
