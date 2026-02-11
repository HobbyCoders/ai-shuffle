/**
 * Browser State Store
 *
 * Manages the state of the AI agent's browser session,
 * including URL, title, screenshots, and panel visibility.
 *
 * Screenshots are fetched from the REST API after each browser tool
 * completes, since image content from MCP goes to the model but the
 * text result comes to the frontend via WebSocket.
 */
import { writable } from 'svelte/store';
import { sessionNotifier } from './session/session-notifier';

export interface BrowserState {
	/** Whether the browser panel is open */
	isOpen: boolean;
	/** Current page URL */
	url: string;
	/** Current page title */
	title: string;
	/** Latest screenshot as base64 JPEG */
	screenshot: string | null;
	/** Whether a browser session is active */
	isActive: boolean;
	/** Browser panel width in pixels */
	panelWidth: number;
	/** Whether we're loading a screenshot */
	isLoading: boolean;
	/** The session ID for API calls */
	sessionId: string | null;
}

const DEFAULT_STATE: BrowserState = {
	isOpen: false,
	url: '',
	title: '',
	screenshot: null,
	isActive: false,
	panelWidth: 480,
	isLoading: false,
	sessionId: null,
};

function createBrowserStore() {
	const { subscribe, set, update } = writable<BrowserState>({ ...DEFAULT_STATE });

	let _currentState = { ...DEFAULT_STATE };
	// Keep a copy of current state for non-reactive access
	subscribe((state) => {
		_currentState = state;
	});

	return {
		subscribe,

		/**
		 * Update browser state from a tool result.
		 * Called when a browser_* tool result comes through WebSocket.
		 * Uses inline screenshot from MCP content blocks if available,
		 * falls back to REST API fetch.
		 */
		async updateFromToolResult(toolName: string, result: string, sessionId?: string, screenshot?: string) {
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

					// Use inline screenshot from MCP content blocks
					if (screenshot) newState.screenshot = screenshot;

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
						newState.screenshot = null;
					}

					return newState;
				});

				// If no inline screenshot, try fetching from REST API as fallback
				if (!screenshot && toolName !== 'browser_close' && toolName !== 'browser_tab_list' && toolName !== 'browser_get_content' && toolName !== 'browser_get_elements') {
					const sid = sessionId || _currentState.sessionId;
					if (sid) {
						await this.fetchScreenshot(sid);
					}
				}
			} catch {
				// Non-JSON result, ignore
			}
		},

		/**
		 * Fetch screenshot from the REST API.
		 */
		async fetchScreenshot(sessionId: string) {
			update((state) => ({ ...state, isLoading: true }));

			try {
				const token = localStorage.getItem('auth_token') || '';
				const response = await fetch(`/api/v1/browser/${sessionId}/screenshot`, {
					headers: {
						'Authorization': `Bearer ${token}`,
					},
				});

				if (response.ok) {
					const blob = await response.blob();
					const reader = new FileReader();
					reader.onload = () => {
						const base64 = (reader.result as string).split(',')[1];
						update((state) => ({
							...state,
							screenshot: base64,
							isLoading: false,
						}));
					};
					reader.readAsDataURL(blob);
				} else {
					update((state) => ({ ...state, isLoading: false }));
				}
			} catch {
				update((state) => ({ ...state, isLoading: false }));
			}
		},

		/**
		 * Update screenshot directly from base64 data.
		 */
		updateScreenshot(screenshotBase64: string) {
			update((state) => ({
				...state,
				screenshot: screenshotBase64,
				isLoading: false,
			}));
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
		 * Close the browser panel.
		 */
		closePanel() {
			update((state) => ({
				...state,
				isOpen: false,
			}));
		},

		/**
		 * Set panel width (for resizing).
		 */
		setPanelWidth(width: number) {
			update((state) => ({
				...state,
				panelWidth: Math.max(300, Math.min(800, width)),
			}));
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
			set({ ...DEFAULT_STATE });
		},

		// =====================================================================
		// User-driven browser actions
		// =====================================================================

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
						'Authorization': `Bearer ${token}`,
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
					screenshot: data.screenshot ?? state.screenshot,
					isLoading: false,
				}));
				return true;
			} catch {
				update((state) => ({ ...state, isLoading: false }));
				return false;
			}
		},

		/** Navigate to a URL. */
		async navigate(url: string) {
			const sid = _currentState.sessionId;
			if (!sid) return false;
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

		/** Click at coordinates (viewport-space). */
		async clickAt(x: number, y: number) {
			const sid = _currentState.sessionId;
			if (!sid) return false;
			return this._executeAction(sid, 'click', { x: Math.round(x), y: Math.round(y) });
		},

		/** Type text. */
		async typeText(text: string, selector?: string) {
			const sid = _currentState.sessionId;
			if (!sid) return false;
			return this._executeAction(sid, 'type', { text, selector });
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
	let screenshot: string | undefined;
	let sessionId: string | undefined = event.session_id ?? undefined;

	if (ev.type === 'tool_result') {
		// Direct WS event from originating device
		toolName = ev.name as string | undefined;
		content = ev.output as string | undefined;
		screenshot = ev.screenshot as string | undefined;
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
	if (toolName && (toolName.startsWith('browser_') || toolName.includes('browser-tools__browser_')) && content) {
		// Normalize MCP prefix to plain tool name for updateFromToolResult
		const normalizedName = toolName.includes('__browser_')
			? 'browser_' + toolName.split('__browser_').pop()
			: toolName;
		browserState.updateFromToolResult(normalizedName, content, sessionId, screenshot);
	}
});
