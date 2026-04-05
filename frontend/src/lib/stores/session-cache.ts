/**
 * Session Ephemeral Cache
 *
 * LRU cache for per-session UI state that survives session switches.
 * Stores drafts, scroll positions, overrides, and worktree config.
 * Memory-only (not persisted to backend).
 */

import type { ExecutionMode } from './types';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface SessionEphemeral {
	title: string;
	draft: string;
	scrollTop: number;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	fastMode: boolean;
	executionMode: ExecutionMode;
	worktreeId: string | null;
	worktreeBranch: string;
	worktreeBaseBranch: string;
	selectedWorktreeBranch: string | null;
	worktreeMode: 'new' | 'existing';
	worktreeDeleted: boolean;
}

// ─── LRU Cache ───────────────────────────────────────────────────────────────

const MAX_ENTRIES = 50;

class SessionEphemeralCache {
	private cache = new Map<string, SessionEphemeral>();

	/**
	 * Save ephemeral state for a session.
	 * Moves the entry to the "most recently used" position.
	 */
	save(sessionId: string, ephemeral: SessionEphemeral): void {
		// Delete first to reinsert at end (Map preserves insertion order)
		this.cache.delete(sessionId);
		this.cache.set(sessionId, ephemeral);
		this.evict();
	}

	/**
	 * Restore ephemeral state for a session.
	 * Returns null if not cached.
	 */
	restore(sessionId: string): SessionEphemeral | null {
		const entry = this.cache.get(sessionId);
		if (!entry) return null;

		// Move to most recently used
		this.cache.delete(sessionId);
		this.cache.set(sessionId, entry);

		return entry;
	}

	/**
	 * Check if a session has cached ephemeral state.
	 */
	has(sessionId: string): boolean {
		return this.cache.has(sessionId);
	}

	/**
	 * Remove a session's cached state (e.g., when session is deleted).
	 */
	remove(sessionId: string): void {
		this.cache.delete(sessionId);
	}

	/**
	 * Clear all cached state.
	 */
	clear(): void {
		this.cache.clear();
	}

	/**
	 * Evict oldest entries when over capacity.
	 */
	private evict(): void {
		while (this.cache.size > MAX_ENTRIES) {
			// Map.keys() iterates in insertion order; first key is the oldest
			const oldest = this.cache.keys().next().value;
			if (oldest !== undefined) {
				this.cache.delete(oldest);
			}
		}
	}
}

// ─── Export singleton ────────────────────────────────────────────────────────

export const sessionEphemeralCache = new SessionEphemeralCache();
