/**
 * Tabbed Chat Store - Supports multiple concurrent chat sessions
 *
 * Each tab has its own WebSocket connection and independent state.
 * Tabs can run queries simultaneously without interfering with each other.
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile, PermissionRequest as PermissionRequestType } from '$lib/api/client';
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

export type MessageType = 'text' | 'tool_use' | 'tool_result' | 'system' | 'subagent';

// Subagent child message - represents a single tool call or text from a subagent
export interface SubagentChildMessage {
	id: string;
	type: 'text' | 'tool_use' | 'tool_result';
	content: string;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	timestamp?: string;
}

export interface ChatMessage {
	id: string;
	role: 'user' | 'assistant' | 'system';
	content: string;
	type?: MessageType;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use messages (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	metadata?: Record<string, unknown>;
	streaming?: boolean;
	partialToolInput?: string; // Accumulator for streaming tool input JSON
	systemSubtype?: string; // For system messages (e.g., 'context' for /context command)
	systemData?: Record<string, unknown>; // Raw data from system message
	// Subagent-specific fields
	agentId?: string; // The subagent's unique ID (e.g., '00ed8f4d')
	agentType?: string; // The subagent type (e.g., 'Explore', 'Plan')
	agentDescription?: string; // Task description from the Task tool
	agentPrompt?: string; // The initial prompt sent to the subagent
	agentStatus?: 'pending' | 'running' | 'completed' | 'error'; // Current status
	agentChildren?: SubagentChildMessage[]; // Nested messages from subagent execution
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
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	// Actual context window usage from /context command (source of truth)
	contextUsed: number | null;
	contextMax: number;
	// Session overrides (override profile settings for this session)
	modelOverride: string | null;  // null = use profile default
	permissionModeOverride: string | null;  // null = use profile default
	// Permission requests queue
	pendingPermissions: PermissionRequestType[];
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
// Track tabs waiting for history response - queue sync events until history arrives
const tabsWaitingForHistory: Map<string, Array<Record<string, unknown>>> = new Map();

// Interface for persisted tab state (only what we need to restore tabs)
interface PersistedTab {
	id: string;
	title: string;
	sessionId: string | null;
	profile: string;
	project: string;
}

interface PersistedTabsState {
	tabs: PersistedTab[];
	activeTabId: string | null;
}

// Debounce timer for saving tabs
let saveTabsTimer: ReturnType<typeof setTimeout> | null = null;
const SAVE_DEBOUNCE_MS = 1000;

// Flag to prevent saving during initial load
let isInitializing = false;

// Persistent device ID for multi-device sync
// This ensures the same browser/device keeps the same ID across reconnections
function getOrCreateDeviceId(): string {
	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	}

	let deviceId = localStorage.getItem('aihub_device_id');
	if (!deviceId) {
		deviceId = `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
		localStorage.setItem('aihub_device_id', deviceId);
		console.log('[Tabs] Generated new device ID:', deviceId);
	}
	return deviceId;
}

/**
 * Save tabs state to backend (debounced)
 */
