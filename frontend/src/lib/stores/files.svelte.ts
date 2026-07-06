/**
 * Files Store - State management for the Files workspace
 *
 * Manages:
 * - Path navigation within the workspace root
 * - File listing and caching
 * - File operations (CRUD)
 * - Upload progress tracking
 * - UI preferences (persisted to localStorage + server)
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';
import { getPreference, setPreference } from '$lib/api/client';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

export interface FileItem {
	name: string;
	type: 'file' | 'directory';
	size: number | null;
	sizeFormatted: string | null;
	path: string;
	extension: string | null;
	modifiedAt: string;
	createdAt: string;
}

interface FilesState {
	currentPath: string;
	files: FileItem[];
	loading: boolean;
	error: string | null;
	// Selection state
	selectedFiles: Set<string>;
	// Upload state
	uploading: boolean;
	uploadProgress: number;
	// UI state
	viewMode: 'grid' | 'list';
	sortBy: 'name' | 'size' | 'modified';
	sortAsc: boolean;
	showHidden: boolean;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'files_state';
const SERVER_PREFERENCE_KEY = 'files_preferences';
const SERVER_SYNC_DEBOUNCE_MS = 500;

// ============================================================================
// Module-level mutable state (timers, flags)
// ============================================================================

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingPrefs: FilesPreferences | null = null;

// ============================================================================
// Server preferences type (subset of FilesState)
// ============================================================================

interface FilesPreferences {
	viewMode: 'grid' | 'list';
	sortBy: 'name' | 'size' | 'modified';
	sortAsc: boolean;
	showHidden: boolean;
}

// ============================================================================
// Persistence Helpers
// ============================================================================

/**
 * Load file preferences from server
 */
async function loadFromServer(): Promise<FilesPreferences | null> {
	try {
		const response = await getPreference<FilesPreferences>(SERVER_PREFERENCE_KEY);
		return (response?.value as FilesPreferences) || null;
	} catch (e) {
		console.error('[Files] Failed to load preferences from server:', e);
		return null;
	}
}

/**
 * Save file preferences to server (debounced)
 */
function saveToServer(prefs: FilesPreferences) {
	if (!browser) return;

	pendingPrefs = prefs;

	if (!hasSyncedFromServer) {
		return;
	}

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, prefs);
			pendingPrefs = null;
		} catch (e) {
			console.error('[Files] Failed to save preferences to server:', e);
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

/**
 * Flush pending save on unload using sendBeacon
 */
function flushPendingFilesSave() {
	if (!pendingPrefs || !hasSyncedFromServer || !browser) return;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
		serverSyncTimer = null;
	}

	const data = JSON.stringify({
		key: SERVER_PREFERENCE_KEY,
		value: pendingPrefs,
	});
	window.navigator.sendBeacon(
		'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
		new Blob([data], { type: 'application/json' })
	);
	pendingPrefs = null;
}

// Register beforeunload + visibilitychange handlers (guarded against HMR duplicate registration)
if (browser && !(window as any).__files_listeners) {
	(window as any).__files_listeners = true;
	window.addEventListener('beforeunload', flushPendingFilesSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingFilesSave();
		}
	});
}

function loadFromStorage(): Partial<FilesState> | null {
	if (!browser) return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored);
			return {
				viewMode: parsed.viewMode || 'list',
				sortBy: parsed.sortBy || 'name',
				sortAsc: parsed.sortAsc ?? true,
				showHidden: parsed.showHidden ?? false,
			};
		}
	} catch (e) {
		console.error('[Files] Failed to load state from localStorage:', e);
	}
	return null;
}

function saveToStorage(state: FilesState) {
	if (!browser) return;

	try {
		const toSave = {
			viewMode: state.viewMode,
			sortBy: state.sortBy,
			sortAsc: state.sortAsc,
			showHidden: state.showHidden,
		};
		localStorage.setItem(STORAGE_KEY, JSON.stringify(toSave));

		// Also sync preferences to server
		const prefs: FilesPreferences = {
			viewMode: state.viewMode,
			sortBy: state.sortBy,
			sortAsc: state.sortAsc,
			showHidden: state.showHidden,
		};
		saveToServer(prefs);
	} catch (e) {
		console.error('[Files] Failed to save state to localStorage:', e);
	}
}

// ============================================================================
// Reactive State
// ============================================================================

const persisted = loadFromStorage();

