/**
 * Project Memory (AGENTS.md) Store
 *
 * Manages state for AGENTS.md memory files in project directories:
 * - "project": <project>/AGENTS.md (shared)
 * - "local":   <project>/.agents/AGENTS.md (private)
 *
 * Legacy CLAUDE.md / .claude/CLAUDE.md files are read as a fallback when no
 * AGENTS.md exists; saves always write the AGENTS.md path.
 *
 * Svelte 5 runes store following the projectAgents pattern.
 */

import type { MemoryFile, MemoryLocation } from '$lib/api/projectMemory';
import * as memoryApi from '$lib/api/projectMemory';
import { getErrorMessage } from '$lib/api/client';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

interface ProjectMemoryState {
	files: MemoryFile[];
	currentProjectId: string | null;
	loading: boolean;
	saving: boolean;
	error: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: ProjectMemoryState = {
	files: [],
	currentProjectId: null,
	loading: false,
	saving: false,
	error: null
};

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<ProjectMemoryState>({ ...initialState });

/** Latest loadFiles call; an older (slower) response is discarded so a
 *  project the user already left cannot overwrite the current files. */
let _loadSeq = 0;

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, notifySubscribers: _notifySubscribers, update: _update, createDerived } =
	createSubscribers<ProjectMemoryState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const projectMemory = {
	get current() {
		return _state;
	},

	subscribe,

	// ========================================================================
	// Loading
	// ========================================================================

	/**
	 * Load all memory files for a project
	 */
	async loadFiles(projectId: string): Promise<void> {
		const seq = ++_loadSeq;
		_update((state) => ({
			...state,
			loading: true,
			error: null,
			currentProjectId: projectId
		}));

		try {
			const files = await memoryApi.getMemoryFiles(projectId);
			if (seq !== _loadSeq) return;
			_update((state) => ({ ...state, files, loading: false }));
		} catch (e) {
			if (seq !== _loadSeq) throw e;
			const error = getErrorMessage(e, 'Failed to load memory files');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	// ========================================================================
	// CRUD Operations
	// ========================================================================

	/**
	 * Save (create or update) a memory file
	 */
	async saveFile(
		projectId: string,
		location: MemoryLocation,
		content: string
	): Promise<MemoryFile> {
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			const file = await memoryApi.saveMemoryFile(projectId, location, content);
			_update((state) => ({
				...state,
				files: state.files.map((f) =>
					f.location === location ? file : f
				),
				saving: false
			}));
			return file;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to save memory file');
			_update((state) => ({ ...state, error, saving: false }));
			throw e;
		}
	},

	/**
	 * Delete a memory file
	 */
	async deleteFile(
		projectId: string,
		location: MemoryLocation
	): Promise<void> {
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			await memoryApi.deleteMemoryFile(projectId, location);
			_update((state) => ({
				...state,
				files: state.files.map((f) =>
					f.location === location
						? { ...f, exists: false, content: '', size: 0, updated_at: null }
						: f
				),
				saving: false
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to delete memory file');
			_update((state) => ({ ...state, error, saving: false }));
			throw e;
		}
	},

	// ========================================================================
	// Utility
	// ========================================================================

	clearError(): void {
		_update((state) => ({ ...state, error: null }));
	},

	reset(): void {
		_loadSeq++;
		_state = { ...initialState };
		_notifySubscribers();
	}
};

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

/** All memory files */
export const allMemoryFiles = createDerived(() => _state.files);

/** Loading state */
export const memoryLoading = createDerived(() => _state.loading);

/** Saving state */
export const memorySaving = createDerived(() => _state.saving);

/** Error state */
export const memoryError = createDerived(() => _state.error);