async function saveTabsToServer(state: TabsState) {
	// Don't save during initialization
	if (isInitializing) return;

	// Clear existing timer
	if (saveTabsTimer) {
		clearTimeout(saveTabsTimer);
	}

	// Debounce the save
	saveTabsTimer = setTimeout(async () => {
		try {
			const persistedState: PersistedTabsState = {
				tabs: state.tabs.map(tab => ({
					id: tab.id,
					title: tab.title,
					sessionId: tab.sessionId,
					profile: tab.profile,
					project: tab.project
				})),
				activeTabId: state.activeTabId
			};

			await api.put('/preferences/open_tabs', {
				key: 'open_tabs',
				value: persistedState
			});
			console.log('[Tabs] Saved tabs state to server');
		} catch (error) {
			console.error('[Tabs] Failed to save tabs state:', error);
		}
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Load tabs state from backend
 */
async function loadTabsFromServer(): Promise<PersistedTabsState | null> {
	try {
		const response = await api.get<{ key: string; value: PersistedTabsState } | null>('/preferences/open_tabs');
		if (response && response.value) {
			console.log('[Tabs] Loaded tabs state from server:', response.value);
			return response.value;
		}
	} catch (error) {
		console.error('[Tabs] Failed to load tabs state:', error);
	}
	return null;
}

// Load persisted values - empty string means no selection (user must choose)
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
			project: getPersistedProject(),
			totalTokensIn: 0,
			totalTokensOut: 0,
			totalCacheCreationTokens: 0,
			totalCacheReadTokens: 0,
			contextUsed: null,
			contextMax: 200000,
			modelOverride: null,
			permissionModeOverride: null,
			pendingPermissions: []
		}],
		activeTabId: initialTabId,
		profiles: [],
		projects: [],
		sessions: [],
		adminSessions: [],
		apiUsers: [],
		adminSessionsFilter: null,
		// Initialize selection state
		selectedSessionIds: new Set<string>(),
		selectedAdminSessionIds: new Set<string>(),
		selectionMode: false,
		adminSelectionMode: false,
		// Initialize loading state
		sessionsLoading: false
	});

	/**
	 * Get WebSocket URL with device_id for multi-device sync
	 */
	function getWsUrl(): string {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		const deviceId = getOrCreateDeviceId();
		return `${protocol}//${host}/api/v1/ws/chat?device_id=${encodeURIComponent(deviceId)}`;
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
	function connectTab(tabId: string, isReconnect = false) {
		const existingWs = tabConnections.get(tabId);
		if (existingWs && (existingWs.readyState === WebSocket.OPEN || existingWs.readyState === WebSocket.CONNECTING)) {
			return;
		}

		// Try to get token from cookie (may be null if httpOnly)
		// Server will also check cookie directly for httpOnly cookies
		const token = getAuthToken();
		let url = getWsUrl(); // Already includes device_id
		if (token) {
			url = `${url}&token=${encodeURIComponent(token)}`;
		}
		console.log(`[Tab ${tabId}] ${isReconnect ? 'Reconnecting' : 'Connecting'} to WebSocket...`);

		const ws = new WebSocket(url);
		tabConnections.set(tabId, ws);

		ws.onopen = () => {
			console.log(`[Tab ${tabId}] WebSocket connected`);
			updateTab(tabId, { wsConnected: true, error: null });

			// Always register with SyncEngine by sending load_session if tab has a session
			// This is critical for multi-device sync - devices must be registered to receive events
			const tab = getTab(tabId);
			if (tab?.sessionId) {
				console.log(`[Tab ${tabId}] Registering with session via load_session:`, tab.sessionId);

				// Mark tab as waiting for history - queue sync events until history arrives
				// This prevents race conditions where sync events arrive before history
				tabsWaitingForHistory.set(tabId, []);

				// Use the WebSocket to register with SyncEngine and get latest messages
				// The backend will return isStreaming=true with buffer if still active,
				// or isStreaming=false with complete history if finished
				ws.send(JSON.stringify({
					type: 'load_session',
					session_id: tab.sessionId
				}));
			}

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
						connectTab(tabId, true); // Pass isReconnect flag
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

		// Clear any waiting for history state
		tabsWaitingForHistory.delete(tabId);

		const ws = tabConnections.get(tabId);
		if (ws) {
			ws.close(1000);
			tabConnections.delete(tabId);
		}

		updateTab(tabId, { wsConnected: false });
	}

	/**
	 * Handle sync events from other devices (via SyncEngine)
	 */
	function handleSyncEvent(tabId: string, data: Record<string, unknown>) {
		const eventType = data.event_type as string;
		const sessionId = data.session_id as string;
		const eventData = data.data as Record<string, unknown>;

		// Only process events for tabs that have this session loaded
		const tab = getTab(tabId);
		if (!tab || tab.sessionId !== sessionId) {
			return;
		}

		// If tab is waiting for history, queue the event to process after history loads
		// This prevents race conditions where sync events arrive before history
		const queuedEvents = tabsWaitingForHistory.get(tabId);
		if (queuedEvents !== undefined) {
			console.log(`[Tab ${tabId}] Queueing sync event (waiting for history):`, eventType);
			queuedEvents.push(data);
			return;
		}

		console.log(`[Tab ${tabId}] Sync event:`, eventType, eventData);

		switch (eventType) {
			case 'message_added': {
				// A new message was added by another device (e.g., user message)
				const message = eventData.message as Record<string, unknown>;
				console.log(`[Tab ${tabId}] Message added from another device:`, message);

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						return {
							...t,
							messages: [...t.messages, {
								id: `msg-sync-${Date.now()}`,
								role: message.role as 'user' | 'assistant',
								content: message.content as string,
								type: message.role === 'user' ? undefined : 'text' as const,
								streaming: false
							}]
						};
					})
				}));
				break;
			}

			case 'stream_start': {
				// Another device started streaming
				console.log(`[Tab ${tabId}] Another device started streaming`);
				const messageId = eventData.message_id as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						return {
							...t,
							isStreaming: true,
							messages: [...t.messages, {
								id: messageId || `assistant-sync-${Date.now()}`,
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

			case 'stream_chunk': {
				// Content chunk from another device
				const chunkType = eventData.chunk_type as string;
				const content = eventData.content as string;
				console.log(`[Tab ${tabId}] stream_chunk: type=${chunkType}, content="${content?.substring(0, 50)}..."`);

				if (chunkType === 'text') {
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);

							if (streamingIdx !== -1) {
								messages[streamingIdx] = {
									...messages[streamingIdx],
									content: messages[streamingIdx].content + (content || '')
								};
							} else {
								messages.push({
									id: `text-sync-${Date.now()}`,
									role: 'assistant' as const,
									content: content || '',
									type: 'text' as const,
									streaming: true
								});
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'tool_input') {
					// Tool input streaming - accumulate JSON in the current tool_use message
					const partialJson = content;
					console.log(`[Tab ${tabId}] tool_input sync: ${partialJson?.length} chars`);

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];
							const toolIdx = messages.findLastIndex(
								m => m.type === 'tool_use' && m.streaming
							);

							if (toolIdx !== -1) {
								const current = messages[toolIdx];
								const partialInput = (current.partialToolInput || '') + partialJson;

								// Try to parse accumulated JSON
								let parsedInput = current.toolInput || {};
								try {
									parsedInput = JSON.parse(partialInput);
								} catch {
									// Not valid JSON yet, keep accumulating
								}

								messages[toolIdx] = {
									...current,
									partialToolInput: partialInput,
									toolInput: parsedInput
								};
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'tool_use') {
					// Tool use event - this is the ONLY place we create tool messages for sync
					// (stream_block_start is ignored for tool_use to avoid duplicates)
					const toolId = eventData.tool_id as string;
					const toolName = eventData.tool_name as string;
					const toolInput = eventData.tool_input as Record<string, unknown>;
					console.log(`[Tab ${tabId}] tool_use sync:`, { toolName, toolId, toolInput });

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							let messages = [...t.messages];

							// Handle current streaming text message - finalize it
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (streamingIdx !== -1) {
								if (messages[streamingIdx].content) {
									messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
								} else {
									messages = messages.filter((_, i) => i !== streamingIdx);
								}
							}

							// Create the tool use message
							messages.push({
								id: `tool-sync-${Date.now()}-${toolId || ''}`,
								role: 'assistant' as const,
								content: '',
								type: 'tool_use' as const,
								toolName: toolName,
								toolId: toolId,
								toolInput: toolInput,
								toolStatus: 'running' as const,
								streaming: true
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'tool_result') {
					console.log(`[Tab ${tabId}] tool_result sync:`, {
						tool_id: eventData.tool_id,
						content: content?.substring(0, 100),
						full_eventData: eventData
					});
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];

							// Mark the matching tool_use as complete
							const toolId = eventData.tool_id as string;
							const toolUseIdx = messages.findLastIndex(
								m => m.type === 'tool_use' && (m.toolId === toolId || m.streaming)
							);
							if (toolUseIdx !== -1) {
								messages[toolUseIdx] = {
									...messages[toolUseIdx],
									toolResult: content,
									toolStatus: 'complete' as const,
									streaming: false
								};
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'system') {
					// System message from another device
					const subtype = eventData.subtype as string;
					const systemData = eventData.data as Record<string, unknown>;
					console.log(`[Tab ${tabId}] system sync:`, { subtype, systemData });

					// Extract content for display
					let displayContent = '';
					if (systemData?.content && typeof systemData.content === 'string') {
						displayContent = systemData.content;
					} else if (typeof systemData === 'object') {
						displayContent = JSON.stringify(systemData, null, 2);
					}

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							// Mark any streaming text message as complete
							let messages = t.messages.map(m =>
								m.streaming && m.type === 'text' ? { ...m, streaming: false } : m
							);
							// Remove empty streaming text messages
							messages = messages.filter(
								m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
							);

							// Add the system message
							messages.push({
								id: `system-sync-${Date.now()}`,
								role: 'system' as const,
								content: displayContent,
								type: 'system' as const,
								systemSubtype: subtype,
								systemData: systemData,
								streaming: false
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_start') {
					// Subagent task started on another device
					const agentId = eventData.agent_id as string;
					const agentType = eventData.agent_type as string;
					const description = eventData.description as string;
					const prompt = eventData.prompt as string;
					const toolId = eventData.tool_id as string;
					console.log(`[Tab ${tabId}] subagent_start sync:`, { agentId, agentType, description });

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							let messages = [...t.messages];

							// Mark any streaming text message as complete
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (streamingIdx !== -1) {
								if (messages[streamingIdx].content) {
									messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
								} else {
									messages = messages.filter((_, i) => i !== streamingIdx);
								}
							}

							// Add subagent message group
							messages.push({
								id: `subagent-sync-${agentId}`,
								role: 'assistant' as const,
								content: '',
								type: 'subagent' as const,
								toolId: toolId,
								agentId: agentId,
								agentType: agentType,
								agentDescription: description,
								agentPrompt: prompt,
								agentStatus: 'running' as const,
								agentChildren: [],
								streaming: true
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_chunk') {
					// Text chunk from subagent on another device
					const agentId = eventData.agent_id as string;
					const content = eventData.content as string;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									const lastChild = children[children.length - 1];

									if (lastChild && lastChild.type === 'text') {
										children[children.length - 1] = {
											...lastChild,
											content: lastChild.content + content
										};
									} else {
										children.push({
											id: `${agentId}-text-sync-${Date.now()}`,
											type: 'text',
											content: content
										});
									}
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_tool_use') {
					// Tool use within subagent on another device
					const agentId = eventData.agent_id as string;
					const toolName = eventData.name as string;
					const toolId = eventData.id as string;
					const toolInput = eventData.input as Record<string, unknown>;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									children.push({
										id: `${agentId}-tool-sync-${toolId}`,
										type: 'tool_use',
										content: '',
										toolName: toolName,
										toolId: toolId,
										toolInput: toolInput,
										toolStatus: 'running'
									});
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_tool_result') {
					// Tool result within subagent on another device
					const agentId = eventData.agent_id as string;
					const toolUseId = eventData.tool_use_id as string;
					const output = eventData.output as string;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									// Find matching tool_use and embed result
									for (let i = children.length - 1; i >= 0; i--) {
										if (children[i].type === 'tool_use' && children[i].toolId === toolUseId) {
											children[i] = {
												...children[i],
												toolResult: output,
												toolStatus: 'complete'
											};
											break;
										}
									}
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_done') {
					// Subagent completed on another device
					const agentId = eventData.agent_id as string;
					const result = eventData.result as string | undefined;
					const isError = eventData.is_error as boolean | undefined;
					console.log(`[Tab ${tabId}] subagent_done sync:`, { agentId, isError });

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									return {
										...m,
										content: result || '',
										agentStatus: isError ? 'error' as const : 'completed' as const,
										streaming: false
									};
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'stream_block_start') {
					// Start of a content block from another device
					// For tool_use blocks, we DO NOT create messages here - we let the 'tool_use' event
					// create the message to avoid race conditions and duplicates.
					// The 'tool_use' event has the full data (name, id, input) anyway.
					const blockType = eventData.block_type as string;
					const contentBlock = eventData.content_block as { id?: string; name?: string };
					console.log(`[Tab ${tabId}] stream_block_start sync (ignored for tool_use):`, { blockType, contentBlock });
					// No action needed - tool_use event will create the message
				} else if (chunkType === 'stream_block_stop') {
					// End of a content block - just log for now
					console.log(`[Tab ${tabId}] stream_block_stop sync:`, { index: eventData.index });
				}
				break;
			}

			case 'stream_end': {
				// Another device finished streaming
				console.log(`[Tab ${tabId}] Another device finished streaming`);
				const metadata = eventData.metadata as Record<string, unknown>;
				const interrupted = eventData.interrupted as boolean;

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;

						// Mark all streaming messages as complete
						let messages = t.messages.map(m =>
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

						return { ...t, messages, isStreaming: false };
					})
				}));

				// Refresh sessions list
				loadSessionsInternal();
				break;
			}

			case 'session_rewound': {
				// Another device rewound the session - reload to get updated messages
				const targetUuid = eventData.target_uuid as string;
				const messagesRemoved = eventData.messages_removed as number;
				console.log(`[Tab ${tabId}] Session rewound by another device:`, { targetUuid, messagesRemoved });

				// Reload the session to get the updated message list
				const tab = getTab(tabId);
				if (tab?.sessionId) {
					const ws = tabConnections.get(tabId);
					if (ws && ws.readyState === WebSocket.OPEN) {
						ws.send(JSON.stringify({
							type: 'load_session',
							session_id: tab.sessionId
						}));
					}
				}
				break;
			}

			case 'session_opened': {
				// Another device opened/resumed this session
				const deviceId = eventData.device_id as string;
				const isNew = eventData.is_new as boolean;
				console.log(`[Tab ${tabId}] Session opened by another device:`, { deviceId, isNew });
				// Just log for now - could show a notification or indicator
				break;
			}

			case 'session_closed': {
				// Another device closed this session
				const deviceId = eventData.device_id as string;
				console.log(`[Tab ${tabId}] Session closed by another device:`, { deviceId });
				// Just log for now - could show a notification or indicator
				break;
			}
		}
	}

	/**
	 * Handle incoming WebSocket message for a tab
	 */
	function handleTabMessage(tabId: string, data: Record<string, unknown>) {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Handle sync events from other devices
		if (eventType) {
			handleSyncEvent(tabId, data);
			return;
		}

		switch (msgType) {
			case 'history': {
				// Handle both JSONL format (with explicit type) and legacy DB format
				const rawMessages = (data.messages as Array<Record<string, unknown>>) || [];

				// First pass: convert raw messages to ChatMessage format
				// Also build a map of tool_results by tool_use_id for grouping
				const toolResultMap: Map<string, { content: string; toolStatus: 'complete' | 'error' }> = new Map();

				// Pre-scan for tool_results to group them with tool_use
				for (const m of rawMessages) {
					const msgType = m.type as MessageType | undefined;
					const role = m.role as string;
					if (msgType === 'tool_result' || role === 'tool_result') {
						const toolUseId = m.toolId || m.tool_use_id;
						if (toolUseId) {
							toolResultMap.set(toolUseId as string, {
								content: m.content as string || '',
								toolStatus: 'complete'
							});
						}
					}
				}

				const messages: ChatMessage[] = [];
				const seenToolResults = new Set<string>(); // Track which tool_results we've merged

				for (let i = 0; i < rawMessages.length; i++) {
					const m = rawMessages[i];
					// Determine message type - JSONL messages have explicit 'type' field
					let msgType: MessageType | undefined = m.type as MessageType | undefined;

					// Legacy DB format: infer type from role
					if (!msgType && m.role === 'assistant') {
						msgType = 'text';
					} else if (!msgType && (m.role === 'tool_use' || m.role === 'tool_result')) {
						msgType = m.role as MessageType;
					}

					// Handle system messages (e.g., /context, /compact output)
					if (msgType === 'system' || m.role === 'system') {
						messages.push({
							id: String(m.id || `msg-${i}`),
							role: 'system' as const,
							content: m.content as string,
							type: 'system' as const,
							systemSubtype: m.subtype as string | undefined,
							systemData: { content: m.content } as Record<string, unknown>,
							metadata: m.metadata as Record<string, unknown>,
							streaming: false
						});
						continue;
					}

					// Skip standalone tool_result messages - they should be grouped with tool_use
					if (msgType === 'tool_result') {
						const toolUseId = m.toolId || m.tool_use_id;
						if (toolUseId) {
							seenToolResults.add(toolUseId as string);
						}
						// Skip - will be merged into tool_use message
						continue;
					}

					// Build the chat message with all fields
					const chatMessage: ChatMessage = {
						id: String(m.id || `msg-${i}`),
						role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
						content: m.content as string,
						type: msgType,
						toolName: (m.toolName || m.tool_name) as string | undefined,
						toolId: m.toolId as string | undefined,
						toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
						toolResult: m.toolResult as string | undefined,
						toolStatus: m.toolStatus as 'running' | 'complete' | 'error' | undefined,
						metadata: m.metadata as Record<string, unknown>,
						streaming: false
					};

					// For tool_use messages, try to merge the corresponding tool_result
					if (msgType === 'tool_use' && chatMessage.toolId) {
						const result = toolResultMap.get(chatMessage.toolId);
						if (result && !chatMessage.toolResult) {
							chatMessage.toolResult = result.content;
							chatMessage.toolStatus = result.toolStatus;
							seenToolResults.add(chatMessage.toolId);
						}
						// If no result yet, mark as complete anyway (history = finished operations)
						if (!chatMessage.toolStatus) {
							chatMessage.toolStatus = 'complete';
						}
					}

					// Add subagent-specific fields if present
					if (msgType === 'subagent') {
						chatMessage.agentId = m.agentId as string | undefined;
						chatMessage.agentType = m.agentType as string | undefined;
						chatMessage.agentDescription = m.agentDescription as string | undefined;
						chatMessage.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error' | undefined;
						chatMessage.agentChildren = m.agentChildren as SubagentChildMessage[] | undefined;
					}

					messages.push(chatMessage);
				}

				// Handle streaming state from backend
				const isStreaming = data.isStreaming as boolean || false;
				const streamingBuffer = data.streamingBuffer as Array<Record<string, unknown>> | undefined;
				console.log(`[Tab ${tabId}] History response - isStreaming:`, isStreaming, 'bufferLen:', streamingBuffer?.length || 0);

				// If session is actively streaming and has buffered content, merge it intelligently
				let finalMessages = messages;
				if (isStreaming && streamingBuffer && streamingBuffer.length > 0) {
					console.log(`[Tab ${tabId}] Late-joining streaming session, merging buffer:`, streamingBuffer.length, 'messages');

					// Create a set of existing message content hashes for deduplication
					const existingContentHashes = new Set<string>();
					for (const msg of messages) {
						// Create a simple hash based on content and type
						const hash = `${msg.type || 'text'}:${msg.toolId || ''}:${(msg.content || '').substring(0, 100)}`;
						existingContentHashes.add(hash);
					}

					// Process buffer messages and only add non-duplicates
					for (const m of streamingBuffer) {
						const bufferType = (m.type || m.chunk_type || 'text') as MessageType;
						const bufferToolId = (m.tool_id || '') as string;
						const bufferContent = (m.content || '') as string;
						const hash = `${bufferType}:${bufferToolId}:${bufferContent.substring(0, 100)}`;

						// Skip if we already have similar content in history
						if (existingContentHashes.has(hash)) {
							console.log(`[Tab ${tabId}] Skipping duplicate buffer message:`, hash.substring(0, 50));
							continue;
						}

						const bufferMsg: ChatMessage = {
							id: `buffer-${Date.now()}-${Math.random()}`,
							role: 'assistant' as const,
							content: bufferContent,
							type: bufferType,
							toolName: m.tool_name as string | undefined,
							toolId: bufferToolId || undefined,
							toolInput: m.tool_input as Record<string, unknown> | undefined,
							streaming: (m.streaming as boolean) ?? true
						};
						finalMessages.push(bufferMsg);
						existingContentHashes.add(hash);
					}
				}

				console.log(`[Tab ${tabId}] Updating tab state - isStreaming:`, isStreaming, 'messageCount:', finalMessages.length);
				updateTab(tabId, {
					sessionId: data.session_id as string,
					messages: finalMessages,
					isStreaming: isStreaming,
					error: null  // Clear any previous error
				});

				// Update tab title based on first message
				if (finalMessages.length > 0 && finalMessages[0].role === 'user') {
					const title = finalMessages[0].content.substring(0, 30) + (finalMessages[0].content.length > 30 ? '...' : '');
					updateTab(tabId, { title });
				}

				// Process any queued sync events that arrived while waiting for history
				const queuedEvents = tabsWaitingForHistory.get(tabId);
				if (queuedEvents && queuedEvents.length > 0) {
					console.log(`[Tab ${tabId}] Processing ${queuedEvents.length} queued sync events`);
					// Remove from waiting map first to prevent re-queueing
					tabsWaitingForHistory.delete(tabId);
					// Process each queued event
					for (const queuedData of queuedEvents) {
						handleSyncEvent(tabId, queuedData);
					}
				} else {
					// No queued events, just clear the waiting state
					tabsWaitingForHistory.delete(tabId);
				}
				break;
			}

			case 'session_created': {
				// New session was created - save sessionId immediately so it persists on refresh
				// This is sent BEFORE streaming starts, ensuring we can reconnect if page refreshes
				const sessionId = data.session_id as string;
				console.log(`[Tab ${tabId}] New session created:`, sessionId);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, sessionId };
					})
				}));

				// Save immediately - critical for reconnection on refresh
				saveTabsToServer(get({ subscribe }));
				break;
			}

			case 'start': {
				// Mark streaming as started, but DON'T create empty message yet
				// Messages will be created when we get actual content (stream_delta for text, stream_block_start for tools)
				// This prevents empty message boxes from appearing when Claude starts with a tool call
				const sessionId = data.session_id as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							sessionId,
							isStreaming: true
							// NO empty message added here - wait for actual content
						};
					})
				}));

				// Save again in case session_created wasn't received (backwards compat)
				saveTabsToServer(get({ subscribe }));
				break;
			}

			case 'chunk': {
				// 'chunk' is the final complete text from AssistantMessage
				// When include_partial_messages=True, we already have this text from stream_delta events
				// So we REPLACE the streaming message content (don't append) to avoid duplication
				console.log(`[WS] Chunk received: len=${(data.content as string).length}`);
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = [...tab.messages];
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);

						if (streamingIdx !== -1) {
							// REPLACE content - this is the final version from AssistantMessage
							// Don't append, as stream_delta already accumulated the text
							messages[streamingIdx] = {
								...messages[streamingIdx],
								content: data.content as string,
								streaming: false // Mark as complete
							};
						} else {
							// No streaming message exists - this is the first/only chunk
							// (happens when include_partial_messages=False)
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

			// StreamEvent handlers for real-time character-by-character streaming
			case 'stream_start': {
				// Start of a new streaming response - just log it
				// DON'T create empty message here - wait for actual content via stream_delta
				// This prevents empty message boxes when Claude's response starts with a tool call
				console.log('[WS] Stream start received');
				// No message created - stream_delta will create message when text arrives
				break;
			}

			case 'stream_block_start': {
				// Start of a content block (text, thinking, tool_use)
				const blockType = data.block_type as string;
				console.log(`[WS] Stream block start: ${blockType}`);

				if (blockType === 'tool_use') {
					const contentBlock = data.content_block as { id?: string; name?: string };
					update(s => ({
						...s,
						tabs: s.tabs.map(tab => {
							if (tab.id !== tabId) return tab;

							let messages = [...tab.messages];
							// Handle any existing streaming text message
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (streamingIdx !== -1) {
								if (messages[streamingIdx].content) {
									// Mark as complete if it has content
									messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
								} else {
									// Remove empty streaming text messages to avoid lingering placeholders
									messages = messages.filter((_, i) => i !== streamingIdx);
								}
							}

							// Add tool use message
							messages.push({
								id: `tool-${Date.now()}-${contentBlock.id || ''}`,
								role: 'assistant',
								content: '',
								type: 'tool_use',
								toolName: contentBlock.name || '',
								toolId: contentBlock.id || '',
								toolInput: {},
								toolStatus: 'running',
								streaming: true
							});

							return { ...tab, messages };
						})
					}));
				}
				break;
			}

			case 'stream_delta': {
				const deltaType = data.delta_type as string;

				if (deltaType === 'text') {
					// Real-time text streaming - character by character!
					const text = data.content as string;
					console.log(`[WS] Stream delta text: ${text.length} chars`);

					update(s => ({
						...s,
						tabs: s.tabs.map(tab => {
							if (tab.id !== tabId) return tab;

							const messages = [...tab.messages];
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);

							if (streamingIdx !== -1) {
								// Append to existing streaming message
								messages[streamingIdx] = {
									...messages[streamingIdx],
									content: messages[streamingIdx].content + text
								};
							} else {
								// Create new streaming message
								messages.push({
									id: `stream-${Date.now()}`,
									role: 'assistant',
									content: text,
									type: 'text',
									streaming: true
								});
							}

							return { ...tab, messages };
						})
					}));
				} else if (deltaType === 'tool_input') {
					// Tool input streaming (partial JSON)
					const partialJson = data.content as string;
					console.log(`[WS] Stream delta tool_input: ${partialJson.length} chars`);

					update(s => ({
						...s,
						tabs: s.tabs.map(tab => {
							if (tab.id !== tabId) return tab;

							const messages = [...tab.messages];
							const toolIdx = messages.findLastIndex(
								m => m.type === 'tool_use' && m.streaming
							);

							if (toolIdx !== -1) {
								const current = messages[toolIdx];
								const partialInput = (current.partialToolInput || '') + partialJson;

								// Try to parse accumulated JSON
								let parsedInput = current.toolInput || {};
								try {
									parsedInput = JSON.parse(partialInput);
								} catch {
									// Not valid JSON yet, keep accumulating
								}

								messages[toolIdx] = {
									...current,
									partialToolInput: partialInput,
									toolInput: parsedInput
								};
							}

							return { ...tab, messages };
						})
					}));
				}
				break;
			}

			case 'stream_block_stop': {
				// End of a content block
				console.log(`[WS] Stream block stop: index=${data.index}`);
				// The block is complete - we don't need to do anything special here
				// as the full content comes via AssistantMessage afterwards
				break;
			}

			case 'stream_message_delta': {
				// Final message metadata
				console.log('[WS] Stream message delta received');
				// Contains stop_reason and usage - we'll get final data from done event
				break;
			}

			case 'tool_use': {
				// tool_use is the final event from AssistantMessage
				// When include_partial_messages=True, stream_block_start may have already created this tool
				const toolId = data.id as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Check if this tool already exists (from stream_block_start)
						const existingToolIdx = messages.findIndex(
							m => m.type === 'tool_use' && m.toolId === toolId
						);

						if (existingToolIdx !== -1) {
							// Update existing tool message with final data
							messages[existingToolIdx] = {
								...messages[existingToolIdx],
								toolName: data.name as string,
								toolInput: data.input as Record<string, unknown>,
								toolStatus: 'running'
							};
							return { ...tab, messages };
						}

						// No existing tool - create new one
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
							id: `tool-${Date.now()}-${toolId || ''}`,
							role: 'assistant',
							content: '',
							type: 'tool_use',
							toolName: data.name as string,
							toolId: toolId,
							toolInput: data.input as Record<string, unknown>,
							toolStatus: 'running',
							streaming: true
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'tool_result': {
				// Group tool result with its corresponding tool_use message
				const toolUseId = data.tool_use_id as string;
				const output = data.output as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Find the matching tool_use message and embed the result
						let found = false;
						for (let i = messages.length - 1; i >= 0; i--) {
							const m = messages[i];
							if (m.type === 'tool_use' && m.toolId === toolUseId) {
								messages[i] = {
									...m,
									toolResult: output,
									toolStatus: 'complete',
									streaming: false
								};
								found = true;
								break;
							}
						}

						// Fallback: if not found by toolId, try any running tool_use
						if (!found) {
							for (let i = messages.length - 1; i >= 0; i--) {
								const m = messages[i];
								if (m.type === 'tool_use' && m.toolStatus === 'running') {
									messages[i] = {
										...m,
										toolResult: output,
										toolStatus: 'complete',
										streaming: false
									};
									break;
								}
							}
						}

						// DON'T add empty streaming placeholder here
						// The stream_delta handler will create a message when text actually arrives
						// Adding empty placeholders causes lingering empty message boxes

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'done': {
				const metadata = data.metadata as Record<string, unknown>;
				const sessionId = data.session_id as string;
				console.log('[WS] Done event received, cleaning up streaming messages');

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
							sessionId: sessionId || tab.sessionId,
							title
						};
					})
				}));

				// Load token counts from session history - single source of truth
				// This is more reliable than tracking incremental tokens during streaming
				if (sessionId) {
					api.get<Session>(`/sessions/${sessionId}`).then(session => {
						console.log('[Tab] Loaded session token counts:', {
							sessionId,
							total_tokens_in: session.total_tokens_in,
							total_tokens_out: session.total_tokens_out,
							cache_creation_tokens: session.cache_creation_tokens,
							cache_read_tokens: session.cache_read_tokens,
							context_tokens: session.context_tokens
						});
						updateTab(tabId, {
							totalTokensIn: session.total_tokens_in || 0,
							totalTokensOut: session.total_tokens_out || 0,
							totalCacheCreationTokens: session.cache_creation_tokens || 0,
							totalCacheReadTokens: session.cache_read_tokens || 0,
							contextUsed: session.context_tokens || 0
						});
					}).catch(err => {
						console.error('[Tab] Failed to load session token counts:', err);
					});
				}

				// Refresh sessions list
				loadSessionsInternal();

				// Save tabs state since sessionId may have changed (debounced)
				saveTabsToServer(get({ subscribe }));
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

			case 'system': {
				// Handle system messages from SDK (e.g., /context command output, streaming events)
				const subtype = data.subtype as string;
				const systemData = data.data as Record<string, unknown>;

				console.log('[WS] System message received:', subtype, systemData);

				// Check if this is a streaming event (when include_partial_messages=true)
				const streamEvent = systemData?.event as { type?: string; delta?: { type?: string; text?: string }; index?: number } | undefined;
				if (streamEvent?.type) {
					const eventType = streamEvent.type;
					console.log(`[WS] Streaming event: ${eventType}`);

					// Handle streaming text deltas
					if (eventType === 'content_block_delta' && streamEvent.delta?.type === 'text_delta' && streamEvent.delta?.text) {
						const deltaText = streamEvent.delta.text;
						console.log(`[WS] Text delta received: len=${deltaText.length}`);

						// Append delta text to the current streaming message
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
										content: messages[streamingIdx].content + deltaText
									};
								} else {
									// Create new streaming message if none exists
									messages.push({
										id: `text-${Date.now()}`,
										role: 'assistant',
										content: deltaText,
										type: 'text',
										streaming: true
									});
								}

								return { ...tab, messages };
							})
						}));
						break;
					}

					// For other streaming events (message_start, content_block_start, etc.), just log
					// These are informational and the actual text comes via content_block_delta
					break;
				}

				// Extract content for display - for local_command, the content is in data.content
				let displayContent = '';
				if (systemData?.content && typeof systemData.content === 'string') {
					displayContent = systemData.content;
				} else if (typeof systemData === 'object') {
					displayContent = JSON.stringify(systemData, null, 2);
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						// First, mark any streaming text message as complete
						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						// Remove empty streaming text messages
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						// Add the system message
						messages.push({
							id: `system-${Date.now()}`,
							role: 'system' as const,
							content: displayContent,
							type: 'system' as const,
							systemSubtype: subtype,
							systemData: systemData,
							streaming: false
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_start': {
				// Subagent task started - create a new subagent message group
				const agentId = data.agent_id as string;
				const agentType = data.agent_type as string;
				const description = data.description as string;
				const prompt = data.prompt as string;
				const toolId = data.tool_id as string; // The tool_use ID that launched this subagent

				console.log('[WS] Subagent started:', agentId, agentType, description);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Find and mark the Task tool_use as not streaming
						const toolUseIdx = messages.findIndex(m => m.type === 'tool_use' && m.toolId === toolId);
						if (toolUseIdx !== -1) {
							messages[toolUseIdx] = { ...messages[toolUseIdx], streaming: false };
						}

						// Mark any streaming text message as complete
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);
						if (streamingIdx !== -1) {
							if (messages[streamingIdx].content) {
								messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
							} else {
								messages = messages.filter((_, i) => i !== streamingIdx);
							}
						}

						// Add subagent message group
						messages.push({
							id: `subagent-${agentId}`,
							role: 'assistant',
							content: '',
							type: 'subagent',
							toolId: toolId,
							agentId: agentId,
							agentType: agentType,
							agentDescription: description,
							agentPrompt: prompt,
							agentStatus: 'running',
							agentChildren: [],
							streaming: true
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_tool_use': {
				// Tool use within a subagent - add to the subagent's children
				const agentId = data.agent_id as string;
				const toolName = data.name as string;
				const toolId = data.id as string;
				const toolInput = data.input as Record<string, unknown>;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								children.push({
									id: `${agentId}-tool-${toolId}`,
									type: 'tool_use',
									content: '',
									toolName: toolName,
									toolId: toolId,
									toolInput: toolInput,
									toolStatus: 'running'
								});
								return { ...m, agentChildren: children };
							}
							return m;
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_tool_result': {
				// Tool result within a subagent - embed in matching tool_use child
				const agentId = data.agent_id as string;
				const toolUseId = data.tool_use_id as string;
				const output = data.output as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								// Find matching tool_use and embed result
								let found = false;
								for (let i = children.length - 1; i >= 0; i--) {
									if (children[i].type === 'tool_use' && children[i].toolId === toolUseId) {
										children[i] = {
											...children[i],
											toolResult: output,
											toolStatus: 'complete'
										};
										found = true;
										break;
									}
								}
								// Fallback: try any running tool_use
								if (!found) {
									for (let i = children.length - 1; i >= 0; i--) {
										if (children[i].type === 'tool_use' && children[i].toolStatus === 'running') {
											children[i] = {
												...children[i],
												toolResult: output,
												toolStatus: 'complete'
											};
											break;
										}
									}
								}
								return { ...m, agentChildren: children };
							}
							return m;
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_chunk': {
				// Text chunk from subagent - add to children or update last text child
				const agentId = data.agent_id as string;
				const content = data.content as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								const lastChild = children[children.length - 1];

								if (lastChild && lastChild.type === 'text') {
									// Append to existing text child
									children[children.length - 1] = {
										...lastChild,
										content: lastChild.content + content
									};
								} else {
									// Create new text child
									children.push({
										id: `${agentId}-text-${Date.now()}`,
										type: 'text',
										content: content
									});
								}
								return { ...m, agentChildren: children };
							}
							return m;
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_done': {
				// Subagent completed - update status and store final result
				const agentId = data.agent_id as string;
				const result = data.result as string | undefined;
				const isError = data.is_error as boolean | undefined;

				console.log('[WS] Subagent done:', agentId, isError ? 'error' : 'success');

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						// Find the subagent to get its linked toolId
						const subagent = tab.messages.find(m => m.type === 'subagent' && m.agentId === agentId);
						const linkedToolId = subagent?.toolId;

						const messages = tab.messages.map(m => {
							// Update the subagent message
							if (m.type === 'subagent' && m.agentId === agentId) {
								return {
									...m,
									content: result || '',
									agentStatus: isError ? 'error' as const : 'completed' as const,
									streaming: false
								};
							}
							// Also mark the linked Task tool_use as complete
							if (linkedToolId && m.type === 'tool_use' && m.toolId === linkedToolId) {
								return {
									...m,
									toolStatus: 'complete' as const,
									streaming: false
								};
							}
							return m;
						});

						// DON'T add empty streaming placeholder here
						// The stream_delta handler will create a message when text actually arrives
						// Adding empty placeholders causes lingering empty message boxes

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'permission_request': {
				// Tool permission request from backend - add to queue
				const request: PermissionRequestType = {
					request_id: data.request_id as string,
					tool_name: data.tool_name as string,
					tool_input: data.tool_input as Record<string, unknown>,
					queue_position: data.queue_position as number | undefined,
					queue_total: data.queue_total as number | undefined
				};

				console.log('[WS] Permission request:', request.tool_name, request.request_id);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Add to pending permissions queue
						const pendingPermissions = [...tab.pendingPermissions, request];
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'permission_queue_update': {
				// Some permissions were auto-resolved (e.g., by "Allow Always")
				const resolvedIds = data.resolved_ids as string[];
				const remainingCount = data.remaining_count as number;

				console.log('[WS] Permission queue update:', resolvedIds.length, 'resolved,', remainingCount, 'remaining');

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Remove resolved permissions from queue
						const pendingPermissions = tab.pendingPermissions.filter(
							p => !resolvedIds.includes(p.request_id)
						);
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'permission_response_ack': {
				// Acknowledgement that permission response was processed
				const requestId = data.request_id as string;
				const result = data.result as Record<string, unknown>;

				console.log('[WS] Permission response acknowledged:', requestId, result);

				// Remove this permission from the queue
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						const pendingPermissions = tab.pendingPermissions.filter(
							p => p.request_id !== requestId
						);
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'pending_permissions': {
				// Response to get_pending_permissions request - sync queue state
				const requests = data.requests as PermissionRequestType[];

				console.log('[WS] Pending permissions sync:', requests.length, 'requests');

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, pendingPermissions: requests };
					})
				}));
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
		 * Initialize - load tabs from server and connect WebSockets
		 */
		async init() {
			isInitializing = true;

			try {
				// Try to load persisted tabs from server
				const persistedState = await loadTabsFromServer();

				if (persistedState && persistedState.tabs && persistedState.tabs.length > 0) {
					// Restore tabs from server
					const restoredTabs: ChatTab[] = persistedState.tabs.map(pt => ({
						id: pt.id,
						title: pt.title,
						sessionId: pt.sessionId,
						messages: [],
						isStreaming: false,
						wsConnected: false,
						error: null,
						profile: pt.profile,
						project: pt.project,
						totalTokensIn: 0,
						totalTokensOut: 0,
						totalCacheCreationTokens: 0,
						totalCacheReadTokens: 0,
						contextUsed: null,
						contextMax: 200000,
						modelOverride: null,
						permissionModeOverride: null,
			pendingPermissions: []
					}));

					update(s => ({
						...s,
						tabs: restoredTabs,
						activeTabId: persistedState.activeTabId || restoredTabs[0]?.id || null
					}));

					// Connect WebSockets for each tab
					// Note: We rely on WebSocket load_session (sent in ws.onopen) to load messages
					// This ensures we get streaming state and buffer for active sessions
					// DO NOT call loadSessionInTab here - it uses REST which doesn't have streaming info
					for (const tab of restoredTabs) {
						connectTab(tab.id);
					}

					console.log('[Tabs] Restored', restoredTabs.length, 'tabs from server');
				} else {
					// No persisted state - connect default tab
					const state = get({ subscribe });
					for (const tab of state.tabs) {
						connectTab(tab.id);
					}
				}
			} catch (error) {
				console.error('[Tabs] Failed to initialize from server:', error);
				// Fall back to default behavior
				const state = get({ subscribe });
				for (const tab of state.tabs) {
					connectTab(tab.id);
				}
			} finally {
				isInitializing = false;
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
			const newTabId = generateTabId();

			const newTab: ChatTab = {
				id: newTabId,
				title: 'New Chat',
				sessionId: sessionId || null,
				messages: [],
				isStreaming: false,
				wsConnected: false,
				error: null,
				profile: getPersistedProfile(),
				project: getPersistedProject(),
				totalTokensIn: 0,
				totalTokensOut: 0,
				totalCacheCreationTokens: 0,
				totalCacheReadTokens: 0,
				contextUsed: null,
				contextMax: 200000,
				modelOverride: null,
				permissionModeOverride: null,
			pendingPermissions: []
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
			} else {
				// Save tabs state (debounced)
				saveTabsToServer(get({ subscribe }));
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

			// Send close_session message before disconnecting (so other devices know)
			const tab = state.tabs.find(t => t.id === tabId);
			if (tab?.sessionId) {
				const ws = tabConnections.get(tabId);
				if (ws && ws.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'close_session' }));
				}
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

			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Switch to a tab
		 */
		setActiveTab(tabId: string) {
			update(s => ({ ...s, activeTabId: tabId }));
			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Find a tab that has a specific session loaded
		 */
		findTabBySessionId(sessionId: string): string | null {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.sessionId === sessionId);
			return tab?.id ?? null;
		},

		/**
		 * Open a session - switches to existing tab if already open, otherwise creates new tab
		 */
		openSession(sessionId: string) {
			const existingTabId = this.findTabBySessionId(sessionId);
			if (existingTabId) {
				// Session already open - switch to that tab
				this.setActiveTab(existingTabId);
				return existingTabId;
			} else {
				// Create new tab with this session
				return this.createTab(sessionId);
			}
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

			// Build overrides object only if there are actual overrides
			const overrides: Record<string, string> = {};
			if (tab.modelOverride) {
				overrides.model = tab.modelOverride;
			}
			if (tab.permissionModeOverride) {
				overrides.permission_mode = tab.permissionModeOverride;
			}

			// Send query
			ws.send(JSON.stringify({
				type: 'query',
				prompt,
				session_id: tab.sessionId,
				profile: tab.profile,
				project: tab.project || undefined,
				overrides: Object.keys(overrides).length > 0 ? overrides : undefined
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
				// Debug: log subagent messages from API response
				const subagentMsgs = session.messages.filter(m => m.type === 'subagent');
				if (subagentMsgs.length > 0) {
					console.log('[tabs] Subagent messages from API:', subagentMsgs.map(m => ({
						type: m.type,
						agentId: m.agentId,
						agentStatus: m.agentStatus,
						agentChildrenCount: (m.agentChildren as unknown[])?.length || 0
					})));
				}
				const messages: ChatMessage[] = session.messages.map((m, i) => {
					// Determine message type - JSONL messages have explicit 'type' field
					let msgType: MessageType | undefined = m.type as MessageType | undefined;

					// Legacy DB format: infer type from role
					if (!msgType && m.role === 'assistant') {
						msgType = 'text';
					} else if (!msgType && (m.role === 'tool_use' || m.role === 'tool_result')) {
						msgType = m.role as MessageType;
					}

					// Handle system messages (e.g., /context, /compact output)
					if (msgType === 'system' || m.role === 'system') {
						return {
							id: String(m.id || `msg-${i}`),
							role: 'system' as const,
							content: m.content as string,
							type: 'system' as const,
							systemSubtype: m.subtype as string | undefined,
							systemData: { content: m.content } as Record<string, unknown>,
							metadata: m.metadata as Record<string, unknown>,
							streaming: false
						};
					}

					// Build the chat message with all fields
					const chatMessage: ChatMessage = {
						id: String(m.id || `msg-${i}`),
						role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
						content: m.content as string,
						type: msgType,
						toolName: (m.toolName || m.tool_name) as string | undefined,
						toolId: m.toolId as string | undefined,
						toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
						toolResult: m.toolResult as string | undefined,
						toolStatus: m.toolStatus as 'running' | 'complete' | 'error' | undefined,
						metadata: m.metadata as Record<string, unknown>,
						streaming: false
					};

					// Add subagent-specific fields if present
					if (msgType === 'subagent') {
						chatMessage.agentId = m.agentId as string | undefined;
						chatMessage.agentType = m.agentType as string | undefined;
						chatMessage.agentDescription = m.agentDescription as string | undefined;
						chatMessage.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error' | undefined;
						chatMessage.agentChildren = m.agentChildren as SubagentChildMessage[] | undefined;
					}

					return chatMessage;
				});

				// Generate title from first user message
				let title = 'Chat';
				const firstUserMsg = messages.find(m => m.role === 'user');
				if (firstUserMsg) {
					title = firstUserMsg.content.substring(0, 30) + (firstUserMsg.content.length > 30 ? '...' : '');
				}

				// Load token totals from session history - single source of truth
				// Also restore profile and project from the saved session (they are locked once saved)
				updateTab(tabId, {
					sessionId: session.id,
					messages,
					title,
					error: null,
					totalTokensIn: session.total_tokens_in || 0,
					totalTokensOut: session.total_tokens_out || 0,
					totalCacheCreationTokens: session.cache_creation_tokens || 0,
					totalCacheReadTokens: session.cache_read_tokens || 0,
					profile: session.profile_id,
					project: session.project_id || ''
				});

				// Save tabs state (debounced)
				saveTabsToServer(get({ subscribe }));

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
		 * Set model override for a tab (null = use profile default)
		 */
		setTabModelOverride(tabId: string, model: string | null) {
			updateTab(tabId, { modelOverride: model });
		},

		/**
		 * Set permission mode override for a tab (null = use profile default)
		 */
		setTabPermissionModeOverride(tabId: string, permissionMode: string | null) {
			updateTab(tabId, { permissionModeOverride: permissionMode });
		},

		/**
		 * Set error for a tab
		 */
		setTabError(tabId: string, error: string) {
			updateTab(tabId, { error });
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
				title: 'New Chat',
				totalTokensIn: 0,
				totalTokensOut: 0,
				totalCacheCreationTokens: 0,
				totalCacheReadTokens: 0,
				contextUsed: null,
				contextMax: 200000,
				modelOverride: null,
				permissionModeOverride: null,
			pendingPermissions: []
			});
			connectTab(tabId);
			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
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
		},

		/**
		 * Send permission response for a tool request
		 */
		sendPermissionResponse(
			tabId: string,
			requestId: string,
			decision: 'allow' | 'deny',
			remember?: 'none' | 'session' | 'profile',
			pattern?: string
		) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({
					type: 'permission_response',
					request_id: requestId,
					decision,
					remember: remember || 'none',
					pattern
				}));
				console.log(`[Tab ${tabId}] Sent permission response: ${decision} for ${requestId}`);
			} else {
				console.error(`[Tab ${tabId}] Cannot send permission response: WebSocket not connected`);
			}
		},

		/**
		 * Get pending permissions for a tab
		 */
		getPendingPermissions(tabId: string): PermissionRequestType[] {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.id === tabId);
			return tab?.pendingPermissions || [];
		},

		/**
		 * Request sync of pending permissions from server
		 */
		syncPendingPermissions(tabId: string) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'get_pending_permissions' }));
			}
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
// Selection state derived stores
export const selectedSessionIds = derived(tabs, $tabs => $tabs.selectedSessionIds);
export const selectedAdminSessionIds = derived(tabs, $tabs => $tabs.selectedAdminSessionIds);
export const selectionMode = derived(tabs, $tabs => $tabs.selectionMode);
export const adminSelectionMode = derived(tabs, $tabs => $tabs.adminSelectionMode);
// Loading state
export const sessionsLoading = derived(tabs, $tabs => $tabs.sessionsLoading);
