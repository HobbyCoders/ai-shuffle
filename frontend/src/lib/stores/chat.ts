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
	isStreaming: boolean;
	isRemoteStreaming: boolean; // True when another device is streaming
	error: string | null;
	abortController: AbortController | null;
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
		isStreaming: false,
		isRemoteStreaming: false,
		error: null,
		abortController: null
	});

	let currentEventSource: EventSource | null = null;
	let syncUnsubscribe: (() => void) | null = null;
	let remoteMsgId: string | null = null; // Track remote streaming message ID

	/**
	 * Handle sync events from other devices
	 */
	function handleSyncEvent(event: SyncEvent) {
		const state = get({ subscribe });

		// Only process events for the current session
		if (event.session_id !== state.sessionId) {
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
				// Another device started streaming
				remoteMsgId = event.data.message_id as string;
				const placeholderMsg: ChatMessage = {
					id: remoteMsgId,
					role: 'assistant',
					content: '',
					type: 'text',
					streaming: true
				};
				update((s) => ({
					...s,
					isRemoteStreaming: true,
					messages: [...s.messages, placeholderMsg]
				}));
				break;
			}

			case 'stream_chunk': {
				// Streaming chunk from another device
				const chunkType = event.data.chunk_type as string;

				update((s) => {
					const messages = [...s.messages];

					switch (chunkType) {
						case 'text': {
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
				// Streaming completed on another device
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
				// Initial state from WebSocket connection
				const isStreaming = event.data.is_streaming as boolean;
				const messages = event.data.messages as SessionMessage[];

				if (messages && messages.length > 0) {
					const chatMessages: ChatMessage[] = messages.map((m, i) => ({
						id: `msg-${i}`,
						role: m.role as 'user' | 'assistant',
						content: m.content,
						metadata: m.metadata
					}));

					update((s) => ({
						...s,
						messages: chatMessages,
						isRemoteStreaming: isStreaming
					}));
				}
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

		async loadSession(sessionId: string) {
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
					messages
				}));

				// Connect to sync for real-time updates from other devices
				await connectSync(sessionId);
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to load session' }));
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
			// Create abort controller for this request
			const abortController = new AbortController();

			// Add user message
			const userMsgId = `msg-${Date.now()}`;
			const userMessage: ChatMessage = {
				id: userMsgId,
				role: 'user',
				content: prompt
			};

			// Add placeholder for assistant response (text message)
			const assistantMsgId = `msg-${Date.now() + 1}`;
			const assistantMessage: ChatMessage = {
				id: assistantMsgId,
				role: 'assistant',
				content: '',
				type: 'text',
				streaming: true
			};

			// Capture current state values AFTER updating UI
			// This ensures we get the latest sessionId even if loadSession just completed
			let currentSessionId: string | null = null;
			let currentProfile: string = 'claude-code';
			let currentProject: string = '';

			update(s => {
				// Capture current values from store during update
				currentSessionId = s.sessionId;
				currentProfile = s.selectedProfile;
				currentProject = s.selectedProject;
				return {
					...s,
					messages: [...s.messages, userMessage, assistantMessage],
					isStreaming: true,
					error: null,
					abortController
				};
			});

			// Get device ID for cross-device sync
			const deviceId = getDeviceIdSync();

			// Build request with captured values
			const body: Record<string, unknown> = {
				prompt,
				profile: currentProfile,
				device_id: deviceId // Include device ID for sync
			};

			if (currentSessionId) {
				body.session_id = currentSessionId;
			}

			if (currentProject) {
				body.project = currentProject;
			}

			try {
				// Use fetch for SSE
				const response = await fetch('/api/v1/conversation/stream', {
					method: 'POST',
					headers: {
						'Content-Type': 'application/json'
					},
					credentials: 'include',
					body: JSON.stringify(body),
					signal: abortController.signal
				});

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Stream request failed');
				}

				const reader = response.body?.getReader();
				const decoder = new TextDecoder();

				if (!reader) {
					throw new Error('No response body');
				}

				let buffer = '';

				while (true) {
					const { done, value } = await reader.read();
					if (done) break;

					buffer += decoder.decode(value, { stream: true });

					// Process SSE events
					const lines = buffer.split('\n');
					buffer = lines.pop() || '';

					for (const line of lines) {
						if (line.startsWith('data: ')) {
							const data = line.slice(6);
							try {
								const event = JSON.parse(data);
								this.handleStreamEvent(event, assistantMsgId);
							} catch (e) {
								console.error('Failed to parse SSE event:', data);
							}
						}
					}
				}

				// Reload sessions to update the list
				await this.loadSessions();

			} catch (e: any) {
				// Don't show error for intentional abort
				if (e.name === 'AbortError') {
					update(s => {
						const messages = [...s.messages];
						const msgIndex = messages.findIndex(m => m.id === assistantMsgId);
						if (msgIndex !== -1) {
							messages[msgIndex] = {
								...messages[msgIndex],
								streaming: false,
								content: messages[msgIndex].content + '\n\n[Stopped]'
							};
						}
						return {
							...s,
							messages,
							isStreaming: false,
							abortController: null
						};
					});
					return;
				}

				update(s => ({
					...s,
					isStreaming: false,
					error: e.message || 'Failed to send message',
					abortController: null
				}));
			}
		},

		async stopGeneration() {
			const state = get({ subscribe });

			// Abort the fetch request
			if (state.abortController) {
				state.abortController.abort();
			}

			// Also call the server-side interrupt if we have a session
			if (state.sessionId) {
				try {
					await fetch(`/api/v1/session/${state.sessionId}/interrupt`, {
						method: 'POST',
						credentials: 'include'
					});
				} catch (e) {
					console.error('Failed to interrupt session:', e);
				}
			}

			update(s => ({
				...s,
				isStreaming: false,
				abortController: null
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
							isStreaming: false,
							abortController: null
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
							isStreaming: false,
							abortController: null
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
							error: event.message as string,
							abortController: null
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
