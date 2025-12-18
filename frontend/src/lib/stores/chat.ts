/**
 * Simplified Chat Store with WebSocket-first Architecture
 *
 * Core principle: The WebSocket connection IS the streaming channel.
 * No sync engine, no background tasks, no event filtering complexity.
 *
 * Flow:
 * 1. User sends message via WebSocket
 * 2. Backend streams response directly through same WebSocket
 * 3. Frontend displays each chunk immediately
 * 4. Single isStreaming state tracks everything
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile } from '$lib/api/client';
import { api } from '$lib/api/client';
import { isNotificationsEnabled, notifySessionComplete } from '$lib/services/notifications';

export type MessageType = 'text' | 'tool_use' | 'tool_result';

export interface ChatMessage {
	id: string;
	role: 'user' | 'assistant' | 'system';
	content: string;
	type?: MessageType;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	streaming?: boolean;
}

export interface Project {
	id: string;
	name: string;
	description?: string;
	path: string;
	settings: Record<string, unknown>;
	created_at: string;
	updated_at: string;
}

interface ChatState {
	sessionId: string | null;
	messages: ChatMessage[];
	profiles: Profile[];
	selectedProfile: string;
	projects: Project[];
	selectedProject: string;
	sessions: Session[];
	isStreaming: boolean;
	error: string | null;
	wsConnected: boolean;
	connectionState: 'connecting' | 'connected' | 'reconnecting' | 'disconnected';
	deviceId: string;
	connectedDevices: number;
	streamStartTime: number | null; // Track when streaming started for notifications
}

// Load persisted values from localStorage - empty string means no selection
// Note: These are also synced to backend for cross-device persistence
function getPersistedProfile(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || '';
	}
	return '';
}

function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
}

// Sync selected profile/project to backend for cross-device persistence
let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;
const SELECTION_SYNC_DEBOUNCE_MS = 300;

// Track pending selection for flush on unload
let pendingSelection: { profile: string; project: string } | null = null;

/**
 * Immediately flush any pending selection save to backend (sync, for beforeunload)
 */
function flushPendingSelectionSave() {
	if (!pendingSelection) return;

	// Cancel the debounce timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
		selectionSyncTimeout = null;
	}

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({
		key: 'selection',
		value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
	});
	navigator.sendBeacon('/api/v1/preferences/selection', new Blob([data], { type: 'application/json' }));

	pendingSelection = null;
}

// Register beforeunload handler to flush pending saves when browser closes
if (typeof window !== 'undefined') {
	window.addEventListener('beforeunload', flushPendingSelectionSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingSelectionSave();
		}
	});
}

async function syncSelectionToBackend(profile: string, project: string) {
	// Store pending selection for flush on unload
	pendingSelection = { profile, project };

	// Clear existing timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
	}

	// Debounce the sync to avoid excessive API calls
	selectionSyncTimeout = setTimeout(async () => {
		if (!pendingSelection) return;

		try {
			await fetch('/api/v1/preferences/selection', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					key: 'selection',
					value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
				})
			});
		} catch (e) {
			console.error('[Chat] Failed to sync selection to backend:', e);
		}

		pendingSelection = null;
		selectionSyncTimeout = null;
	}, SELECTION_SYNC_DEBOUNCE_MS);
}

// Load selection from backend (called on init)
async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
	if (typeof window === 'undefined') return null;

	try {
		const response = await fetch('/api/v1/preferences/selection', {
			credentials: 'include'
		});

		if (response.ok) {
			const data = await response.json();
			if (data && data.value) {
				return {
					selectedProfile: data.value.selectedProfile || '',
					selectedProject: data.value.selectedProject || ''
				};
			}
		}
	} catch (e) {
		console.error('[Chat] Failed to load selection from backend:', e);
	}

	return null;
}

/**
 * Get or create persistent device ID
 */
