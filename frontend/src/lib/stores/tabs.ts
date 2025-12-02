/**
 * Tabbed Chat Store - Supports multiple concurrent chat sessions
 *
 * Each tab has its own WebSocket connection and independent state.
 * Tabs can run queries simultaneously without interfering with each other.
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile } from '$lib/api/client';
import { api } from '$lib/api/client';

export interface ApiUser {
	id: string;
	name: string;
	description?: string;
	project_id?: string;
	profile_id?: string;
	is_active: boolean;
	created_at: string;
	updated_at: string;
	last_used_at?: string;
}

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

export interface ChatTab {
	id: string;
	title: string;
	sessionId: string | null;
	messages: ChatMessage[];
	isStreaming: boolean;
	wsConnected: boolean;
	error: string | null;
	profile: string;
	project: string;
}

interface TabsState {
	tabs: ChatTab[];
	activeTabId: string | null;
	profiles: Profile[];
	projects: Project[];
	sessions: Session[];
	adminSessions: Session[];
	apiUsers: ApiUser[];
	adminSessionsFilter: string | null; // null = all, '' = admin only, 'user_id' = specific user
	defaultProfile: string;
	defaultProject: string;
	// Selection state for batch operations
	selectedSessionIds: Set<string>;
	selectedAdminSessionIds: Set<string>;
	selectionMode: boolean;
	adminSelectionMode: boolean;
	// Loading state to prevent race conditions
	sessionsLoading: boolean;
}

// WebSocket connections per tab
const tabConnections: Map<string, WebSocket> = new Map();
const tabPingTimers: Map<string, ReturnType<typeof setInterval>> = new Map();
const tabReconnectTimers: Map<string, ReturnType<typeof setTimeout>> = new Map();

// Load persisted values
function getPersistedProfile(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || 'claude-code';
	}
	return 'claude-code';
}

function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
}

function generateTabId(): string {
	return `tab-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function createTabsStore() {
	const initialTabId = generateTabId();

	const { subscribe, set, update } = writable<TabsState>({
		tabs: [{
			id: initialTabId,
			title: 'New Chat',
			sessionId: null,
			messages: [],
			isStreaming: false,
			wsConnected: false,
			error: null,
			profile: getPersistedProfile(),
			project: getPersistedProject()
		}],
		activeTabId: initialTabId,
		profiles: [],
		projects: [],
		sessions: [],
		adminSessions: [],
		apiUsers: [],
		adminSessionsFilter: null,
		defaultProfile: getPersistedProfile(),
		defaultProject: getPersistedProject(),
		// Initialize selection state
		selectedSessionIds: new Set<string>(),
		selectedAdminSessionIds: new Set<string>(),
		selectionMode: false,
		adminSelectionMode: false,
		// Initialize loading state
		sessionsLoading: false
	});

	/**
	 * Get WebSocket URL
	 */
	function getWsUrl(): string {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		return `${protocol}//${host}/api/v1/ws/chat`;
	}

	/**
	 * Get auth token from cookie (may return null for httpOnly cookies)
	 */
	function getAuthToken(): string | null {
		const cookies = document.cookie.split(';');
		for (const cookie of cookies) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') {
				return value;
			}
		}
		// Cookie might be httpOnly, return null and let server check cookie directly
		return null;
	}

	/**
	 * Update a specific tab's state
	 */
	function updateTab(tabId: string, updates: Partial<ChatTab>) {
		update(s => ({
			...s,
			tabs: s.tabs.map(tab =>
				tab.id === tabId ? { ...tab, ...updates } : tab
			)
		}));
	}

	/**
	 * Get tab by ID
	 */
	function getTab(tabId: string): ChatTab | undefined {
		const state = get({ subscribe });
		return state.tabs.find(t => t.id === tabId);
	}

	/**
	 * Connect WebSocket for a specific tab
	 */
	function connectTab(tabId: string) {
		const existingWs = tabConnections.get(tabId);
		if (existingWs && (existingWs.readyState === WebSocket.OPEN || existingWs.readyState === WebSocket.CONNECTING)) {
			return;
		}

		// Try to get token from cookie (may be null if httpOnly)
		// Server will also check cookie directly for httpOnly cookies
		const token = getAuthToken();
		let url = getWsUrl();
		if (token) {
			url = `${url}?token=${encodeURIComponent(token)}`;
		}
		console.log(`[Tab ${tabId}] Connecting to WebSocket...`);

		const ws = new WebSocket(url);
		tabConnections.set(tabId, ws);

		ws.onopen = () => {
			console.log(`[Tab ${tabId}] WebSocket connected`);
			updateTab(tabId, { wsConnected: true, error: null });

			// Start ping timer
			const existingPing = tabPingTimers.get(tabId);
			if (existingPing) clearInterval(existingPing);

			const pingTimer = setInterval(() => {
				const currentWs = tabConnections.get(tabId);
				if (currentWs?.readyState === WebSocket.OPEN) {
					currentWs.send(JSON.stringify({ type: 'pong' }));
				}
			}, 25000);
			tabPingTimers.set(tabId, pingTimer);
		};

		ws.onclose = (event) => {
			console.log(`[Tab ${tabId}] WebSocket closed:`, event.code, event.reason);
			updateTab(tabId, { wsConnected: false });

			const pingTimer = tabPingTimers.get(tabId);
			if (pingTimer) {
				clearInterval(pingTimer);
				tabPingTimers.delete(tabId);
			}

			// Reconnect after delay (unless intentionally closed or tab removed)
			if (event.code !== 1000) {
				const existingReconnect = tabReconnectTimers.get(tabId);
				if (existingReconnect) clearTimeout(existingReconnect);

				const reconnectTimer = setTimeout(() => {
					// Check if tab still exists
					const tab = getTab(tabId);
					if (tab) {
						console.log(`[Tab ${tabId}] Attempting reconnect...`);
						connectTab(tabId);
					}
				}, 3000);
				tabReconnectTimers.set(tabId, reconnectTimer);
			}
		};

		ws.onerror = (error) => {
			console.error(`[Tab ${tabId}] WebSocket error:`, error);
		};

		ws.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data);
				handleTabMessage(tabId, data);
			} catch (e) {
				console.error(`[Tab ${tabId}] Failed to parse message:`, e);
			}
		};
	}

	/**
	 * Disconnect WebSocket for a specific tab
	 */
	function disconnectTab(tabId: string) {
		const reconnectTimer = tabReconnectTimers.get(tabId);
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			tabReconnectTimers.delete(tabId);
		}

		const pingTimer = tabPingTimers.get(tabId);
		if (pingTimer) {
			clearInterval(pingTimer);
			tabPingTimers.delete(tabId);
		}

		const ws = tabConnections.get(tabId);
		if (ws) {
			ws.close(1000);
			tabConnections.delete(tabId);
		}

		updateTab(tabId, { wsConnected: false });
	}

	/**
	 * Handle incoming WebSocket message for a tab
	 */
	function handleTabMessage(tabId: string, data: Record<string, unknown>) {
		const msgType = data.type as string;

		switch (msgType) {
			case 'history': {
				// Handle both JSONL format (with explicit type) and legacy DB format
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

				updateTab(tabId, {
					sessionId: data.session_id as string,
					messages
				});

				// Update tab title based on first message
				if (messages.length > 0 && messages[0].role === 'user') {
					const title = messages[0].content.substring(0, 30) + (messages[0].content.length > 30 ? '...' : '');
					updateTab(tabId, { title });
				}
				break;
			}

			case 'start': {
				const sessionId = data.session_id as string;
				const assistantMsgId = `assistant-${Date.now()}`;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							sessionId,
							isStreaming: true,
							messages: [...tab.messages, {
								id: assistantMsgId,
								role: 'assistant' as const,
								content: '',
								type: 'text' as const,
								streaming: true
							}]
						};
					})
				}));
				break;
			}

			case 'chunk': {
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = [...tab.messages];
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);

						if (streamingIdx !== -1) {
							messages[streamingIdx] = {
								...messages[streamingIdx],
								content: messages[streamingIdx].content + (data.content as string)
							};
						} else {
							messages.push({
								id: `text-${Date.now()}`,
								role: 'assistant',
								content: data.content as string,
								type: 'text',
								streaming: true
							});
						}

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'tool_use': {
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];
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

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'tool_result': {
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = [...tab.messages];
						const toolUseId = data.tool_use_id as string;

						// Find and update the matching tool_use message
						// First try to match by toolId, then fall back to any streaming tool
						let foundByToolId = false;
						for (let i = messages.length - 1; i >= 0; i--) {
							const m = messages[i];
							if (m.type === 'tool_use' && m.toolId === toolUseId) {
								messages[i] = { ...m, streaming: false };
								foundByToolId = true;
								break;
							}
						}

						// If not found by toolId, update any streaming tool_use message
						if (!foundByToolId) {
							for (let i = messages.length - 1; i >= 0; i--) {
								const m = messages[i];
								if (m.type === 'tool_use' && m.streaming) {
									messages[i] = { ...m, streaming: false };
									break;
								}
							}
						}

						messages.push({
							id: `result-${Date.now()}`,
							role: 'assistant',
							content: data.output as string,
							type: 'tool_result',
							toolName: data.name as string,
							toolId: toolUseId,
							streaming: false
						});

						messages.push({
							id: `text-${Date.now()}-cont`,
							role: 'assistant',
							content: '',
							type: 'text',
							streaming: true
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'done': {
				const metadata = data.metadata as Record<string, unknown>;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						if (messages.length > 0) {
							const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
							if (lastIdx !== -1) {
								messages[lastIdx] = {
									...messages[lastIdx],
									metadata
								};
							}
						}

						// Update title if this is the first response
						let title = tab.title;
						if (title === 'New Chat' && messages.length > 0) {
							const firstUserMsg = messages.find(m => m.role === 'user');
							if (firstUserMsg) {
								title = firstUserMsg.content.substring(0, 30) + (firstUserMsg.content.length > 30 ? '...' : '');
							}
						}

						return {
							...tab,
							messages,
							isStreaming: false,
							sessionId: data.session_id as string || tab.sessionId,
							title
						};
					})
				}));

				// Refresh sessions list
				loadSessionsInternal();
				break;
			}

			case 'stopped':
			case 'interrupted': {
				// Both 'stopped' (from task cancellation) and 'interrupted' (from SDK interrupt)
				// should be handled the same way - stop streaming and mark as stopped
				console.log(`[Tab ${tabId}] Received ${msgType} message`);
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						if (messages.length > 0) {
							const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
							if (lastIdx !== -1) {
								messages[lastIdx] = {
									...messages[lastIdx],
									content: messages[lastIdx].content + '\n\n[Stopped]'
								};
							}
						}

						return { ...tab, messages, isStreaming: false };
					})
				}));
				break;
			}

			case 'error': {
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						return {
							...tab,
							messages,
							isStreaming: false,
							error: data.message as string
						};
					})
				}));
				break;
			}

			case 'ping': {
				const ws = tabConnections.get(tabId);
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				break;
			}
		}
	}

	/**
	 * Load sessions list (user's own sessions)
	 * For admins: loads sessions where api_user_id IS NULL (their own chats)
	 * For API users: the backend automatically filters to their sessions
	 */
	async function loadSessionsInternal() {
		update(s => ({ ...s, sessionsLoading: true }));
		try {
			// Load user's own sessions (admin_only=true for admins gets sessions without api_user_id)
			const sessions = await api.get<Session[]>('/sessions?limit=50&admin_only=true');
			update(s => ({ ...s, sessions, sessionsLoading: false }));
		} catch (e) {
			console.error('Failed to load sessions:', e);
			update(s => ({ ...s, sessionsLoading: false }));
		}
	}

	/**
	 * Load admin sessions (all API user sessions for admin view)
	 */
	async function loadAdminSessionsInternal(apiUserId?: string | null) {
		try {
			let url = '/sessions?limit=50';
			if (apiUserId) {
				// Filter by specific API user
				url += `&api_user_id=${encodeURIComponent(apiUserId)}`;
			} else {
				// Show all API user sessions (exclude admin's own sessions)
				url += '&api_users_only=true';
			}
			const adminSessions = await api.get<Session[]>(url);
			update(s => ({ ...s, adminSessions, adminSessionsFilter: apiUserId ?? null }));
		} catch (e) {
			console.error('Failed to load admin sessions:', e);
		}
	}

	/**
	 * Load API users list (admin only)
	 */
	async function loadApiUsersInternal() {
		try {
			const apiUsers = await api.get<ApiUser[]>('/api-users');
			update(s => ({ ...s, apiUsers }));
		} catch (e) {
			// Non-admins will get 403, that's expected
			console.debug('Failed to load API users (may not be admin):', e);
		}
	}

	return {
		subscribe,

		/**
		 * Initialize - connect WebSocket for all existing tabs
		 */
		init() {
			const state = get({ subscribe });
			for (const tab of state.tabs) {
				connectTab(tab.id);
			}
		},

		/**
		 * Cleanup all connections
		 */
		destroy() {
			const state = get({ subscribe });
			for (const tab of state.tabs) {
				disconnectTab(tab.id);
			}
		},

		/**
		 * Create a new tab
		 */
		createTab(sessionId?: string) {
			const state = get({ subscribe });
			const newTabId = generateTabId();

			const newTab: ChatTab = {
				id: newTabId,
				title: 'New Chat',
				sessionId: sessionId || null,
				messages: [],
				isStreaming: false,
				wsConnected: false,
				error: null,
				profile: state.defaultProfile,
				project: state.defaultProject
			};

			update(s => ({
				...s,
				tabs: [...s.tabs, newTab],
				activeTabId: newTabId
			}));

			// Connect WebSocket for new tab
			connectTab(newTabId);

			// If sessionId provided, load that session
			if (sessionId) {
				this.loadSessionInTab(newTabId, sessionId);
			}

			return newTabId;
		},

		/**
		 * Close a tab
		 */
		closeTab(tabId: string) {
			const state = get({ subscribe });

			// Don't close if it's the last tab
			if (state.tabs.length <= 1) {
				return;
			}

			// Disconnect WebSocket
			disconnectTab(tabId);

			// Remove tab and update active tab if needed
			update(s => {
				const tabIndex = s.tabs.findIndex(t => t.id === tabId);
				const newTabs = s.tabs.filter(t => t.id !== tabId);

				let newActiveId = s.activeTabId;
				if (s.activeTabId === tabId) {
					// Switch to adjacent tab
					newActiveId = newTabs[Math.min(tabIndex, newTabs.length - 1)]?.id || null;
				}

				return {
					...s,
					tabs: newTabs,
					activeTabId: newActiveId
				};
			});
		},

		/**
		 * Switch to a tab
		 */
		setActiveTab(tabId: string) {
			update(s => ({ ...s, activeTabId: tabId }));
		},

		/**
		 * Send message in a specific tab
		 */
		sendMessage(tabId: string, prompt: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			const ws = tabConnections.get(tabId);
			if (!ws || ws.readyState !== WebSocket.OPEN) {
				updateTab(tabId, { error: 'Not connected' });
				return;
			}

			// Add user message immediately
			const userMsgId = `user-${Date.now()}`;
			update(s => ({
				...s,
				tabs: s.tabs.map(t => {
					if (t.id !== tabId) return t;
					return {
						...t,
						messages: [...t.messages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt
						}],
						isStreaming: true,
						error: null,
						title: t.title === 'New Chat' ? prompt.substring(0, 30) + (prompt.length > 30 ? '...' : '') : t.title
					};
				})
			}));

			// Send query
			ws.send(JSON.stringify({
				type: 'query',
				prompt,
				session_id: tab.sessionId,
				profile: tab.profile,
				project: tab.project || undefined
			}));
		},

		/**
		 * Stop generation in a specific tab
		 *
		 * Note: We do NOT set isStreaming: false here. We wait for the backend
		 * to send a 'stopped' message confirming the stop was successful.
		 * This prevents the UI from showing "stopped" while the backend continues.
		 */
		stopGeneration(tabId: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN && tab.sessionId) {
				console.log(`[Tab ${tabId}] Sending stop request for session ${tab.sessionId}`);
				ws.send(JSON.stringify({
					type: 'stop',
					session_id: tab.sessionId
				}));
			}

			// Don't set isStreaming: false here - wait for 'stopped' confirmation from backend
			// The backend will send either 'stopped' or 'done' when it actually stops
		},

		/**
		 * Load a session into a specific tab
		 */
		async loadSessionInTab(tabId: string, sessionId: string) {
			try {
				const session = await api.get<Session & { messages: Array<Record<string, unknown>> }>(`/sessions/${sessionId}`);
				const messages: ChatMessage[] = session.messages.map((m, i) => {
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
				});

				// Generate title from first user message
				let title = 'Chat';
				const firstUserMsg = messages.find(m => m.role === 'user');
				if (firstUserMsg) {
					title = firstUserMsg.content.substring(0, 30) + (firstUserMsg.content.length > 30 ? '...' : '');
				}

				updateTab(tabId, {
					sessionId: session.id,
					messages,
					title,
					error: null
				});

				return true;
			} catch (e: unknown) {
				const error = e as { detail?: string };
				updateTab(tabId, { error: error.detail || 'Failed to load session' });
				return false;
			}
		},

		/**
		 * Set profile for a tab
		 */
		setTabProfile(tabId: string, profileId: string) {
			updateTab(tabId, { profile: profileId });
		},

		/**
		 * Set project for a tab
		 */
		setTabProject(tabId: string, projectId: string) {
			updateTab(tabId, { project: projectId });
		},

		/**
		 * Set default profile (persisted)
		 */
		setDefaultProfile(profileId: string) {
			if (typeof window !== 'undefined') {
				localStorage.setItem('aihub_selectedProfile', profileId);
			}
			update(s => ({ ...s, defaultProfile: profileId }));
		},

		/**
		 * Set default project (persisted)
		 */
		setDefaultProject(projectId: string) {
			if (typeof window !== 'undefined') {
				localStorage.setItem('aihub_selectedProject', projectId);
			}
			update(s => ({ ...s, defaultProject: projectId }));
		},

		/**
		 * Clear error for a tab
		 */
		clearTabError(tabId: string) {
			updateTab(tabId, { error: null });
		},

		/**
		 * Start new chat in current tab
		 */
		startNewChatInTab(tabId: string) {
			// Disconnect and reconnect to reset session
			disconnectTab(tabId);
			updateTab(tabId, {
				sessionId: null,
				messages: [],
				isStreaming: false,
				error: null,
				title: 'New Chat'
			});
			connectTab(tabId);
		},

		// Data loading functions
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

		async loadAdminSessions(apiUserId?: string | null) {
			await loadAdminSessionsInternal(apiUserId);
		},

		async loadApiUsers() {
			await loadApiUsersInternal();
		},

		setAdminSessionsFilter(apiUserId: string | null) {
			update(s => ({ ...s, adminSessionsFilter: apiUserId }));
			loadAdminSessionsInternal(apiUserId);
		},

		// Profile/Project CRUD operations
		async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
			await api.post('/profiles', data);
			await this.loadProfiles();
		},

		async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
			await api.put(`/profiles/${profileId}`, data);
			await this.loadProfiles();
		},

		async deleteProfile(profileId: string) {
			await api.delete(`/profiles/${profileId}`);
			await this.loadProfiles();
		},

		async createProject(data: { id: string; name: string; description?: string }) {
			await api.post('/projects', data);
			await this.loadProjects();
		},

		async deleteProject(projectId: string) {
			await api.delete(`/projects/${projectId}`);
			await this.loadProjects();
		},

		async deleteSession(sessionId: string) {
			await api.delete(`/sessions/${sessionId}`);
			await this.loadSessions();

			// Clear any tabs that had this session
			update(s => ({
				...s,
				tabs: s.tabs.map(tab =>
					tab.sessionId === sessionId
						? { ...tab, sessionId: null, messages: [], title: 'New Chat' }
						: tab
				)
			}));
		},

		// Selection mode methods
		toggleSelectionMode(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return {
						...s,
						adminSelectionMode: !s.adminSelectionMode,
						selectedAdminSessionIds: new Set<string>()
					};
				}
				return {
					...s,
					selectionMode: !s.selectionMode,
					selectedSessionIds: new Set<string>()
				};
			});
		},

		exitSelectionMode(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return {
						...s,
						adminSelectionMode: false,
						selectedAdminSessionIds: new Set<string>()
					};
				}
				return {
					...s,
					selectionMode: false,
					selectedSessionIds: new Set<string>()
				};
			});
		},

		toggleSessionSelection(sessionId: string, isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					const newSet = new Set(s.selectedAdminSessionIds);
					if (newSet.has(sessionId)) {
						newSet.delete(sessionId);
					} else {
						newSet.add(sessionId);
					}
					return { ...s, selectedAdminSessionIds: newSet };
				}
				const newSet = new Set(s.selectedSessionIds);
				if (newSet.has(sessionId)) {
					newSet.delete(sessionId);
				} else {
					newSet.add(sessionId);
				}
				return { ...s, selectedSessionIds: newSet };
			});
		},

		selectAllSessions(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					const allIds = new Set(s.adminSessions.map(session => session.id));
					return { ...s, selectedAdminSessionIds: allIds };
				}
				const allIds = new Set(s.sessions.map(session => session.id));
				return { ...s, selectedSessionIds: allIds };
			});
		},

		deselectAllSessions(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return { ...s, selectedAdminSessionIds: new Set<string>() };
				}
				return { ...s, selectedSessionIds: new Set<string>() };
			});
		},

		async deleteSelectedSessions(isAdmin: boolean = false) {
			const state = get({ subscribe });
			const sessionIds = isAdmin
				? Array.from(state.selectedAdminSessionIds)
				: Array.from(state.selectedSessionIds);

			if (sessionIds.length === 0) return;

			// Use batch delete endpoint
			await api.post('/sessions/batch-delete', { session_ids: sessionIds });

			// Reload sessions
			await this.loadSessions();
			if (isAdmin) {
				await loadAdminSessionsInternal(state.adminSessionsFilter);
			}

			// Clear any tabs that had deleted sessions
			update(s => ({
				...s,
				tabs: s.tabs.map(tab =>
					sessionIds.includes(tab.sessionId || '')
						? { ...tab, sessionId: null, messages: [], title: 'New Chat' }
						: tab
				),
				// Exit selection mode and clear selections
				selectionMode: isAdmin ? s.selectionMode : false,
				adminSelectionMode: isAdmin ? false : s.adminSelectionMode,
				selectedSessionIds: isAdmin ? s.selectedSessionIds : new Set<string>(),
				selectedAdminSessionIds: isAdmin ? new Set<string>() : s.selectedAdminSessionIds
			}));
		}
	};
}

