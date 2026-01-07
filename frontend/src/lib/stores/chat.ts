/**
 * Chat/conversation store with SSE streaming support
 * Includes sessions, profiles, and projects management
 * With cross-device synchronization support
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, SessionMessage, Profile } from '$lib/api/client';
import { api } from '$lib/api/client';
import { sync, type SyncEvent } from './sync';
import { getDeviceIdSync } from './device';

export type MessageType = 'text' | 'tool_use' | 'tool_result';

export interface ChatMessage {
	id: string;
	role: 'user' | 'assistant' | 'system';
	content: string;
	type?: MessageType;
	toolName?: string;
	toolId?: string; // Unique ID for matching tool_use with tool_result
	toolInput?: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	streaming?: boolean;
	isLastInGroup?: boolean; // Marks the last message in an assistant response group
}

export interface ToolUse {
	name: string;
	input: Record<string, unknown>;
	output?: string;
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
	activeSessionIds: Set<string>; // Sessions currently streaming
	isStreaming: boolean;
	isRemoteStreaming: boolean; // True when another device is streaming
	error: string | null;
}

function createChatStore() {
	const { subscribe, set, update } = writable<ChatState>({
		sessionId: null,
		messages: [],
		profiles: [],
		selectedProfile: 'claude-code',
		projects: [],
		selectedProject: '',
		sessions: [],
		activeSessionIds: new Set(),
		isStreaming: false,
		isRemoteStreaming: false,
		error: null
	});
	let syncUnsubscribe: (() => void) | null = null;
	let remoteMsgId: string | null = null; // Track remote streaming message ID

	/**
	 * Handle sync events from other devices
	 */
	function handleSyncEvent(event: SyncEvent) {
		const state = get({ subscribe });

		console.log('[Chat] handleSyncEvent:', event.event_type, 'session:', event.session_id, 'current:', state.sessionId);

		// Only process events for the current session
		if (event.session_id !== state.sessionId) {
			console.log('[Chat] Ignoring event for different session');
			return;
		}

		switch (event.event_type) {
			case 'message_added': {
				// Another device added a message (user message)
				const message = event.data.message as Record<string, unknown>;
				if (message) {
					const newMsg: ChatMessage = {
						id: `sync-${message.id}`,
						role: message.role as 'user' | 'assistant',
						content: message.content as string,
						metadata: message.metadata as Record<string, unknown>
					};
					update((s) => ({
						...s,
						messages: [...s.messages, newMsg]
					}));
				}
				break;
			}

			case 'stream_start': {
				// Streaming started (either local or remote)
				remoteMsgId = event.data.message_id as string;
				const placeholderMsg: ChatMessage = {
					id: remoteMsgId,
					role: 'assistant',
					content: '',
					type: 'text',
					streaming: true
				};
				update((s) => {
					// Check if we already have a streaming message (avoid duplicates)
					const hasStreamingMsg = s.messages.some(m => m.streaming && m.role === 'assistant');
					if (hasStreamingMsg) {
						// Already have a streaming message, just update streaming state
						return { ...s, isStreaming: true };
					}
					return {
						...s,
						isStreaming: true,
						messages: [...s.messages, placeholderMsg]
					};
				});
				break;
			}

			case 'stream_chunk': {
				// Streaming chunk (from background task or another device)
				const chunkType = event.data.chunk_type as string;
				console.log('[Chat] Received stream_chunk:', chunkType, event.data);

				update((s) => {
					const messages = [...s.messages];

					switch (chunkType) {
						case 'text': {
							// Mark any streaming tool_use messages as complete when we receive text
							// This handles the case where backend doesn't send tool_result events
							for (let i = 0; i < messages.length; i++) {
								if (messages[i].type === 'tool_use' && messages[i].streaming) {
									messages[i] = { ...messages[i], streaming: false };
								}
							}

							// Find the LAST streaming text message
							const lastStreamingTextIndex = messages.findLastIndex(
								(m) => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (lastStreamingTextIndex !== -1) {
								const msg = { ...messages[lastStreamingTextIndex] };
								msg.content += event.data.content as string;
								messages[lastStreamingTextIndex] = msg;
							} else {
								// No streaming text message found - create one
								console.warn('[Sync] No streaming text message found, creating new one');
								const newTextMsgId = `msg-sync-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
								messages.push({
									id: newTextMsgId,
									role: 'assistant',
									type: 'text',
									content: event.data.content as string,
									streaming: true
								});
							}
							break;
						}

						case 'tool_use': {
							// Find and mark current streaming text message as not streaming
							const currentTextIndex = messages.findLastIndex(
								(m) => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (currentTextIndex !== -1 && messages[currentTextIndex].content) {
								messages[currentTextIndex] = {
									...messages[currentTextIndex],
									streaming: false
								};
							}

							// Add a new message for the tool use
							const toolMsgId = `tool-sync-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
							const toolMessage: ChatMessage = {
								id: toolMsgId,
								role: 'assistant',
								type: 'tool_use',
								content: '',
								toolName: event.data.name as string,
								toolId: event.data.id as string, // Store the tool_use ID
								toolInput: event.data.input as Record<string, unknown>,
								streaming: true
							};
							messages.push(toolMessage);
							break;
						}

						case 'tool_result': {
							// Find the tool_use message by tool_use_id (preferred) or by name (fallback)
							const toolUseId = event.data.tool_use_id as string;
							let toolUseIndex = -1;

							if (toolUseId) {
								toolUseIndex = messages.findLastIndex(
									(m) => m.type === 'tool_use' && m.toolId === toolUseId
								);
							}

							if (toolUseIndex === -1) {
								toolUseIndex = messages.findLastIndex(
									(m) => m.type === 'tool_use' && m.toolName === event.data.name && m.streaming
								);
							}

							if (toolUseIndex !== -1) {
								messages[toolUseIndex] = {
									...messages[toolUseIndex],
									streaming: false
								};
							}

							// Add a new message for the tool result
							const resultMsgId = `result-sync-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
							const resultMessage: ChatMessage = {
								id: resultMsgId,
								role: 'assistant',
								type: 'tool_result',
								content: event.data.output as string,
								toolName: event.data.name as string,
								toolId: toolUseId,
								streaming: false
							};
							messages.push(resultMessage);

							// Add a new text message placeholder for any following text
							const newTextMsgId = `msg-sync-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
							const newTextMessage: ChatMessage = {
								id: newTextMsgId,
								role: 'assistant',
								type: 'text',
								content: '',
								streaming: true
							};
							messages.push(newTextMessage);
							break;
						}
					}

					return { ...s, messages };
				});
				break;
			}

			case 'stream_end': {
				// Streaming completed (local or remote)
				const metadata = event.data.metadata as Record<string, unknown>;
				const interrupted = event.data.interrupted as boolean;

				update((s) => {
					// Mark all streaming messages as done
					const finalMessages = s.messages.map(m => {
						if (m.streaming) {
							return { ...m, streaming: false };
						}
						return m;
					});

					// Remove empty text messages
					const cleanedMessages = finalMessages.filter(
						m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
					);

					// Add metadata to the last assistant message
					if (cleanedMessages.length > 0) {
						const lastAssistantIndex = cleanedMessages.findLastIndex(m => m.role === 'assistant');
						if (lastAssistantIndex !== -1) {
							cleanedMessages[lastAssistantIndex] = {
								...cleanedMessages[lastAssistantIndex],
								metadata,
								content: cleanedMessages[lastAssistantIndex].content + (interrupted ? '\n\n[Interrupted]' : ''),
								isLastInGroup: true
							};
						}
					}

					return {
						...s,
						messages: cleanedMessages,
						isStreaming: false,
						isRemoteStreaming: false
					};
				});

				remoteMsgId = null;
				break;
			}

			case 'session_updated': {
				// Session metadata changed - reload sessions list
				// This is non-critical, so we just refresh in background
				api.get<Session[]>('/sessions?limit=50')
					.then((sessions) => update((s) => ({ ...s, sessions })))
					.catch(console.error);
				break;
			}

			case 'state': {
				// State event from WebSocket connection (received on connect/reconnect)
				// This tells us if the session is currently streaming on another device
				const isStreaming = event.data.is_streaming as boolean;
				const streamingMessages = event.data.streaming_messages as Array<{
					type: string;
					role: string;
					content: string;
					tool_name?: string;
					tool_id?: string;
					tool_input?: Record<string, unknown>;
					streaming?: boolean;
				}> | undefined;

				console.log('[Chat] State event: is_streaming=', isStreaming, 'buffered messages:', streamingMessages?.length || 0);

				update((s) => {
					// If session is streaming and we have buffered messages, merge them
					if (isStreaming && streamingMessages && streamingMessages.length > 0) {
						const messages = [...s.messages];

						// Find existing streaming assistant message
						const streamingMsgIndex = messages.findIndex(m => m.streaming && m.role === 'assistant');

						if (streamingMsgIndex !== -1) {
							// Merge buffered content into existing streaming message
							console.log('[Chat] Merging buffered content into existing streaming message');
							for (const sm of streamingMessages) {
								if (sm.type === 'text' && sm.content) {
									// Append text content
									messages[streamingMsgIndex] = {
										...messages[streamingMsgIndex],
										content: (messages[streamingMsgIndex].content || '') + sm.content
									};
								} else if (sm.type === 'tool_use') {
									// Add tool use message
									messages.push({
										id: `tool-${Date.now()}-${sm.tool_id}`,
										role: 'assistant' as const,
										content: '',
										type: 'tool_use' as const,
										toolName: sm.tool_name,
										toolId: sm.tool_id,
										toolInput: sm.tool_input,
										streaming: sm.streaming ?? true
									});
								}
							}
							return {
								...s,
								messages,
								isStreaming: true,
								isRemoteStreaming: isStreaming
							};
						} else {
							// No existing streaming message - create new ones from buffer
							console.log('[Chat] No streaming message found, creating from buffer');
							const newMsgs = streamingMessages.map((sm, i) => ({
								id: `stream-${Date.now()}-${i}`,
								role: sm.role as 'user' | 'assistant',
								content: sm.content || '',
								type: sm.type as MessageType,
								toolName: sm.tool_name,
								toolId: sm.tool_id,
								toolInput: sm.tool_input,
								streaming: sm.streaming ?? true
							}));
							return {
								...s,
								messages: [...s.messages, ...newMsgs],
								isStreaming: true,
								isRemoteStreaming: isStreaming
							};
						}
					}

					// Don't change isStreaming if we're already streaming locally
					return { ...s, isRemoteStreaming: isStreaming };
				});
				break;
			}
		}
	}

	/**
	 * Connect to sync for a session
	 */
	async function connectSync(sessionId: string) {
		// Unsubscribe from previous handler
		if (syncUnsubscribe) {
			syncUnsubscribe();
		}

		// Subscribe to sync events
		syncUnsubscribe = sync.onEvent(handleSyncEvent);

		// Connect WebSocket
		await sync.connect(sessionId);
	}

	/**
	 * Disconnect from sync
	 */
	function disconnectSync() {
		if (syncUnsubscribe) {
			syncUnsubscribe();
			syncUnsubscribe = null;
		}
		sync.disconnect();
	}

	return {
		subscribe,

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
			try {
				const sessions = await api.get<Session[]>('/sessions?limit=50');
				update(s => ({ ...s, sessions }));
			} catch (e) {
				console.error('Failed to load sessions:', e);
			}
		},

		async loadActiveSessions() {
			try {
				const response = await api.get<{ active_sessions: string[] }>('/streaming/active');
				const activeIds = new Set(response.active_sessions || []);
				update(s => ({ ...s, activeSessionIds: activeIds }));
			} catch (e) {
				console.error('Failed to load active sessions:', e);
			}
		},

		async loadSession(sessionId: string): Promise<boolean> {
			try {
				const session = await api.get<Session & { messages: SessionMessage[] }>(`/sessions/${sessionId}`);
				const messages: ChatMessage[] = session.messages.map((m, i) => ({
					id: `msg-${i}`,
					role: m.role as 'user' | 'assistant',
					content: m.content,
					type: m.role === 'assistant' ? 'text' : undefined, // Set type for assistant messages
					metadata: m.metadata,
					streaming: false
				}));
				// Don't change selectedProfile when resuming - keep user's current selection
				update(s => ({
					...s,
					sessionId: session.id,
					messages,
					error: null // Clear any previous errors on successful load
				}));

				// Connect to sync for real-time updates from other devices
				// This is non-critical - session should load even if sync fails
				try {
					await connectSync(sessionId);
				} catch (syncError) {
					console.warn('[Chat] Failed to connect sync, session loaded without real-time updates:', syncError);
				}
				return true;
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to load session' }));
				return false;
			}
		},

		setProfile(profileId: string) {
			update(s => ({ ...s, selectedProfile: profileId }));
		},

		setProject(projectId: string) {
			update(s => ({ ...s, selectedProject: projectId }));
		},

		startNewChat() {
			// Disconnect from sync when starting a new chat
			disconnectSync();

			update(s => ({
				...s,
				sessionId: null,
				messages: [],
				isRemoteStreaming: false,
				error: null
			}));
		},

		async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
			try {
				await api.post('/profiles', data);
				await this.loadProfiles();
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to create profile' }));
			}
		},

		async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
			try {
				await api.put(`/profiles/${profileId}`, data);
				await this.loadProfiles();
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to update profile' }));
			}
		},

		async deleteProfile(profileId: string) {
			try {
				await api.delete(`/profiles/${profileId}`);
				await this.loadProfiles();
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to delete profile' }));
			}
		},

		async createProject(data: { id: string; name: string; description?: string }) {
			try {
				await api.post('/projects', data);
				await this.loadProjects();
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to create project' }));
			}
		},

		async deleteProject(projectId: string) {
			try {
				await api.delete(`/projects/${projectId}`);
				await this.loadProjects();
				// Reset selected project if it was the deleted one
				update(s => ({
					...s,
					selectedProject: s.selectedProject === projectId ? '' : s.selectedProject
				}));
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to delete project' }));
			}
		},

		async sendMessage(prompt: string) {
			// Add user message immediately for responsive UI
			const userMsgId = `msg-${Date.now()}`;
			const userMessage: ChatMessage = {
				id: userMsgId,
				role: 'user',
				content: prompt
			};

			// Capture current state values
			let currentSessionId: string | null = null;
			let currentProfile: string = 'claude-code';
			let currentProject: string = '';

			update(s => {
				currentSessionId = s.sessionId;
				currentProfile = s.selectedProfile;
				currentProject = s.selectedProject;
				return {
					...s,
					// Only add user message - assistant placeholder will come from stream_start event
					messages: [...s.messages, userMessage],
					isStreaming: true,
					error: null
				};
			});

			// Get device ID for cross-device sync
			const deviceId = getDeviceIdSync();

			// Build request
			const body: Record<string, unknown> = {
				prompt,
				profile: currentProfile,
				device_id: deviceId
			};

			if (currentSessionId) {
				body.session_id = currentSessionId;
			}

			if (currentProject) {
				body.project = currentProject;
			}

			try {
				// Start background query - returns immediately
				// Streaming events come via WebSocket, not HTTP
				const response = await fetch('/api/v1/conversation/start', {
					method: 'POST',
					headers: {
						'Content-Type': 'application/json'
					},
					credentials: 'include',
					body: JSON.stringify(body)
				});

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Failed to start conversation');
				}

				const result = await response.json();
				const newSessionId = result.session_id;
				const streamingMsgId = result.message_id;

				// Update session ID and add assistant placeholder immediately
				// This ensures we have a streaming message ready for chunks
				update(s => ({
					...s,
					sessionId: newSessionId,
					messages: [...s.messages, {
						id: streamingMsgId,
						role: 'assistant' as const,
						content: '',
						type: 'text' as const,
						streaming: true
					}]
				}));

				// Connect to WebSocket for streaming updates
				// The sync handler will receive stream_chunk, stream_end events
				await connectSync(newSessionId);

				// Reload sessions list to show new session
				await this.loadSessions();

			} catch (e: any) {
				// Remove the user message on error
				update(s => ({
					...s,
					messages: s.messages.filter(m => m.id !== userMsgId),
					isStreaming: false,
					error: e.message || 'Failed to send message'
				}));
			}
		},

		async stopGeneration() {
			const state = get({ subscribe });

			// Call the server-side interrupt to stop the background task
			if (state.sessionId) {
				try {
					const response = await fetch(`/api/v1/session/${state.sessionId}/interrupt`, {
						method: 'POST',
						credentials: 'include'
					});

					if (!response.ok) {
						console.warn('Interrupt request failed:', response.status);
					}
				} catch (e) {
					console.error('Failed to interrupt session:', e);
				}
			}

			// Update local state - the stream_end event from WebSocket will also update this
			// but we do it immediately for responsive UI
			update(s => ({
				...s,
				isStreaming: false,
				isRemoteStreaming: false
			}));
		},

		handleStreamEvent(event: Record<string, unknown>, msgId: string) {
			// Handle init event specially - connect to sync for new session
			if (event.type === 'init') {
				const sessionId = event.session_id as string;
				update(s => ({ ...s, sessionId }));
				// Connect to sync for this new session
				connectSync(sessionId).catch(console.error);
				return;
			}

			update(s => {
				const messages = [...s.messages];

				switch (event.type) {
					case 'text': {
						// Mark any streaming tool_use messages as complete when we receive text
						// This handles the case where backend doesn't send tool_result events
						for (let i = 0; i < messages.length; i++) {
							if (messages[i].type === 'tool_use' && messages[i].streaming) {
								messages[i] = { ...messages[i], streaming: false };
							}
						}

						// Find the LAST streaming text message (not the original msgId)
						// This ensures text goes to the correct message after tool results
						const lastStreamingTextIndex = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);

						if (lastStreamingTextIndex !== -1) {
							const msg = { ...messages[lastStreamingTextIndex] };
							msg.content += event.content as string;
							messages[lastStreamingTextIndex] = msg;
						} else {
							// No streaming text message found - create one
							// This can happen if we receive text without a prior placeholder
							console.warn('[Chat] No streaming text message found, creating new one');
							const newTextMsgId = `msg-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
							messages.push({
								id: newTextMsgId,
								role: 'assistant',
								type: 'text',
								content: event.content as string,
								streaming: true
							});
						}
						break;
					}

					case 'tool_use': {
						// Find and mark current streaming text message as not streaming
						const currentTextIndex = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);
						if (currentTextIndex !== -1 && messages[currentTextIndex].content) {
							messages[currentTextIndex] = {
								...messages[currentTextIndex],
								streaming: false
							};
						}

						// Add a new message for the tool use
						const toolMsgId = `tool-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
						const toolMessage: ChatMessage = {
							id: toolMsgId,
							role: 'assistant',
							type: 'tool_use',
							content: '',
							toolName: event.name as string,
							toolId: event.id as string, // Store the tool_use ID for matching with result
							toolInput: event.input as Record<string, unknown>,
							streaming: true
						};
						messages.push(toolMessage);
						break;
					}

					case 'tool_result': {
						// Find the tool_use message by tool_use_id (preferred) or by name (fallback)
						const toolUseId = event.tool_use_id as string;
						let toolUseIndex = -1;

						if (toolUseId) {
							// Match by tool_use_id (most reliable)
							toolUseIndex = messages.findLastIndex(
								m => m.type === 'tool_use' && m.toolId === toolUseId
							);
						}

						if (toolUseIndex === -1) {
							// Fallback: match by name and streaming status
							toolUseIndex = messages.findLastIndex(
								m => m.type === 'tool_use' && m.toolName === event.name && m.streaming
							);
						}

						if (toolUseIndex !== -1) {
							messages[toolUseIndex] = {
								...messages[toolUseIndex],
								streaming: false
							};
						}

						// Add a new message for the tool result
						const resultMsgId = `result-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
						const resultMessage: ChatMessage = {
							id: resultMsgId,
							role: 'assistant',
							type: 'tool_result',
							content: event.output as string,
							toolName: event.name as string,
							toolId: toolUseId, // Store for reference
							streaming: false
						};
						messages.push(resultMessage);

						// Add a new text message placeholder for any following text
						const newTextMsgId = `msg-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
						const newTextMessage: ChatMessage = {
							id: newTextMsgId,
							role: 'assistant',
							type: 'text',
							content: '',
							streaming: true
						};
						messages.push(newTextMessage);
						break;
					}

					case 'done': {
						// Mark all streaming messages as done and add metadata to last message
						const finalMessages = messages.map((m, i) => {
							if (m.streaming) {
								return { ...m, streaming: false };
							}
							return m;
						});

						// Remove empty text messages
						const cleanedMessages = finalMessages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						// Add metadata to the last assistant message
						if (cleanedMessages.length > 0) {
							const lastAssistantIndex = cleanedMessages.findLastIndex(m => m.role === 'assistant');
							if (lastAssistantIndex !== -1) {
								cleanedMessages[lastAssistantIndex] = {
									...cleanedMessages[lastAssistantIndex],
									metadata: event.metadata as Record<string, unknown>,
									isLastInGroup: true
								};
							}
						}

						return {
							...s,
							messages: cleanedMessages,
							isStreaming: false
						};
					}

					case 'interrupted': {
						// Mark all streaming messages as done
						const finalMessages = messages.map(m => {
							if (m.streaming) {
								return { ...m, streaming: false };
							}
							return m;
						});

						// Remove empty text messages
						const cleanedMessages = finalMessages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						// Add interrupted notice to last message
						if (cleanedMessages.length > 0) {
							const lastIndex = cleanedMessages.length - 1;
							if (cleanedMessages[lastIndex].role === 'assistant') {
								cleanedMessages[lastIndex] = {
									...cleanedMessages[lastIndex],
									content: cleanedMessages[lastIndex].content + '\n\n[Interrupted]',
									isLastInGroup: true
								};
							}
						}

						return {
							...s,
							messages: cleanedMessages,
							isStreaming: false
						};
					}

					case 'error': {
						// Mark all streaming messages as done
						const finalMessages = messages.map(m => {
							if (m.streaming) {
								return { ...m, streaming: false };
							}
							return m;
						});

						// Remove empty text messages
						const cleanedMessages = finalMessages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						return {
							...s,
							messages: cleanedMessages,
							isStreaming: false,
							error: event.message as string
						};
					}
				}

				return { ...s, messages };
			});
		},

		clearError() {
			update(s => ({ ...s, error: null }));
		}
	};
}

export const chat = createChatStore();

// Derived stores
export const messages = derived(chat, $chat => $chat.messages);
export const isStreaming = derived(chat, $chat => $chat.isStreaming);
export const isRemoteStreaming = derived(chat, $chat => $chat.isRemoteStreaming);
export const isAnyStreaming = derived(chat, $chat => $chat.isStreaming || $chat.isRemoteStreaming);
export const chatError = derived(chat, $chat => $chat.error);
export const profiles = derived(chat, $chat => $chat.profiles);
export const selectedProfile = derived(chat, $chat => $chat.selectedProfile);
export const projects = derived(chat, $chat => $chat.projects);
export const selectedProject = derived(chat, $chat => $chat.selectedProject);
export const sessions = derived(chat, $chat => $chat.sessions);
export const currentSessionId = derived(chat, $chat => $chat.sessionId);
export const activeSessionIds = derived(chat, $chat => $chat.activeSessionIds);