function getOrCreateDeviceId(): string {
	const key = 'ai-hub-device-id';
	if (typeof window === 'undefined') {
		return 'server-' + Math.random().toString(36).substr(2, 9);
	}
	let deviceId = localStorage.getItem(key);
	if (!deviceId) {
		// crypto.randomUUID() requires secure context (HTTPS), fallback for HTTP
		if (typeof crypto !== 'undefined' && crypto.randomUUID) {
			deviceId = crypto.randomUUID();
		} else {
			// Fallback: generate a UUID-like string
			deviceId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
				const r = (Math.random() * 16) | 0;
				const v = c === 'x' ? r : (r & 0x3) | 0x8;
				return v.toString(16);
			});
		}
		localStorage.setItem(key, deviceId);
	}
	return deviceId;
}

function createChatStore() {
	const { subscribe, set, update } = writable<ChatState>({
		sessionId: null,
		messages: [],
		profiles: [],
		selectedProfile: getPersistedProfile(),
		projects: [],
		selectedProject: getPersistedProject(),
		sessions: [],
		isStreaming: false,
		error: null,
		wsConnected: false,
		connectionState: 'disconnected',
		deviceId: getOrCreateDeviceId(),
		connectedDevices: 0,
		streamStartTime: null
	});

	let ws: WebSocket | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let pingTimer: ReturnType<typeof setInterval> | null = null;
	let reconnectAttempts = 0;
	const maxReconnectDelay = 30000;

	/**
	 * Calculate exponential backoff delay with jitter
	 */
	function getReconnectDelay(): number {
		const baseDelay = Math.min(1000 * Math.pow(2, reconnectAttempts), maxReconnectDelay);
		const jitter = Math.random() * 1000;
		return baseDelay + jitter;
	}

	/**
	 * Get WebSocket URL for chat
	 */
	function getWsUrl(): string {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		return `${protocol}//${host}/api/v1/ws/chat`;
	}

	/**
	 * Get auth token from cookie
	 */
	function getAuthToken(): string | null {
		const cookies = document.cookie.split(';');
		for (const cookie of cookies) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session_token') {
				return value;
			}
		}
		return null;
	}

	/**
	 * Connect to WebSocket
	 */
	function connect(isReconnect = false) {
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
			return;
		}

		const token = getAuthToken();
		if (!token) {
			console.error('[Chat] No auth token available');
			update(s => ({ ...s, error: 'Not authenticated', connectionState: 'disconnected' }));
			return;
		}

		const deviceId = getOrCreateDeviceId();
		const url = `${getWsUrl()}?token=${encodeURIComponent(token)}&device_id=${encodeURIComponent(deviceId)}`;

		if (isReconnect) {
			console.log(`[Chat] Reconnecting (attempt ${reconnectAttempts + 1})...`);
			update(s => ({ ...s, connectionState: 'reconnecting' }));
		} else {
			console.log('[Chat] Connecting to WebSocket...');
			update(s => ({ ...s, connectionState: 'connecting' }));
		}

		ws = new WebSocket(url);

		ws.onopen = () => {
			console.log('[Chat] WebSocket connected');
			reconnectAttempts = 0; // Reset on successful connection
			update(s => ({ ...s, wsConnected: true, connectionState: 'connected', error: null }));

			// If reconnecting and we have a current session, reload it to get latest state
			const state = get({ subscribe });
			if (isReconnect && state.sessionId) {
				console.log('[Chat] Reloading session after reconnect:', state.sessionId);
				loadSession(state.sessionId);
			}

			// Start ping timer
			if (pingTimer) clearInterval(pingTimer);
			pingTimer = setInterval(() => {
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
			}, 25000);
		};

		ws.onclose = (event) => {
			console.log('[Chat] WebSocket closed:', event.code, event.reason);
			update(s => ({ ...s, wsConnected: false, connectionState: 'disconnected' }));

			if (pingTimer) {
				clearInterval(pingTimer);
				pingTimer = null;
			}

			// Reconnect after delay with exponential backoff (unless intentionally closed)
			if (event.code !== 1000) {
				if (reconnectTimer) clearTimeout(reconnectTimer);
				const delay = getReconnectDelay();
				console.log(`[Chat] Scheduling reconnect in ${Math.round(delay / 1000)}s...`);
				reconnectAttempts++;
				reconnectTimer = setTimeout(() => {
					connect(true);
				}, delay);
			}
		};

		ws.onerror = (error) => {
			console.error('[Chat] WebSocket error:', error);
		};

		ws.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data);
				handleMessage(data);
			} catch (e) {
				console.error('[Chat] Failed to parse message:', e);
			}
		};
	}

	/**
	 * Disconnect WebSocket
	 */
	function disconnect() {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		if (pingTimer) {
			clearInterval(pingTimer);
			pingTimer = null;
		}
		if (ws) {
			ws.close(1000);
			ws = null;
		}
		update(s => ({ ...s, wsConnected: false }));
	}

	/**
	 * Handle sync events from other devices
	 */
	function handleSyncEvent(data: Record<string, unknown>) {
		const eventType = data.event_type as string;
		const sessionId = data.session_id as string;
		const eventData = data.data as Record<string, unknown>;

		console.log('[Chat] Sync event:', eventType, eventData);

		// Only process events for the current session
		const state = get({ subscribe });
		if (state.sessionId !== sessionId) {
			console.log('[Chat] Ignoring sync event for different session:', sessionId);
			return;
		}

		switch (eventType) {
			case 'stream_start': {
				// Another device started streaming
				console.log('[Chat] Another device started streaming');
				const messageId = eventData.message_id as string;

				update(s => ({
					...s,
					isStreaming: true,
					messages: [...s.messages, {
						id: messageId || `assistant-${Date.now()}`,
						role: 'assistant',
						content: '',
						type: 'text',
						streaming: true
					}]
				}));
				break;
			}

			case 'stream_chunk': {
				// Content chunk from another device
				const chunkType = eventData.chunk_type as string;
				const content = eventData.content as string;

				if (chunkType === 'text') {
					// Append text to last streaming message
					update(s => {
						const messages = [...s.messages];
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);

						if (streamingIdx !== -1) {
							messages[streamingIdx] = {
								...messages[streamingIdx],
								content: messages[streamingIdx].content + (content || '')
							};
						} else {
							// No streaming message found, create one
							messages.push({
								id: `text-sync-${Date.now()}`,
								role: 'assistant',
								content: content || '',
								type: 'text',
								streaming: true
							});
						}

						return { ...s, messages };
					});
				} else if (chunkType === 'tool_use') {
					// Tool use event
					update(s => {
						let messages = [...s.messages];

						// Handle current streaming text message
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);
						if (streamingIdx !== -1) {
							if (messages[streamingIdx].content) {
								messages[streamingIdx] = {
									...messages[streamingIdx],
									streaming: false
								};
							} else {
								messages = messages.filter((_, i) => i !== streamingIdx);
							}
						}

						// Add tool use message
						messages.push({
							id: `tool-sync-${Date.now()}-${eventData.tool_id || ''}`,
							role: 'assistant',
							content: '',
							type: 'tool_use',
							toolName: eventData.tool_name as string,
							toolId: eventData.tool_id as string,
							toolInput: eventData.tool_input as Record<string, unknown>,
							streaming: true
						});

						return { ...s, messages };
					});
				} else if (chunkType === 'tool_result') {
					// Tool result event
					update(s => {
						const messages = [...s.messages];

						// Mark the matching tool_use as complete
						const toolUseId = eventData.tool_use_id as string;
						const toolUseIdx = messages.findLastIndex(
							m => m.type === 'tool_use' && (m.toolId === toolUseId || m.streaming)
						);
						if (toolUseIdx !== -1) {
							messages[toolUseIdx] = {
								...messages[toolUseIdx],
								streaming: false
							};
						}

						// Add tool result message
						messages.push({
							id: `result-sync-${Date.now()}`,
							role: 'assistant',
							content: eventData.output as string,
							type: 'tool_result',
							toolName: eventData.tool_name as string,
							toolId: toolUseId,
							streaming: false
						});

						// Add new text placeholder for continuation
						messages.push({
							id: `text-sync-${Date.now()}-cont`,
							role: 'assistant',
							content: '',
							type: 'text',
							streaming: true
						});

						return { ...s, messages };
					});
				}
				break;
			}

			case 'stream_end': {
				// Another device finished streaming
				console.log('[Chat] Another device finished streaming');
				const metadata = eventData.metadata as Record<string, unknown>;
				const interrupted = eventData.interrupted as boolean;

				update(s => {
					// Mark all streaming messages as complete
					let messages = s.messages.map(m =>
						m.streaming ? { ...m, streaming: false } : m
					);

					// Remove empty text messages
					messages = messages.filter(
						m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
					);

					// Add metadata to last assistant message
					if (messages.length > 0) {
						const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
						if (lastIdx !== -1) {
							messages[lastIdx] = {
								...messages[lastIdx],
								metadata,
								content: interrupted
									? messages[lastIdx].content + '\n\n[Stopped]'
									: messages[lastIdx].content
							};
						}
					}

					return { ...s, messages, isStreaming: false };
				});

				// Refresh sessions list
				loadSessionsInternal();
				break;
			}
		}
	}

	/**
	 * Handle incoming WebSocket message
	 */
	function handleMessage(data: Record<string, unknown>) {
		const msgType = data.type as string;
		const eventType = data.event_type as string;
		// console.log('[Chat] Received:', msgType || eventType, data);

		// Handle sync events from other devices
		if (eventType) {
			handleSyncEvent(data);
			return;
		}

		switch (msgType) {
			case 'history': {
				// Session history loaded - handle both JSONL format and legacy DB format
				const messages = (data.messages as Array<Record<string, unknown>>)?.map((m, i) => {
					// Determine message type - JSONL messages have explicit 'type' field
					let msgType: MessageType | undefined = m.type as MessageType | undefined;

					// Legacy DB format: infer type from role
					if (!msgType && m.role === 'assistant') {
						msgType = 'text';
					} else if (!msgType && (m.role === 'tool_use' || m.role === 'tool_result')) {
						msgType = m.role as MessageType;
					}

					return {
						id: String(m.id || `msg-${i}`),
						role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
						content: m.content as string,
						type: msgType,
						toolName: (m.toolName || m.tool_name) as string | undefined,
						toolId: m.toolId as string | undefined,
						toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
						metadata: m.metadata as Record<string, unknown>,
						streaming: false
					};
				}) || [];

				// Handle late-joining to streaming session
				const isStreaming = data.isStreaming as boolean;
				const streamingBuffer = data.streamingBuffer as Array<Record<string, unknown>> | undefined;

				if (isStreaming && streamingBuffer && streamingBuffer.length > 0) {
					console.log('[Chat] Late-joining streaming session, merging buffer:', streamingBuffer.length, 'messages');
					const bufferMessages: ChatMessage[] = streamingBuffer.map((m) => ({
						id: `buffer-${Date.now()}-${Math.random()}`,
						role: 'assistant',
						content: (m.content as string) || '',
						type: (m.type || m.chunk_type) as MessageType,
						toolName: m.tool_name as string | undefined,
						toolId: m.tool_id as string | undefined,
						toolInput: m.tool_input as Record<string, unknown> | undefined,
						streaming: m.streaming as boolean || true
					}));

					update(s => ({
						...s,
						sessionId: data.session_id as string,
						messages: [...messages, ...bufferMessages],
						isStreaming: true
					}));
				} else {
					update(s => ({
						...s,
						sessionId: data.session_id as string,
						messages,
						isStreaming: isStreaming || false
					}));
				}
				break;
			}

			case 'start': {
				// Query started, streaming begins
				const sessionId = data.session_id as string;
				const assistantMsgId = `assistant-${Date.now()}`;

				update(s => ({
					...s,
					sessionId,
					isStreaming: true,
					streamStartTime: Date.now(),
					messages: [...s.messages, {
						id: assistantMsgId,
						role: 'assistant',
						content: '',
						type: 'text',
						streaming: true
					}]
				}));
				break;
			}

			case 'chunk': {
				// Text content chunk
				update(s => {
					const messages = [...s.messages];

					// Find the last streaming text message
					const streamingIdx = messages.findLastIndex(
						m => m.type === 'text' && m.role === 'assistant' && m.streaming
					);

					if (streamingIdx !== -1) {
						messages[streamingIdx] = {
							...messages[streamingIdx],
							content: messages[streamingIdx].content + (data.content as string)
						};
					} else {
						// No streaming message found, create one
						messages.push({
							id: `text-${Date.now()}`,
							role: 'assistant',
							content: data.content as string,
							type: 'text',
							streaming: true
						});
					}

					return { ...s, messages };
				});
				break;
			}

			case 'tool_use': {
				// Tool being used
				update(s => {
					let messages = [...s.messages];

					// Handle current streaming text message
					const streamingIdx = messages.findLastIndex(
						m => m.type === 'text' && m.role === 'assistant' && m.streaming
					);
					if (streamingIdx !== -1) {
						if (messages[streamingIdx].content) {
							// Mark text message as complete if it has content
							messages[streamingIdx] = {
								...messages[streamingIdx],
								streaming: false
							};
						} else {
							// Remove empty streaming text messages to avoid leaving empty cards
							messages = messages.filter((_, i) => i !== streamingIdx);
						}
					}

					// Add tool use message
					messages.push({
						id: `tool-${Date.now()}-${data.id || ''}`,
						role: 'assistant',
						content: '',
						type: 'tool_use',
						toolName: data.name as string,
						toolId: data.id as string,
						toolInput: data.input as Record<string, unknown>,
						streaming: true
					});

					return { ...s, messages };
				});
				break;
			}

			case 'tool_result': {
				// Tool result
				update(s => {
					const messages = [...s.messages];

					// Mark the matching tool_use as complete
					const toolUseId = data.tool_use_id as string;
					const toolUseIdx = messages.findLastIndex(
						m => m.type === 'tool_use' && (m.toolId === toolUseId || m.streaming)
					);
					if (toolUseIdx !== -1) {
						messages[toolUseIdx] = {
							...messages[toolUseIdx],
							streaming: false
						};
					}

					// Add tool result message
					messages.push({
						id: `result-${Date.now()}`,
						role: 'assistant',
						content: data.output as string,
						type: 'tool_result',
						toolName: data.name as string,
						toolId: toolUseId,
						streaming: false
					});

					// Add new text placeholder for continuation
					messages.push({
						id: `text-${Date.now()}-cont`,
						role: 'assistant',
						content: '',
						type: 'text',
						streaming: true
					});

					return { ...s, messages };
				});
				break;
			}

			case 'done': {
				// Query complete
				const metadata = data.metadata as Record<string, unknown>;
				const currentState = get({ subscribe });

				update(s => {
					// Mark all streaming messages as complete
					let messages = s.messages.map(m =>
						m.streaming ? { ...m, streaming: false } : m
					);

					// Remove empty text messages
					messages = messages.filter(
						m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
					);

					// Add metadata to last assistant message
					if (messages.length > 0) {
						const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
						if (lastIdx !== -1) {
							messages[lastIdx] = {
								...messages[lastIdx],
								metadata
							};
						}
					}

					return {
						...s,
						messages,
						isStreaming: false,
						streamStartTime: null,
						sessionId: data.session_id as string || s.sessionId
					};
				});

				// Trigger browser notification if enabled and session took > 10 seconds
				if (isNotificationsEnabled() && currentState.streamStartTime) {
					const duration = (Date.now() - currentState.streamStartTime) / 1000;
					if (duration > 10) {
						// Find session title from sessions list
						const session = currentState.sessions.find(s => s.id === currentState.sessionId);
						notifySessionComplete(
							session?.title || null,
							currentState.sessionId || '',
							duration
						);
					}
				}

				// Refresh sessions list
				loadSessionsInternal();
				break;
			}

			case 'stopped': {
				// Query was stopped/interrupted
				update(s => {
					let messages = s.messages.map(m =>
						m.streaming ? { ...m, streaming: false } : m
					);

					messages = messages.filter(
						m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
					);

					// Add interrupted notice to last message
					if (messages.length > 0) {
						const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
						if (lastIdx !== -1) {
							messages[lastIdx] = {
								...messages[lastIdx],
								content: messages[lastIdx].content + '\n\n[Stopped]'
							};
						}
					}

					return { ...s, messages, isStreaming: false, streamStartTime: null };
				});
				break;
			}

			case 'error': {
				// Error occurred
				update(s => {
					let messages = s.messages.map(m =>
						m.streaming ? { ...m, streaming: false } : m
					);

					messages = messages.filter(
						m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
					);

					return {
						...s,
						messages,
						isStreaming: false,
						streamStartTime: null,
						error: data.message as string
					};
				});
				break;
			}

			case 'ping': {
				// Respond to ping
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				break;
			}
		}
	}

	/**
	 * Send a query via WebSocket
	 */
	function sendQuery(prompt: string) {
		const state = get({ subscribe });

		if (!ws || ws.readyState !== WebSocket.OPEN) {
			update(s => ({ ...s, error: 'Not connected' }));
			return;
		}

		// Add user message immediately
		const userMsgId = `user-${Date.now()}`;
		update(s => ({
			...s,
			messages: [...s.messages, {
				id: userMsgId,
				role: 'user',
				content: prompt
			}],
			isStreaming: true,
			error: null
		}));

		// Send query via WebSocket
		ws.send(JSON.stringify({
			type: 'query',
			prompt,
			session_id: state.sessionId,
			profile: state.selectedProfile,
			project: state.selectedProject || undefined
		}));
	}

	/**
	 * Stop current generation
	 */
	function stopGeneration() {
		const state = get({ subscribe });

		if (ws?.readyState === WebSocket.OPEN && state.sessionId) {
			ws.send(JSON.stringify({
				type: 'stop',
				session_id: state.sessionId
			}));
		}

		// Optimistically update UI
		update(s => ({ ...s, isStreaming: false }));
	}

	/**
	 * Load a specific session
	 */
	async function loadSession(sessionId: string) {
		if (ws?.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({
				type: 'load_session',
				session_id: sessionId
			}));
		}
	}

	/**
	 * Internal function to load sessions list
	 * For admins: loads sessions where api_user_id IS NULL (their own chats)
	 * For API users: the backend automatically filters to their sessions
	 */
	async function loadSessionsInternal() {
		try {
			const sessions = await api.get<Session[]>('/sessions?limit=50&admin_only=true');
			update(s => ({ ...s, sessions }));
		} catch (e) {
			console.error('Failed to load sessions:', e);
		}
	}

	return {
		subscribe,

		/**
		 * Initialize the store and connect WebSocket
		 */
		init() {
			connect();
			// Load selection from backend and sync to localStorage
			loadSelectionFromBackend().then((backendSelection) => {
				if (backendSelection) {
					// Backend has data - use it as authoritative source
					if (backendSelection.selectedProfile) {
						localStorage.setItem('aihub_selectedProfile', backendSelection.selectedProfile);
						update(s => ({ ...s, selectedProfile: backendSelection.selectedProfile }));
					}
					if (backendSelection.selectedProject) {
						localStorage.setItem('aihub_selectedProject', backendSelection.selectedProject);
						update(s => ({ ...s, selectedProject: backendSelection.selectedProject }));
					}
				} else {
					// No backend data - sync localStorage to backend
					const profile = getPersistedProfile();
					const project = getPersistedProject();
					if (profile || project) {
						syncSelectionToBackend(profile, project);
					}
				}
			});
		},

		/**
		 * Cleanup on destroy
		 */
		destroy() {
			disconnect();
		},

		async loadProfiles() {
			try {
				const profiles = await api.get<Profile[]>('/profiles');
				update(s => ({ ...s, profiles }));
			} catch (e) {
				console.error('Failed to load profiles:', e);
			}
		},

		async loadProjects() {
			try {
				const projects = await api.get<Project[]>('/projects');
				update(s => ({ ...s, projects }));
			} catch (e) {
				console.error('Failed to load projects:', e);
			}
		},

		async loadSessions() {
			await loadSessionsInternal();
		},

		async loadSession(sessionId: string) {
			// First load from REST API for reliable data
			try {
				const session = await api.get<Session & { messages: Array<Record<string, unknown>> }>(`/sessions/${sessionId}`);
				const messages: ChatMessage[] = session.messages.map((m, i) => ({
					id: `msg-${m.id || i}`,
					role: m.role as 'user' | 'assistant',
					content: m.content as string,
					type: m.role === 'assistant' ? 'text' as const : undefined,
					metadata: m.metadata as Record<string, unknown>,
					streaming: false
				}));

				update(s => ({
					...s,
					sessionId: session.id,
					messages,
					error: null
				}));

				return true;
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to load session' }));
				return false;
			}
		},

		setProfile(profileId: string) {
			if (typeof window !== 'undefined') {
				localStorage.setItem('aihub_selectedProfile', profileId);
			}
			update(s => {
				// Sync to backend for persistence
				syncSelectionToBackend(profileId, s.selectedProject);
				return { ...s, selectedProfile: profileId };
			});
		},

		setProject(projectId: string) {
			if (typeof window !== 'undefined') {
				localStorage.setItem('aihub_selectedProject', projectId);
			}
			update(s => {
				// Sync to backend for persistence
				syncSelectionToBackend(s.selectedProfile, projectId);
				return { ...s, selectedProject: projectId };
			});
		},

		startNewChat() {
			update(s => ({
				...s,
				sessionId: null,
				messages: [],
				isStreaming: false,
				error: null
			}));
		},

		async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
			try {
				await api.post('/profiles', data);
				await this.loadProfiles();
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to create profile' }));
			}
		},

		async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
			try {
				await api.put(`/profiles/${profileId}`, data);
				await this.loadProfiles();
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to update profile' }));
			}
		},

		async deleteProfile(profileId: string) {
			try {
				await api.delete(`/profiles/${profileId}`);
				await this.loadProfiles();
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to delete profile' }));
			}
		},

		async createProject(data: { id: string; name: string; description?: string }) {
			try {
				await api.post('/projects', data);
				await this.loadProjects();
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to create project' }));
			}
		},

		async deleteProject(projectId: string) {
			try {
				await api.delete(`/projects/${projectId}`);
				await this.loadProjects();
				update(s => ({
					...s,
					selectedProject: s.selectedProject === projectId ? '' : s.selectedProject
				}));
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to delete project' }));
			}
		},

		async deleteSession(sessionId: string) {
			try {
				await api.delete(`/sessions/${sessionId}`);
				await this.loadSessions();
				// If we deleted the current session, start a new chat
				update(s => {
					if (s.sessionId === sessionId) {
						return { ...s, sessionId: null, messages: [] };
					}
					return s;
				});
			} catch (e: unknown) {
				const error = e as { detail?: string };
				update(s => ({ ...s, error: error.detail || 'Failed to delete session' }));
			}
		},

		sendMessage(prompt: string) {
			sendQuery(prompt);
		},

		stopGeneration() {
			stopGeneration();
		},

		clearError() {
			update(s => ({ ...s, error: null }));
		}
	};
}

export const chat = createChatStore();

// Derived stores for convenience
export const messages = derived(chat, $chat => $chat.messages);
export const isStreaming = derived(chat, $chat => $chat.isStreaming);
export const chatError = derived(chat, $chat => $chat.error);
export const profiles = derived(chat, $chat => $chat.profiles);
export const selectedProfile = derived(chat, $chat => $chat.selectedProfile);
export const projects = derived(chat, $chat => $chat.projects);
export const selectedProject = derived(chat, $chat => $chat.selectedProject);
export const sessions = derived(chat, $chat => $chat.sessions);
export const currentSessionId = derived(chat, $chat => $chat.sessionId);
export const wsConnected = derived(chat, $chat => $chat.wsConnected);
export const connectionState = derived(chat, $chat => $chat.connectionState);
export const deviceId = derived(chat, $chat => $chat.deviceId);
export const connectedDevices = derived(chat, $chat => $chat.connectedDevices);