export const tabs = createTabsStore();

// Derived stores for convenience
export const allTabs = derived(tabs, $tabs => $tabs.tabs);
export const activeTabId = derived(tabs, $tabs => $tabs.activeTabId);
export const activeTab = derived(tabs, $tabs => $tabs.tabs.find(t => t.id === $tabs.activeTabId));
export const profiles = derived(tabs, $tabs => $tabs.profiles);
export const projects = derived(tabs, $tabs => $tabs.projects);
export const sessions = derived(tabs, $tabs => $tabs.sessions);
export const adminSessions = derived(tabs, $tabs => $tabs.adminSessions);
export const apiUsers = derived(tabs, $tabs => $tabs.apiUsers);
export const adminSessionsFilter = derived(tabs, $tabs => $tabs.adminSessionsFilter);
export const defaultProfile = derived(tabs, $tabs => $tabs.defaultProfile);
export const defaultProject = derived(tabs, $tabs => $tabs.defaultProject);
// Selection state derived stores
export const selectedSessionIds = derived(tabs, $tabs => $tabs.selectedSessionIds);
export const selectedAdminSessionIds = derived(tabs, $tabs => $tabs.selectedAdminSessionIds);
export const selectionMode = derived(tabs, $tabs => $tabs.selectionMode);
export const adminSelectionMode = derived(tabs, $tabs => $tabs.adminSelectionMode);
// Loading state
export const sessionsLoading = derived(tabs, $tabs => $tabs.sessionsLoading);
