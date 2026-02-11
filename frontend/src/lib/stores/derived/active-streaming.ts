/**
 * Active Streaming Derived Store
 *
 * Provides streaming state for the currently active session and workspace.
 * Uses sessionState when available, falls back to tabs store.
 */

import { derived } from 'svelte/store';
import { tabs, streamingSessions } from '../tabs';
import { sessionState } from '../session/session-state';

/** Read from session state (new store architecture) */
const USE_NEW_STORES = true;

/**
 * Whether the active tab is currently streaming.
 */
export const activeIsStreaming = derived(
	[tabs, sessionState],
	([$tabs, $sessionState]) => {
		const tab = $tabs.tabs.find(t => t.id === $tabs.activeTabId);
		if (!tab) return false;

		// Tab-level isStreaming is set immediately when user sends a message.
		// This must take priority over stale sessionState which may still reflect
		// the previous (ended) stream's state.
		if (tab.isStreaming) return true;

		if (USE_NEW_STORES && tab.sessionId) {
			const session = $sessionState.sessions.get(tab.sessionId);
			if (session?.isStreaming) return true;
		}

		return false;
	}
);

/**
 * Whether any session in the active tab's workspace (project) is currently streaming.
 * This is a superset of activeIsStreaming — it also checks cross-device/cross-session
 * streaming within the same project. Used to lock git controls (branch selector, commit)
 * so the workspace branch can't be changed while any session is streaming there.
 */
export const workspaceIsStreaming = derived(
	[tabs, sessionState, streamingSessions],
	([$tabs, $sessionState, $streamingSessions]) => {
		const tab = $tabs.tabs.find(t => t.id === $tabs.activeTabId);
		if (!tab) return false;

		// Active tab's own session is streaming (immediate/local)
		if (tab.isStreaming) return true;
		if (tab.sessionId) {
			const session = $sessionState.sessions.get(tab.sessionId);
			if (session?.isStreaming) return true;
		}

		// Any session in the same project is streaming (user-level cross-device)
		if (tab.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === tab.project) return true;
			}
		}

		return false;
	}
);

/**
 * Whether git controls (branch selector, commit button) should be locked due to streaming.
 *
 * For worktree-mode tabs: only locked if the active tab's OWN session is streaming,
 * because worktrees have independent working directories and branches.
 *
 * For project-mode tabs: locked if ANY non-worktree session in the same project is streaming,
 * because they share the same working directory and branch. Worktree sessions are excluded
 * because they operate in independent working directories and cannot affect the main branch.
 */
export const gitControlsStreaming = derived(
	[tabs, sessionState, streamingSessions],
	([$tabs, $sessionState, $streamingSessions]) => {
		const tab = $tabs.tabs.find(t => t.id === $tabs.activeTabId);
		if (!tab) return false;

		// Active tab's own session is streaming (always relevant)
		if (tab.isStreaming) return true;
		if (tab.sessionId) {
			const session = $sessionState.sessions.get(tab.sessionId);
			if (session?.isStreaming) return true;
		}

		// For worktree-mode tabs, that's all we check — independent working directory
		if (tab.executionMode === 'worktree') return false;

		// For project-mode tabs, check cross-session streaming in the same project,
		// but skip worktree sessions — they have independent working directories
		if (tab.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === tab.project && !info.worktreeId) return true;
			}
		}

		return false;
	}
);

/**
 * Whether the active tab's WebSocket is connected.
 * (This remains in tabs store since it's connection/UI state, not session data)
 */
export const activeWsConnected = derived(
	tabs,
	$tabs => {
		const tab = $tabs.tabs.find(t => t.id === $tabs.activeTabId);
		return tab?.wsConnected ?? false;
	}
);
