/**
 * Project Memory (CLAUDE.md) Store
 *
 * Manages state for CLAUDE.md memory files in project directories:
 * - "project": <project>/CLAUDE.md (shared)
 * - "local":   <project>/.claude/CLAUDE.md (private)
 *
 * Svelte 5 runes store following the projectAgents pattern.
 */

import type { MemoryFile, MemoryLocation } from '$lib/api/projectMemory';
import * as memoryApi from '$lib/api/projectMemory';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

export interface ProjectMemoryState {
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
		_update((state) => ({
			...state,
			loading: true,
			error: null,
			currentProjectId: projectId
		}));

		try {
			const files = await memoryApi.getMemoryFiles(projectId);
			_update((state) => ({ ...state, files, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load memory files';
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
			const error = e instanceof Error ? e.message : 'Failed to save memory file';
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
			const error = e instanceof Error ? e.message : 'Failed to delete memory file';
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

/** Get a specific file by location */
export function getMemoryFileByLocation(location: MemoryLocation) {
	return createDerived(() => _state.files.find((f) => f.location === location) ?? null);
}
