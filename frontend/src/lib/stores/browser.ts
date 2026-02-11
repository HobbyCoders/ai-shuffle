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
		 * Also triggers a screenshot fetch from the REST API.
		 */
		async updateFromToolResult(toolName: string, result: string, sessionId?: string) {
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
						newState.screenshot = null;
					}

					return newState;
				});

				// Fetch screenshot from REST API after tool completes
				// (tools that modify the page include screenshots in their MCP response
				// to the model, but we need to fetch it separately for the panel)
				if (toolName !== 'browser_close' && toolName !== 'browser_tab_list' && toolName !== 'browser_get_content' && toolName !== 'browser_get_elements') {
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
	};
}

export const browserState = createBrowserStore();

// Register a listener on the session notifier to handle browser tool results.
// When a tool_result WS event arrives with a browser_* tool name, update the browser state.
sessionNotifier.onEvent((event) => {
	const chunkType = (event as Record<string, unknown>).chunk_type as string | undefined;
	if (chunkType !== 'tool_result') return;

	const toolName = (event as Record<string, unknown>).tool_name as string | undefined;
	const content = (event as Record<string, unknown>).content as string | undefined;
	const sessionId = event.session_id;

	if (toolName && toolName.startsWith('browser_') && content) {
		browserState.updateFromToolResult(toolName, content, sessionId || undefined);
	}
});