let _state = $state<FilesState>({
	currentPath: '',
	files: [],
	loading: false,
	error: null,
	selectedFiles: new Set(),
	uploading: false,
	uploadProgress: 0,
	viewMode: persisted?.viewMode || 'list',
	sortBy: persisted?.sortBy || 'name',
	sortAsc: persisted?.sortAsc ?? true,
	showHidden: persisted?.showHidden ?? false,
});

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<FilesState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const files = {
	get current() {
		return _state;
	},

	subscribe,

	// ========================================================================
	// Navigation
	// ========================================================================

	navigateTo(path: string): void {
		_update((s) => ({
			...s,
			currentPath: path,
			selectedFiles: new Set<string>(),
		}));
		files.loadFiles();
	},

	navigateUp(): void {
		if (!_state.currentPath) return;

		const parts = _state.currentPath.split('/');
		parts.pop();
		const parentPath = parts.join('/');

		files.navigateTo(parentPath);
	},

	// ========================================================================
	// File Loading
	// ========================================================================

	async loadFiles(): Promise<void> {
		const currentPath = _state.currentPath;
		const showHidden = _state.showHidden;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			const params = new URLSearchParams({
				path: currentPath,
				show_hidden: showHidden.toString(),
			});

			const response = await fetch(`/api/v1/workspace/files?${params}`, {
				credentials: 'include',
			});

			if (!response.ok) {
				const error = await response.json();
				throw new Error(error.detail || 'Failed to load files');
			}

			const data = await response.json();
			_update((s) => ({
				...s,
				files: data.files,
				loading: false,
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load files';
			_update((s) => ({
				...s,
				loading: false,
				error,
			}));
		}
	},

	// ========================================================================
	// File Operations
	// ========================================================================

	async createFolder(name: string): Promise<boolean> {
		const currentPath = _state.currentPath;

		try {
			const params = new URLSearchParams({ path: currentPath });

			const response = await fetch(`/api/v1/workspace/files/folder?${params}`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
				},
				credentials: 'include',
				body: JSON.stringify({ name }),
			});

			if (!response.ok) {
				const error = await response.json();
				throw new Error(error.detail || 'Failed to create folder');
			}

			await files.loadFiles();
			return true;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to create folder';
			_update((s) => ({ ...s, error }));
			return false;
		}
	},

	async rename(path: string, newName: string): Promise<boolean> {
		try {
			const params = new URLSearchParams({ path });

			const response = await fetch(`/api/v1/workspace/files/rename?${params}`, {
				method: 'PUT',
				headers: {
					'Content-Type': 'application/json',
				},
				credentials: 'include',
				body: JSON.stringify({ new_name: newName }),
			});

			if (!response.ok) {
				const error = await response.json();
				throw new Error(error.detail || 'Failed to rename');
			}

			await files.loadFiles();
			return true;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to rename';
			_update((s) => ({ ...s, error }));
			return false;
		}
	},

	async delete(path: string): Promise<boolean> {
		try {
			const params = new URLSearchParams({ path });

			const response = await fetch(`/api/v1/workspace/files?${params}`, {
				method: 'DELETE',
				credentials: 'include',
			});

			if (!response.ok) {
				const error = await response.json();
				throw new Error(error.detail || 'Failed to delete');
			}

			await files.loadFiles();
			return true;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to delete';
			_update((s) => ({ ...s, error }));
			return false;
		}
	},

	async deleteSelected(): Promise<boolean> {
		if (_state.selectedFiles.size === 0) return false;

		const paths = Array.from(_state.selectedFiles);
		let success = true;

		for (const path of paths) {
			const result = await files.delete(path);
			if (!result) success = false;
		}

		_update((s) => ({ ...s, selectedFiles: new Set<string>() }));
		return success;
	},

	// ========================================================================
	// Upload
	// ========================================================================

	async uploadFiles(fileList: FileList): Promise<boolean> {
		const currentPath = _state.currentPath;
		if (fileList.length === 0) return false;

		_update((s) => ({ ...s, uploading: true, uploadProgress: 0, error: null }));

		try {
			const total = fileList.length;
			let completed = 0;

			for (const file of Array.from(fileList)) {
				const formData = new FormData();
				formData.append('file', file);
				formData.append('path', currentPath);

				const response = await fetch(`/api/v1/workspace/upload`, {
					method: 'POST',
					credentials: 'include',
					body: formData,
				});

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || `Failed to upload ${file.name}`);
				}

				completed++;
				_update((s) => ({
					...s,
					uploadProgress: Math.round((completed / total) * 100),
				}));
			}

			_update((s) => ({ ...s, uploading: false, uploadProgress: 0 }));
			await files.loadFiles();
			return true;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to upload files';
			_update((s) => ({
				...s,
				uploading: false,
				uploadProgress: 0,
				error,
			}));
			return false;
		}
	},

	// ========================================================================
	// Download
	// ========================================================================

	async downloadFile(path: string, filename: string): Promise<void> {
		try {
			const params = new URLSearchParams({ path });

			const response = await fetch(`/api/v1/workspace/files/download?${params}`, {
				credentials: 'include',
			});

			if (!response.ok) {
				const error = await response.json();
				throw new Error(error.detail || 'Failed to download');
			}

			// Create blob and download
			const blob = await response.blob();
			const url = window.URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = filename;
			document.body.appendChild(a);
			a.click();
			window.URL.revokeObjectURL(url);
			document.body.removeChild(a);
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to download';
			_update((s) => ({ ...s, error }));
		}
	},

	// ========================================================================
	// Selection
	// ========================================================================

	toggleSelection(path: string): void {
		_update((s) => {
			const newSelection = new Set(s.selectedFiles);
			if (newSelection.has(path)) {
				newSelection.delete(path);
			} else {
				newSelection.add(path);
			}
			return { ...s, selectedFiles: newSelection };
		});
	},

	selectAll(): void {
		_update((s) => ({
			...s,
			selectedFiles: new Set(s.files.map((f) => f.path)),
		}));
	},

	clearSelection(): void {
		_update((s) => ({
			...s,
			selectedFiles: new Set<string>(),
		}));
	},

	// ========================================================================
	// View Settings
	// ========================================================================

	setViewMode(mode: 'grid' | 'list'): void {
		_update((s) => {
			const newState = { ...s, viewMode: mode };
			saveToStorage(newState);
			return newState;
		});
	},

	setSortBy(sortBy: 'name' | 'size' | 'modified'): void {
		_update((s) => {
			const newState = { ...s, sortBy };
			saveToStorage(newState);
			return newState;
		});
	},

	toggleSortDirection(): void {
		_update((s) => {
			const newState = { ...s, sortAsc: !s.sortAsc };
			saveToStorage(newState);
			return newState;
		});
	},

	toggleShowHidden(): void {
		_update((s) => {
			const newState = { ...s, showHidden: !s.showHidden };
			saveToStorage(newState);
			return newState;
		});
		files.loadFiles();
	},

	// ========================================================================
	// Error Handling
	// ========================================================================

	clearError(): void {
		_update((s) => ({ ...s, error: null }));
	},

	// ========================================================================
	// Server Sync
	// ========================================================================

	/**
	 * Sync preferences from server (call after authentication)
	 * Server is the source of truth for UI preferences
	 */
	async syncFromServer(): Promise<void> {
		if (!browser) return;

		const serverPrefs = await loadFromServer();

		if (serverPrefs) {
			// Server has preferences - update local state
			const needsUpdate =
				serverPrefs.viewMode !== _state.viewMode ||
				serverPrefs.sortBy !== _state.sortBy ||
				serverPrefs.sortAsc !== _state.sortAsc ||
				serverPrefs.showHidden !== _state.showHidden;

			if (needsUpdate) {
				// Update localStorage
				localStorage.setItem(STORAGE_KEY, JSON.stringify(serverPrefs));

				// Update store
				_update((s) => ({
					...s,
					...serverPrefs,
				}));
			}
		}

		hasSyncedFromServer = true;

		// If no server data, save current local preferences to server
		if (!serverPrefs) {
			const prefs: FilesPreferences = {
				viewMode: _state.viewMode,
				sortBy: _state.sortBy,
				sortAsc: _state.sortAsc,
				showHidden: _state.showHidden,
			};
			saveToServer(prefs);
		}
	},

	// ========================================================================
	// Reset
	// ========================================================================

	reset(): void {
		_state = {
			currentPath: '',
			files: [],
			loading: false,
			error: null,
			selectedFiles: new Set(),
			uploading: false,
			uploadProgress: 0,
			viewMode: 'list',
			sortBy: 'name',
			sortAsc: true,
			showHidden: false,
		};
		_notifySubscribers();
	},
};

// ============================================================================
// Derived Stores (via store-utils createDerived)
// ============================================================================

// Derived: Current path breadcrumbs
export const breadcrumbs = createDerived(() => {
	const parts = _state.currentPath.split('/').filter(Boolean);
	const crumbs = [{ name: 'Workspace', path: '' }];

	let currentPath = '';
	for (const part of parts) {
		currentPath = currentPath ? `${currentPath}/${part}` : part;
		crumbs.push({ name: part, path: currentPath });
	}

	return crumbs;
});

// Derived: Sorted files
export const sortedFiles = createDerived(() => {
	const sorted = [..._state.files];

	sorted.sort((a, b) => {
		// Directories always first
		if (a.type !== b.type) {
			return a.type === 'directory' ? -1 : 1;
		}

		let comparison = 0;
		switch (_state.sortBy) {
			case 'name':
				comparison = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
				break;
			case 'size':
				comparison = (a.size || 0) - (b.size || 0);
				break;
			case 'modified':
				comparison = new Date(a.modifiedAt).getTime() - new Date(b.modifiedAt).getTime();
				break;
		}

		return _state.sortAsc ? comparison : -comparison;
	});

	return sorted;
});
