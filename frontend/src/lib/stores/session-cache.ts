/**
 * Session Ephemeral Cache
 *
 * LRU cache for per-session UI state that survives session switches.
 * Stores drafts, scroll positions, overrides, and worktree config.
 * Memory-only (not persisted to backend).
 */

import type { DeviceIntent, ExecutionMode } from './types';
import type { ContextWindowChoice } from '$lib/constants/model-context';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface SessionEphemeral {
	profile?: string;
	title: string;
	draft: string;
	scrollTop: number;
	project: string;
	projectV2Id: string | null;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	effortLevelOverride: string | null;
	/** Fast service tier — part of the run binding, cached like the rest of it. */
	fastMode?: boolean;
	workspaceProjectIds?: string[];
	contextWindowChoice?: ContextWindowChoice;
	worktreeIntent: ExecutionMode;
	/** Compose-time device pick — memory-only, exactly like the rest of this
	 *  cache, so a draft keeps its device across a switch away and back without
	 *  ever becoming a durable default. */
	deviceIntent: DeviceIntent | null;
	worktreeId: string | null;
	worktreeBranch: string;
	worktreeBaseBranch: string;
	selectedWorktreeBranch: string | null;
	worktreeMode: 'new' | 'existing';
	worktreeCreateNewBranch?: boolean;
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
	 * Update just the draft of an already-cached session, in place.
	 *
	 * Used to flush the chat input's *live* editor content for the session being
	 * left at switch time — the store's draft is debounced and can be stale, so
	 * relying on it alone risks losing (or, in the worst case, leaking) text
	 * across a session switch. No-op if the session was never cached; switch
	 * handlers always save() the leaving session's ephemeral first, so an entry
	 * exists by the time this runs.
	 */
	patchDraft(sessionId: string, draft: string): void {
		const entry = this.cache.get(sessionId);
		if (entry) entry.draft = draft;
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
