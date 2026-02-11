/**
 * Active Streaming Derived Store
 *
 * Provides streaming state for the currently active session and workspace.
 * Reads from activeSession store and sessionState.
 */

import { derived } from 'svelte/store';
import { activeSession, streamingSessions } from '../active-session';
import { sessionState } from '../session/session-state';

/**
 * Whether the active session is currently streaming.
 */
export const activeIsStreaming = derived(
	[activeSession, sessionState],
	([$activeSession, $sessionState]) => {
		// Store-level isStreaming is set immediately when user sends a message
		if ($activeSession.isStreaming) return true;

		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		return false;
	}
);

/**
 * Whether any session in the active session's workspace (project) is currently streaming.
 * Used to lock git controls so the workspace branch can't change during streaming.
 */
export const workspaceIsStreaming = derived(
	[activeSession, sessionState, streamingSessions],
	([$activeSession, $sessionState, $streamingSessions]) => {
		// Active session is streaming
		if ($activeSession.isStreaming) return true;
		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		// Any session in the same project is streaming (cross-device)
		if ($activeSession.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === $activeSession.project) return true;
			}
		}

		return false;
	}
);

/**
 * Whether git controls should be locked due to streaming.
 *
 * For worktree-mode: only locked if the active session's OWN session is streaming.
 * For project-mode: locked if ANY non-worktree session in the same project is streaming.
 */
export const gitControlsStreaming = derived(
	[activeSession, sessionState, streamingSessions],
	([$activeSession, $sessionState, $streamingSessions]) => {
		// Active session is streaming (always relevant)
		if ($activeSession.isStreaming) return true;
		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		// Worktree-mode has independent working directory
		if ($activeSession.executionMode === 'worktree') return false;

		// Project-mode: check cross-session streaming, skip worktree sessions
		if ($activeSession.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === $activeSession.project && !info.worktreeId) return true;
			}
		}

		return false;
	}
);

/**
 * Whether the active session's WebSocket is connected.
 */
export const activeWsConnected = derived(
	activeSession,
	$activeSession => $activeSession.wsConnected
);
