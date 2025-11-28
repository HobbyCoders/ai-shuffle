/**
 * Cross-device synchronization store.
 * Handles WebSocket connections for real-time sync and polling fallback.
 */

import { writable, derived, get } from 'svelte/store';
import { getDeviceIdSync } from './device';

export interface SyncEvent {
	event_type: string;
	session_id: string;
	data: Record<string, unknown>;
	timestamp: string | null;
	source_device_id?: string;
}

export interface SyncState {
	connected: boolean;
	sessionId: string | null;
	isRemoteStreaming: boolean;
	lastSyncId: number;
	connectedDevices: number;
	error: string | null;
}

type SyncEventHandler = (event: SyncEvent) => void;

function createSyncStore() {
	const { subscribe, set, update } = writable<SyncState>({
		connected: false,
		sessionId: null,
		isRemoteStreaming: false,
		lastSyncId: 0,
		connectedDevices: 0,
		error: null
	});

	let websocket: WebSocket | null = null;
	let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
	let pollingInterval: ReturnType<typeof setInterval> | null = null;
	let eventHandlers: SyncEventHandler[] = [];
	let authToken: string | null = null;

	/**
	 * Set the auth token for WebSocket connections
	 */
	function setAuthToken(token: string) {
		authToken = token;
	}

	/**
	 * Register an event handler for sync events
	 */
	function onEvent(handler: SyncEventHandler): () => void {
		eventHandlers.push(handler);
		return () => {
			eventHandlers = eventHandlers.filter((h) => h !== handler);
		};
	}

	/**
	 * Dispatch event to all handlers
	 */
	function dispatchEvent(event: SyncEvent) {
		eventHandlers.forEach((handler) => {
			try {
				handler(event);
			} catch (e) {
				console.error('Sync event handler error:', e);
			}
		});
	}

	/**
	 * Connect to a session via WebSocket
	 */
	async function connect(sessionId: string): Promise<boolean> {
		const deviceId = getDeviceIdSync();
		const state = get({ subscribe });

		// Already connected to this session
		if (state.connected && state.sessionId === sessionId && websocket?.readyState === WebSocket.OPEN) {
			return true;
		}

		// Disconnect from previous session
		disconnect();

		// Get auth token from cookie
		const token = authToken || getCookieToken();
		if (!token) {
			update((s) => ({ ...s, error: 'No authentication token' }));
			return false;
		}

		// Build WebSocket URL
		const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
		const wsUrl = `${protocol}://${window.location.host}/ws/sessions/${sessionId}?device_id=${deviceId}&token=${encodeURIComponent(token)}`;

		try {
			websocket = new WebSocket(wsUrl);

			websocket.onopen = () => {
				console.log(`[Sync] Connected to session ${sessionId}`);
				update((s) => ({
					...s,
					connected: true,
					sessionId,
					error: null
				}));

				// Stop polling if it was running
				stopPolling();
			};

			websocket.onmessage = (event) => {
				try {
					const syncEvent: SyncEvent = JSON.parse(event.data);
					handleSyncEvent(syncEvent);
				} catch (e) {
					console.error('[Sync] Failed to parse message:', e);
				}
			};

			websocket.onerror = (error) => {
				console.error('[Sync] WebSocket error:', error);
				update((s) => ({ ...s, error: 'WebSocket connection error' }));
			};

			websocket.onclose = (event) => {
				console.log(`[Sync] Disconnected from session ${sessionId}:`, event.code, event.reason);
				update((s) => ({ ...s, connected: false }));

				// Attempt reconnect unless intentionally closed
				if (event.code !== 1000) {
					scheduleReconnect(sessionId);
				}
			};

			// Wait for connection to be established
			return new Promise((resolve) => {
				const checkConnection = setInterval(() => {
					if (websocket?.readyState === WebSocket.OPEN) {
						clearInterval(checkConnection);
						resolve(true);
					} else if (websocket?.readyState === WebSocket.CLOSED) {
						clearInterval(checkConnection);
						resolve(false);
					}
				}, 100);

				// Timeout after 5 seconds
				setTimeout(() => {
					clearInterval(checkConnection);
					if (websocket?.readyState !== WebSocket.OPEN) {
						console.warn('[Sync] Connection timeout, falling back to polling');
						startPolling(sessionId);
						resolve(false);
					}
				}, 5000);
			});
		} catch (e) {
			console.error('[Sync] Failed to connect:', e);
			update((s) => ({ ...s, error: 'Failed to establish connection' }));
			startPolling(sessionId);
			return false;
		}
	}

	/**
	 * Handle incoming sync events
	 */
	function handleSyncEvent(event: SyncEvent) {
		const deviceId = getDeviceIdSync();

		// Ignore events from our own device
		if (event.source_device_id === deviceId) {
			return;
		}

		switch (event.event_type) {
			case 'state':
				// Initial state received on connect
				update((s) => ({
					...s,
					isRemoteStreaming: (event.data?.is_streaming as boolean) || false,
					connectedDevices: (event.data?.connected_devices as number) || 0,
					lastSyncId: (event.data?.latest_sync_id as number) || 0
				}));
				break;

			case 'stream_start':
				update((s) => ({ ...s, isRemoteStreaming: true }));
				break;

			case 'stream_end':
				update((s) => ({ ...s, isRemoteStreaming: false }));
				break;

			case 'ping':
				// Respond to ping with pong
				if (websocket?.readyState === WebSocket.OPEN) {
					websocket.send(JSON.stringify({ type: 'pong' }));
				}
				return; // Don't dispatch ping events
		}

		// Dispatch to handlers
		dispatchEvent(event);
	}

	/**
	 * Disconnect from current session
	 */
	function disconnect() {
		if (reconnectTimeout) {
			clearTimeout(reconnectTimeout);
			reconnectTimeout = null;
		}

		stopPolling();

		if (websocket) {
			websocket.close(1000, 'Client disconnect');
			websocket = null;
		}

		update((s) => ({
			...s,
			connected: false,
			sessionId: null,
			isRemoteStreaming: false,
			connectedDevices: 0
		}));
	}

	/**
	 * Schedule a reconnection attempt
	 */
	function scheduleReconnect(sessionId: string) {
		if (reconnectTimeout) {
			clearTimeout(reconnectTimeout);
		}

		reconnectTimeout = setTimeout(() => {
			console.log('[Sync] Attempting reconnect...');
			connect(sessionId);
		}, 3000);
	}

	/**
	 * Start polling fallback for when WebSocket is unavailable
	 */
	function startPolling(sessionId: string) {
		if (pollingInterval) {
			return; // Already polling
		}

		console.log('[Sync] Starting polling fallback');
		const token = authToken || getCookieToken();

		pollingInterval = setInterval(async () => {
			try {
				const state = get({ subscribe });
				const response = await fetch(
					`/api/v1/sessions/${sessionId}/sync?since_id=${state.lastSyncId}`,
					{
						headers: token ? { Authorization: `Bearer ${token}` } : {},
						credentials: 'include'
					}
				);

				if (!response.ok) {
					throw new Error(`Polling failed: ${response.status}`);
				}

				const data = await response.json();

				update((s) => ({
					...s,
					lastSyncId: data.latest_id || s.lastSyncId,
					isRemoteStreaming: data.is_streaming || false,
					connectedDevices: data.connected_devices || 0
				}));

				// Process changes
				for (const change of data.changes || []) {
					const syncEvent: SyncEvent = {
						event_type: change.event_type,
						session_id: sessionId,
						data: change.data || {},
						timestamp: change.created_at
					};
					dispatchEvent(syncEvent);
				}
			} catch (e) {
				console.error('[Sync] Polling error:', e);
			}
		}, 2000); // Poll every 2 seconds
	}

	/**
	 * Stop polling
	 */
	function stopPolling() {
		if (pollingInterval) {
			clearInterval(pollingInterval);
			pollingInterval = null;
		}
	}

	/**
	 * Request current state from server
	 */
	function requestState() {
		if (websocket?.readyState === WebSocket.OPEN) {
			websocket.send(JSON.stringify({ type: 'request_state' }));
		}
	}

	/**
	 * Get auth token from cookie
	 */
	function getCookieToken(): string | null {
		if (typeof document === 'undefined') return null;
		const match = document.cookie.match(/session_token=([^;]+)/);
		return match ? match[1] : null;
	}

	return {
		subscribe,
		setAuthToken,
		connect,
		disconnect,
		onEvent,
		requestState
	};
}

export const sync = createSyncStore();

// Derived stores for convenience
export const isConnected = derived(sync, ($sync) => $sync.connected);
export const isRemoteStreaming = derived(sync, ($sync) => $sync.isRemoteStreaming);
export const connectedDevices = derived(sync, ($sync) => $sync.connectedDevices);
export const syncError = derived(sync, ($sync) => $sync.error);
