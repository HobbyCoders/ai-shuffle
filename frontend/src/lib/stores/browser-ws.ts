/**
 * Browser WebSocket — Dedicated connection for live screencast + input.
 *
 * Manages the /ws/browser/{sessionId} WebSocket which carries:
 * - Binary frames (JPEG screencast) from server → rendered to canvas
 * - JSON state updates (url, title) from server → update store
 * - JSON input events (mouse, key, scroll, resize) to server → CDP dispatch
 *
 * Separate from the chat WebSocket to avoid bandwidth interference.
 */

export interface BrowserWsCallbacks {
	/** Called with raw JPEG bytes for each screencast frame */
	onFrame: (data: ArrayBuffer) => void;
	/** Called when the browser navigates — updates URL/title */
	onStateUpdate: (url: string, title: string) => void;
	/** Called on WebSocket errors */
	onError: (message: string) => void;
	/** Called when the WebSocket connects */
	onConnected: () => void;
	/** Called when the WebSocket disconnects */
	onDisconnected: () => void;
}

export interface BrowserWsConnection {
	/** Connect to the browser WebSocket and start screencast */
	connect: (sessionId: string, width: number, height: number) => void;
	/** Disconnect and stop screencast */
	disconnect: () => void;
	/** Send a mouse event (click, move, down, up, dblclick) */
	sendMouse: (action: string, x: number, y: number, button?: string) => void;
	/** Send a key event (press, down, up) */
	sendKey: (action: string, key: string, code: string, text?: string, modifiers?: number) => void;
	/** Send a scroll event */
	sendScroll: (x: number, y: number, deltaX: number, deltaY: number) => void;
	/** Send a resize event — triggers viewport change + screencast restart */
	sendResize: (width: number, height: number) => void;
	/** Check if the WebSocket is connected */
	isConnected: () => boolean;
}

/**
 * Create a browser WebSocket connection manager.
 *
 * Usage:
 *   const ws = createBrowserWs({
 *     onFrame: (data) => renderToCanvas(data),
 *     onStateUpdate: (url, title) => updateStore(url, title),
 *     ...
 *   });
 *   ws.connect('session-123', 800, 600);
 */
export function createBrowserWs(callbacks: BrowserWsCallbacks): BrowserWsConnection {
	let ws: WebSocket | null = null;
	let currentSessionId: string | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	function getWsUrl(sessionId: string): string {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		// Auth is handled via cookies (same as chat WebSocket).
		// No token query param needed — authenticate_websocket reads cookies.
		return `${protocol}//${host}/api/v1/ws/browser/${sessionId}`;
	}

	function connect(sessionId: string, width: number, height: number): void {
		// Skip if already connected (or connecting) to the same session
		if (currentSessionId === sessionId && ws && ws.readyState <= WebSocket.OPEN) {
			return;
		}

		// Clean up any existing connection
		disconnect();

		currentSessionId = sessionId;

		try {
			ws = new WebSocket(getWsUrl(sessionId));
			ws.binaryType = 'arraybuffer'; // Critical: binary frames are JPEG bytes
		} catch (e) {
			callbacks.onError(`WebSocket creation failed: ${e}`);
			return;
		}

		ws.onopen = () => {
			callbacks.onConnected();
			// Start screencast at the panel's dimensions
			sendJson({
				type: 'start',
				width: Math.round(width),
				height: Math.round(height),
				quality: 60,
				fps: 10,
			});
		};

		ws.onmessage = (event: MessageEvent) => {
			if (event.data instanceof ArrayBuffer) {
				// Binary frame — raw JPEG bytes from CDP screencast
				callbacks.onFrame(event.data);
			} else if (typeof event.data === 'string') {
				// JSON message
				try {
					const data = JSON.parse(event.data);
					if (data.type === 'state') {
						callbacks.onStateUpdate(data.url || '', data.title || '');
					} else if (data.type === 'error') {
						callbacks.onError(data.message || 'Unknown browser error');
					} else if (data.type === 'ping') {
						sendJson({ type: 'pong' });
					}
				} catch {
					/* ignore malformed JSON */
				}
			}
		};

		ws.onclose = () => {
			callbacks.onDisconnected();
			ws = null;
		};

		ws.onerror = () => {
			callbacks.onError('Browser WebSocket connection error');
		};
	}

	function disconnect(): void {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		if (ws) {
			try {
				ws.close(1000);
			} catch {
				/* ignore */
			}
			ws = null;
		}
		currentSessionId = null;
	}

	function sendJson(data: Record<string, unknown>): void {
		if (ws && ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify(data));
		}
	}

	return {
		connect,
		disconnect,

		sendMouse(action: string, x: number, y: number, button: string = 'left'): void {
			sendJson({
				type: 'mouse',
				action,
				x: Math.round(x),
				y: Math.round(y),
				button,
			});
		},

		sendKey(
			action: string,
			key: string,
			code: string,
			text: string = '',
			modifiers: number = 0
		): void {
			sendJson({
				type: 'key',
				action,
				key,
				code,
				text,
				modifiers,
			});
		},

		sendScroll(x: number, y: number, deltaX: number, deltaY: number): void {
			sendJson({
				type: 'scroll',
				x: Math.round(x),
				y: Math.round(y),
				deltaX,
				deltaY,
			});
		},

		sendResize(width: number, height: number): void {
			sendJson({
				type: 'resize',
				width: Math.round(width),
				height: Math.round(height),
			});
		},

		isConnected(): boolean {
			return ws !== null && ws.readyState === WebSocket.OPEN;
		},
	};
}
