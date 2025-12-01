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
}

// Load persisted values from localStorage
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
		wsConnected: false
	});

	let ws: WebSocket | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let pingTimer: ReturnType<typeof setInterval> | null = null;

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
	function connect() {
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
			return;
		}

		const token = getAuthToken();
		if (!token) {
			console.error('[Chat] No auth token available');
			update(s => ({ ...s, error: 'Not authenticated' }));
			return;
		}

		const url = `${getWsUrl()}?token=${encodeURIComponent(token)}`;
		console.log('[Chat] Connecting to WebSocket...');

		ws = new WebSocket(url);

		ws.onopen = () => {
			console.log('[Chat] WebSocket connected');
			update(s => ({ ...s, wsConnected: true, error: null }));

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
			update(s => ({ ...s, wsConnected: false }));

			if (pingTimer) {
				clearInterval(pingTimer);
				pingTimer = null;
			}

			// Reconnect after delay (unless intentionally closed)
			if (event.code !== 1000) {
				if (reconnectTimer) clearTimeout(reconnectTimer);
				reconnectTimer = setTimeout(() => {
					console.log('[Chat] Attempting reconnect...');
					connect();
				}, 3000);
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
	 * Handle incoming WebSocket message
	 */
	function handleMessage(data: Record<string, unknown>) {
		const msgType = data.type as string;
		// console.log('[Chat] Received:', msgType, data);

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

				update(s => ({
					...s,
					sessionId: data.session_id as string,
					messages
				}));
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
					const messages = [...s.messages];

					// Mark current streaming text as not streaming (but keep content)
					const streamingIdx = messages.findLastIndex(
						m => m.type === 'text' && m.role === 'assistant' && m.streaming
					);
					if (streamingIdx !== -1 && messages[streamingIdx].content) {
						messages[streamingIdx] = {
							...messages[streamingIdx],
							streaming: false
						};
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
						sessionId: data.session_id as string || s.sessionId
					};
				});

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

					return { ...s, messages, isStreaming: false };
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
						error: data.message as string
					};
				});
				break;
			}

			case 'queued': {
				// Message was queued to active session
				// The user message was already added in sendQuery, just log it
				console.log('[Chat] Message queued for processing');
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
	 */
	async function loadSessionsInternal() {
		try {
			const sessions = await api.get<Session[]>('/sessions?limit=50');
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
			update(s => ({ ...s, selectedProfile: profileId }));
		},

		setProject(projectId: string) {
			if (typeof window !== 'undefined') {
				localStorage.setItem('aihub_selectedProject', projectId);
			}
			update(s => ({ ...s, selectedProject: projectId }));
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
