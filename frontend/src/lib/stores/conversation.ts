/**
 * Conversation Store - Voice conversation state management
 *
 * Manages the state for the Conversation card including:
 * - Active conversation sessions
 * - Audio capture state (listening, muted)
 * - TTS playback state
 * - Conversation transcript
 */

import { writable, derived, get } from 'svelte/store';

// ============================================================================
// Types
// ============================================================================

export type ConversationStatus =
	| 'idle'           // Not in a conversation
	| 'connecting'     // Establishing connection
	| 'listening'      // Actively listening for voice input
	| 'processing'     // Transcribing or waiting for AI response
	| 'speaking'       // Playing TTS audio
	| 'error';         // Error state

export interface ConversationTurn {
	id: string;
	role: 'user' | 'assistant';
	text: string;
	timestamp: Date;
	audioUrl?: string;  // For TTS playback reference
	isStreaming?: boolean;  // True while AI response is streaming
}

export interface ConversationSession {
	id: string;
	profileId: string;
	profileName: string;
	projectId: string;
	projectName: string;
	chatTabId: string;      // Links to underlying chat tab for message routing
	chatSessionId: string;  // Links to underlying chat session
	status: ConversationStatus;
	turns: ConversationTurn[];
	createdAt: Date;
	// Audio state
	isMuted: boolean;
	volume: number;  // 0.0 - 1.0
	// Current streaming state
	currentUtterance: string;  // Live STT transcription buffer
	currentAiResponse: string; // Live AI response buffer
	// Error info
	errorMessage: string | null;
}

