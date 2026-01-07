/**
 * Chat/conversation store with SSE streaming support
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, SessionMessage, Profile } from '$lib/api/client';
import { api } from '$lib/api/client';

export interface ChatMessage {
	id: string;
	role: 'user' | 'assistant' | 'system';
	content: string;
	toolUses?: ToolUse[];
	metadata?: Record<string, unknown>;
	streaming?: boolean;
}

export interface ToolUse {
	name: string;
	input: Record<string, unknown>;
	output?: string;
}

interface ChatState {
	sessionId: string | null;
	messages: ChatMessage[];
	profiles: Profile[];
	selectedProfile: string;
	isStreaming: boolean;
	error: string | null;
}

function createChatStore() {
	const { subscribe, set, update } = writable<ChatState>({
		sessionId: null,
		messages: [],
		profiles: [],
		selectedProfile: 'claude-code',
		isStreaming: false,
		error: null
	});

	let currentEventSource: EventSource | null = null;

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

		async loadSession(sessionId: string) {
			try {
				const session = await api.get<Session & { messages: SessionMessage[] }>(`/sessions/${sessionId}`);
				const messages: ChatMessage[] = session.messages.map((m, i) => ({
					id: `msg-${i}`,
					role: m.role as 'user' | 'assistant',
					content: m.content,
					metadata: m.metadata
				}));
				update(s => ({
					...s,
					sessionId: session.id,
					messages,
					selectedProfile: session.profile_id
				}));
			} catch (e: any) {
				update(s => ({ ...s, error: e.detail || 'Failed to load session' }));
			}
		},

		setProfile(profileId: string) {
			update(s => ({ ...s, selectedProfile: profileId }));
		},

		startNewChat() {
			update(s => ({
				...s,
				sessionId: null,
				messages: [],
				error: null
			}));
		},

		async sendMessage(prompt: string) {
			const state = get({ subscribe });

			// Add user message
			const userMsgId = `msg-${Date.now()}`;
			const userMessage: ChatMessage = {
				id: userMsgId,
				role: 'user',
				content: prompt
			};

			// Add placeholder for assistant response
			const assistantMsgId = `msg-${Date.now() + 1}`;
			const assistantMessage: ChatMessage = {
				id: assistantMsgId,
				role: 'assistant',
				content: '',
				toolUses: [],
				streaming: true
			};

			update(s => ({
				...s,
				messages: [...s.messages, userMessage, assistantMessage],
				isStreaming: true,
				error: null
			}));

			// Build request
			const body: Record<string, unknown> = {
				prompt,
				profile: state.selectedProfile
			};

			if (state.sessionId) {
				body.session_id = state.sessionId;
			}

			try {
				// Use fetch for SSE
				const response = await fetch('/api/v1/conversation/stream', {
					method: 'POST',
					headers: {
						'Content-Type': 'application/json'
					},
					credentials: 'include',
					body: JSON.stringify(body)
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
			} catch (e: any) {
				update(s => ({
					...s,
					isStreaming: false,
					error: e.message || 'Failed to send message'
				}));
			}
		},

		handleStreamEvent(event: Record<string, unknown>, msgId: string) {
			update(s => {
				const messages = [...s.messages];
				const msgIndex = messages.findIndex(m => m.id === msgId);

				if (msgIndex === -1) return s;

				const msg = { ...messages[msgIndex] };

				switch (event.type) {
					case 'init':
						return { ...s, sessionId: event.session_id as string };

					case 'text':
						msg.content += event.content as string;
						break;

					case 'tool_use':
						msg.toolUses = msg.toolUses || [];
						msg.toolUses.push({
							name: event.name as string,
							input: event.input as Record<string, unknown>
						});
						break;

					case 'tool_result':
						if (msg.toolUses && msg.toolUses.length > 0) {
							const lastTool = msg.toolUses[msg.toolUses.length - 1];
							if (lastTool.name === event.name) {
								lastTool.output = event.output as string;
							}
						}
						break;

					case 'done':
						msg.streaming = false;
						msg.metadata = event.metadata as Record<string, unknown>;
						return {
							...s,
							messages: [...messages.slice(0, msgIndex), msg, ...messages.slice(msgIndex + 1)],
							isStreaming: false
						};

					case 'error':
						msg.streaming = false;
						return {
							...s,
							messages: [...messages.slice(0, msgIndex), msg, ...messages.slice(msgIndex + 1)],
							isStreaming: false,
							error: event.message as string
						};
				}

				messages[msgIndex] = msg;
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
export const chatError = derived(chat, $chat => $chat.error);
export const profiles = derived(chat, $chat => $chat.profiles);
export const selectedProfile = derived(chat, $chat => $chat.selectedProfile);
