/**
 * Active Session Store — Re-export shim.
 * Implementation lives in active-session.svelte.ts (Svelte 5 runes).
 */
export {
	streamingSessions,
	backgroundPermissions,
	activeSession,
	activeSessionId,
	activeMessages,
	activeIsStreaming,
	activeWsConnected,
	activePendingPermissions,
	activePendingQuestions,
	activeTodos,
	activeError,
} from './active-session.svelte';

export type {
	StreamingSessionInfo,
	ActiveSessionState,
	PersistedSession,
} from './active-session.svelte';
