/**
 * Chat/conversation store with SSE streaming support
 * Includes sessions, profiles, and projects management
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
		error: null,
		abortController: null
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

		setProject(projectId: string) {
			update(s => ({ ...s, selectedProject: projectId }));
		},

		startNewChat() {
			update(s => ({
				...s,
				sessionId: null,
				messages: [],
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
			const state = get({ subscribe });

			// Create abort controller for this request
			const abortController = new AbortController();

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
				error: null,
				abortController
			}));

			// Build request
			const body: Record<string, unknown> = {
				prompt,
				profile: state.selectedProfile
			};

			if (state.sessionId) {
				body.session_id = state.sessionId;
			}

			if (state.selectedProject) {
				body.project = state.selectedProject;
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
							isStreaming: false,
							abortController: null
						};

					case 'interrupted':
						msg.streaming = false;
						msg.content += '\n\n[Interrupted]';
						return {
							...s,
							messages: [...messages.slice(0, msgIndex), msg, ...messages.slice(msgIndex + 1)],
							isStreaming: false,
							abortController: null
						};

					case 'error':
						msg.streaming = false;
						return {
							...s,
							messages: [...messages.slice(0, msgIndex), msg, ...messages.slice(msgIndex + 1)],
							isStreaming: false,
							error: event.message as string,
							abortController: null
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
export const projects = derived(chat, $chat => $chat.projects);
export const selectedProject = derived(chat, $chat => $chat.selectedProject);
export const sessions = derived(chat, $chat => $chat.sessions);
export const currentSessionId = derived(chat, $chat => $chat.sessionId);