export interface ConversationState {
	// Active conversation sessions (keyed by conversation card ID)
	sessions: Map<string, ConversationSession>;
	// Global settings (from user preferences)
	defaultTtsVoice: string;
	defaultTtsModel: string;
	defaultSttModel: string;
	// VAD settings
	vadSensitivity: number;  // 0.0 - 1.0, higher = more sensitive
	silenceThresholdMs: number;  // How long silence before sending audio
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: ConversationState = {
	sessions: new Map(),
	defaultTtsVoice: 'nova',
	defaultTtsModel: 'gpt-4o-mini-tts',
	defaultSttModel: 'gpt-4o-transcribe',
	vadSensitivity: 0.5,
	silenceThresholdMs: 1500,
};

// ============================================================================
// Store Creation
// ============================================================================

function createConversationStore() {
	const { subscribe, set, update } = writable<ConversationState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Session Management
		// ========================================================================

		/**
		 * Create a new conversation session
		 */
		createSession(
			cardId: string,
			options: {
				profileId: string;
				profileName: string;
				projectId: string;
				projectName: string;
				chatTabId: string;
				chatSessionId: string;
			}
		): void {
			update((state) => {
				const session: ConversationSession = {
					id: cardId,
					profileId: options.profileId,
					profileName: options.profileName,
					projectId: options.projectId,
					projectName: options.projectName,
					chatTabId: options.chatTabId,
					chatSessionId: options.chatSessionId,
					status: 'idle',
					turns: [],
					createdAt: new Date(),
					isMuted: false,
					volume: 1.0,
					currentUtterance: '',
					currentAiResponse: '',
					errorMessage: null,
				};

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, session);

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Get a conversation session by card ID
		 */
		getSession(cardId: string): ConversationSession | undefined {
			const state = get({ subscribe });
			return state.sessions.get(cardId);
		},

		/**
		 * Remove a conversation session
		 */
		removeSession(cardId: string): void {
			update((state) => {
				const newSessions = new Map(state.sessions);
				newSessions.delete(cardId);
				return { ...state, sessions: newSessions };
			});
		},

		// ========================================================================
		// Status Management
		// ========================================================================

		/**
		 * Update session status
		 */
		setStatus(cardId: string, status: ConversationStatus, errorMessage?: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					status,
					errorMessage: errorMessage || null,
				});

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Start listening for voice input
		 */
		startListening(cardId: string): void {
			this.setStatus(cardId, 'listening');
		},

		/**
		 * Stop listening (mute or pause)
		 */
		stopListening(cardId: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					status: session.status === 'listening' ? 'idle' : session.status,
				});

				return { ...state, sessions: newSessions };
			});
		},

		// ========================================================================
		// Audio Controls
		// ========================================================================

		/**
		 * Toggle mute state
		 */
		toggleMute(cardId: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					isMuted: !session.isMuted,
				});

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Set mute state explicitly
		 */
		setMuted(cardId: string, muted: boolean): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					isMuted: muted,
				});

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Set volume level
		 */
		setVolume(cardId: string, volume: number): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					volume: Math.max(0, Math.min(1, volume)),
				});

				return { ...state, sessions: newSessions };
			});
		},

		// ========================================================================
		// Conversation Turns
		// ========================================================================

		/**
		 * Add a user turn (from completed STT)
		 */
		addUserTurn(cardId: string, text: string): string {
			const turnId = `turn-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const turn: ConversationTurn = {
					id: turnId,
					role: 'user',
					text,
					timestamp: new Date(),
				};

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					turns: [...session.turns, turn],
					currentUtterance: '',
				});

				return { ...state, sessions: newSessions };
			});

			return turnId;
		},

		/**
		 * Add or update an assistant turn (streaming response)
		 */
		addOrUpdateAssistantTurn(cardId: string, text: string, isStreaming: boolean = false): string {
			let turnId = '';

			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const lastTurn = session.turns[session.turns.length - 1];
				let newTurns: ConversationTurn[];

				if (lastTurn && lastTurn.role === 'assistant' && lastTurn.isStreaming) {
					// Update existing streaming turn
					turnId = lastTurn.id;
					newTurns = session.turns.map((t) =>
						t.id === lastTurn.id ? { ...t, text, isStreaming } : t
					);
				} else {
					// Create new turn
					turnId = `turn-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
					const turn: ConversationTurn = {
						id: turnId,
						role: 'assistant',
						text,
						timestamp: new Date(),
						isStreaming,
					};
					newTurns = [...session.turns, turn];
				}

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					turns: newTurns,
					currentAiResponse: isStreaming ? text : '',
				});

				return { ...state, sessions: newSessions };
			});

			return turnId;
		},

		/**
		 * Complete an assistant turn (mark as done streaming)
		 */
		completeAssistantTurn(cardId: string, turnId: string, audioUrl?: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newTurns = session.turns.map((t) =>
					t.id === turnId ? { ...t, isStreaming: false, audioUrl } : t
				);

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					turns: newTurns,
					currentAiResponse: '',
				});

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Update live STT transcription buffer
		 */
		updateCurrentUtterance(cardId: string, text: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					currentUtterance: text,
				});

				return { ...state, sessions: newSessions };
			});
		},

		/**
		 * Clear all turns (reset conversation history view)
		 */
		clearTurns(cardId: string): void {
			update((state) => {
				const session = state.sessions.get(cardId);
				if (!session) return state;

				const newSessions = new Map(state.sessions);
				newSessions.set(cardId, {
					...session,
					turns: [],
					currentUtterance: '',
					currentAiResponse: '',
				});

				return { ...state, sessions: newSessions };
			});
		},

		// ========================================================================
		// Global Settings
		// ========================================================================

		/**
		 * Update TTS settings
		 */
		setTtsSettings(voice: string, model: string): void {
			update((state) => ({
				...state,
				defaultTtsVoice: voice,
				defaultTtsModel: model,
			}));
		},

		/**
		 * Update STT settings
		 */
		setSttSettings(model: string): void {
			update((state) => ({
				...state,
				defaultSttModel: model,
			}));
		},

		/**
		 * Update VAD settings
		 */
		setVadSettings(sensitivity: number, silenceThresholdMs: number): void {
			update((state) => ({
				...state,
				vadSensitivity: Math.max(0, Math.min(1, sensitivity)),
				silenceThresholdMs: Math.max(500, Math.min(5000, silenceThresholdMs)),
			}));
		},

		/**
		 * Reset to initial state
		 */
		reset(): void {
			set(initialState);
		},
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const conversation = createConversationStore();

// Active sessions count
export const activeSessionsCount = derived(conversation, ($conv) => $conv.sessions.size);

// Check if any session is actively speaking (for global audio management)
export const anySpeaking = derived(conversation, ($conv) => {
	for (const session of $conv.sessions.values()) {
		if (session.status === 'speaking') return true;
	}
	return false;
});

// Get a specific session as a derived store
export const getSessionStore = (cardId: string) =>
	derived(conversation, ($conv) => $conv.sessions.get(cardId));
