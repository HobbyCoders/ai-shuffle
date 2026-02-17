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
 */
import { writable } from 'svelte/store';
import { sessionNotifier } from './session/session-notifier';
import { createBrowserWs, type BrowserWsConnection } from './browser-ws';

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
	/** Current browser viewport width (matches panel) */
	viewportWidth: number;
	/** Current browser viewport height (matches panel) */
	viewportHeight: number;
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
	viewportWidth: 1280,
	viewportHeight: 720,
};

/** Empty content for sessions with no browser activity */
const EMPTY_CONTENT: BrowserSessionContent = {
	isOpen: false,
	url: '',
	title: '',
	isActive: false,
	sessionId: null,
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

function createBrowserStore() {
	const initialState = { ...DEFAULT_STATE, panelWidth: _loadPanelWidth() };
	const { subscribe, set, update } = writable<BrowserState>(initialState);

	let _currentState = initialState;
	// Keep a copy of current state for non-reactive access
	subscribe((state) => {
		_currentState = state;
	});

	/** LRU cache of per-session browser content (max 50 entries) */
	const _sessionCache = new Map<string, BrowserSessionContent>();
	const MAX_CACHE = 50;

	function _saveToCache(chatSessionId: string) {
		_sessionCache.delete(chatSessionId); // re-insert at end
		_sessionCache.set(chatSessionId, {
			isOpen: _currentState.isOpen,
			url: _currentState.url,
			title: _currentState.title,
			isActive: _currentState.isActive,
			sessionId: _currentState.sessionId,
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
			// Move to most recently used
			_sessionCache.delete(chatSessionId);
			_sessionCache.set(chatSessionId, cached);
			return cached;
		}
		return { ...EMPTY_CONTENT };
	}

	// Browser WebSocket connection — handles screencast frames + input
	let _browserWs: BrowserWsConnection | null = null;

	// Callback: render frame to the canvas (set by BrowserPanel component)
	let _frameCallback: ((data: ArrayBuffer) => void) | null = null;

	function _ensureBrowserWs(): BrowserWsConnection {
		if (!_browserWs) {
			_browserWs = createBrowserWs({
				onFrame: (data) => {
					if (_frameCallback) _frameCallback(data);
				},
				onStateUpdate: (url, title) => {
					update((state) => ({ ...state, url, title }));
				},
				onError: (message) => {
					console.warn('[browser-ws] Error:', message);
				},
				onConnected: () => {
					update((state) => ({ ...state, isStreaming: true }));
				},
				onDisconnected: () => {
					update((state) => ({ ...state, isStreaming: false }));
				},
			});
		}
		return _browserWs;
	}

	return {
		subscribe,

		/**
		 * Register a frame callback — called by BrowserPanel when canvas is ready.
		 * The callback receives raw JPEG ArrayBuffer data for each screencast frame.
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
		 * Called when the browser panel opens or session changes.
		 */
		connectScreencast(sessionId: string, width: number, height: number) {
			const ws = _ensureBrowserWs();
			ws.connect(sessionId, width, height);
			update((state) => ({
				...state,
				viewportWidth: Math.round(width),
				viewportHeight: Math.round(height),
			}));
		},

		/**
		 * Disconnect the screencast WebSocket.
		 * Called when the browser panel closes or session switches away.
		 */
		disconnectScreencast() {
			if (_browserWs) {
				_browserWs.disconnect();
			}
			update((state) => ({ ...state, isStreaming: false }));
		},

		/**
		 * Update browser state from a tool result.
		 * Called when a browser_* tool result comes through the chat WebSocket.
		 * Only updates URL/title — screencast handles visual rendering.
		 */
		updateFromToolResult(toolName: string, result: string, sessionId?: string) {
			try {
				const data = JSON.parse(result);
				if (!data.success) return;

				update((state) => {
					const newState = { ...state };

					// Update URL and title if present
					if (data.url !== undefined) newState.url = data.url;
					if (data.title !== undefined) newState.title = data.title;

					// Store session ID for API calls
					if (sessionId) newState.sessionId = sessionId;

					// Mark as active
					newState.isActive = true;

					// Auto-open panel on first browser tool use
					if (!state.isOpen && toolName.startsWith('browser_')) {
						newState.isOpen = true;
					}

					// Handle browser_close
					if (toolName === 'browser_close') {
						newState.isActive = false;
						newState.url = '';
						newState.title = '';
					}

					return newState;
				});
			} catch {
				// Non-JSON result, ignore
			}
		},

		/**
		 * Toggle the browser panel open/closed.
		 */
		togglePanel() {
			update((state) => ({
				...state,
				isOpen: !state.isOpen,
			}));
		},

		/**
		 * Open the browser panel.
		 */
		openPanel() {
			update((state) => ({
				...state,
				isOpen: true,
			}));
		},

		/**
		 * Close the browser panel UI and destroy the backend Playwright session.
		 */
		async closePanel() {
			// Disconnect screencast first
			if (_browserWs) {
				_browserWs.disconnect();
			}

			const sid = _currentState.sessionId;
			// Close backend browser session
			if (sid) {
				try {
					const token = localStorage.getItem('auth_token') || '';
					await fetch(`/api/v1/browser/${sid}`, {
						method: 'DELETE',
						headers: { Authorization: `Bearer ${token}` },
					});
				} catch {
					// Best-effort cleanup
				}
				// Remove from session cache so it doesn't restore stale state
				_sessionCache.delete(sid);
			}
			// Reset all state
			set({ ...DEFAULT_STATE, panelWidth: _currentState.panelWidth });
		},

		/**
		 * Set panel width (for resizing).
		 */
		setPanelWidth(width: number) {
			const clamped = Math.max(300, Math.min(1400, width));
			update((state) => ({
				...state,
				panelWidth: clamped,
			}));
			try {
				localStorage.setItem(PANEL_WIDTH_KEY, String(clamped));
			} catch {
				/* ignore */
			}
		},

		/**
		 * Set loading state.
		 */
		setLoading(loading: boolean) {
			update((state) => ({
				...state,
				isLoading: loading,
			}));
		},

		/**
		 * Reset the store to default state.
		 */
		reset() {
			if (_browserWs) {
				_browserWs.disconnect();
			}
			set({ ...DEFAULT_STATE });
		},

		/**
		 * Switch browser context between chat sessions.
		 * Saves the leaving session's browser state and restores the arriving session's.
		 */
		switchSession(leavingSessionId: string | null, arrivingSessionId: string | null) {
			// Disconnect screencast for the session we're leaving
			if (_browserWs) {
				_browserWs.disconnect();
			}

			// Save current browser content for the session we're leaving
			if (leavingSessionId) {
				_saveToCache(leavingSessionId);
			}

			// Restore browser content for the arriving session
			const content = arrivingSessionId
				? _restoreFromCache(arrivingSessionId)
				: { ...EMPTY_CONTENT };

			update((state) => ({
				...state,
				// panelWidth stays as global pref
				isOpen: content.isOpen,
				url: content.url,
				title: content.title,
				isActive: content.isActive,
				sessionId: content.sessionId,
				isLoading: false,
				isStreaming: false,
			}));

			// Note: BrowserPanel's $effect will reconnect screencast if panel is open
		},

		// =====================================================================
		// User-driven browser actions (via REST API for intentional navigation)
		// =====================================================================

		/**
		 * Create a browser session via REST API (for user-initiated browsing).
		 */
		async createSession(sessionId: string): Promise<boolean> {
			update((state) => ({ ...state, isLoading: true }));
			try {
				const token = localStorage.getItem('auth_token') || '';
				const response = await fetch(`/api/v1/browser/${sessionId}/create`, {
					method: 'POST',
					headers: {
						Authorization: `Bearer ${token}`,
						'Content-Type': 'application/json',
					},
				});

				if (!response.ok) {
					update((state) => ({ ...state, isLoading: false }));
					return false;
				}

				update((state) => ({
					...state,
					sessionId,
					isActive: true,
					isLoading: false,
				}));
				return true;
			} catch {
				update((state) => ({ ...state, isLoading: false }));
				return false;
			}
		},

		/**
		 * Ensure a browser session exists, creating one if needed.
		 */
		async _ensureSession(): Promise<string | null> {
			if (_currentState.sessionId) return _currentState.sessionId;
			const tempId = 'user-browser-' + Date.now();
			const ok = await this.createSession(tempId);
			return ok ? tempId : null;
		},

		/**
		 * Execute a browser action via REST API and update state from response.
		 */
		async _executeAction(
			sessionId: string,
			endpoint: string,
			body?: Record<string, unknown>
		): Promise<boolean> {
			update((state) => ({ ...state, isLoading: true }));
			try {
				const token = localStorage.getItem('auth_token') || '';
				const response = await fetch(`/api/v1/browser/${sessionId}/${endpoint}`, {
					method: 'POST',
					headers: {
						Authorization: `Bearer ${token}`,
						'Content-Type': 'application/json',
					},
					body: body ? JSON.stringify(body) : '{}',
				});

				if (!response.ok) {
					update((state) => ({ ...state, isLoading: false }));
					return false;
				}

				const data = await response.json();
				update((state) => ({
					...state,
					url: data.url ?? state.url,
					title: data.title ?? state.title,
					isActive: true,
					isLoading: false,
				}));
				return true;
			} catch {
				update((state) => ({ ...state, isLoading: false }));
				return false;
			}
		},

		/** Navigate to a URL. Auto-creates session if needed. */
		async navigate(url: string) {
			let sid = _currentState.sessionId;
			if (!sid) {
				sid = await this._ensureSession();
				if (!sid) return false;
			}
			return this._executeAction(sid, 'navigate', { url });
		},

		/** Go back in history. */
		async goBack() {
			const sid = _currentState.sessionId;
			if (!sid) return false;
			return this._executeAction(sid, 'back');
		},

		/** Go forward in history. */
		async goForward() {
			const sid = _currentState.sessionId;
			if (!sid) return false;
			return this._executeAction(sid, 'forward');
		},

		/** Refresh the current page. */
		async refresh() {
			const sid = _currentState.sessionId;
			if (!sid) return false;
			return this._executeAction(sid, 'refresh');
		},
	};
}

export const browserState = createBrowserStore();

// Register a listener on the session notifier to handle browser tool results.
// When a tool_result WS event arrives with a browser_* tool name, update the browser state.
//
// Events arrive in two formats:
// 1. Direct WS events: { type: "tool_result", name: "mcp__browser-tools__browser_navigate", output: "..." }
// 2. Sync events: { event_type: "stream_chunk", data: { chunk_type: "tool_result", tool_name: "...", content: "..." } }
sessionNotifier.onEvent((event) => {
	const ev = event as Record<string, unknown>;

	let toolName: string | undefined;
	let content: string | undefined;
	let sessionId: string | undefined = event.session_id ?? undefined;

	if (ev.type === 'tool_result') {
		// Direct WS event from originating device
		toolName = ev.name as string | undefined;
		content = ev.output as string | undefined;
	} else if (ev.event_type === 'stream_chunk') {
		// Sync event from another device
		const data = ev.data as Record<string, unknown> | undefined;
		if (data?.chunk_type !== 'tool_result') return;
		toolName = data.tool_name as string | undefined;
		content = data.content as string | undefined;
		if (!sessionId) sessionId = ev.session_id as string | undefined;
	} else {
		return;
	}

	// Match both MCP-prefixed names (mcp__browser-tools__browser_*) and plain names (browser_*)
	if (
		toolName &&
		(toolName.startsWith('browser_') || toolName.includes('browser-tools__browser_')) &&
		content
	) {
		// Normalize MCP prefix to plain tool name for updateFromToolResult
		const normalizedName = toolName.includes('__browser_')
			? 'browser_' + toolName.split('__browser_').pop()
			: toolName;
		browserState.updateFromToolResult(normalizedName, content, sessionId);
	}
});
