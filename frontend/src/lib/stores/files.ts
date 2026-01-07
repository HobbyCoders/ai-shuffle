/**
 * Files Store - State management for the Files workspace
 *
 * Manages:
 * - Current project and path navigation
 * - File listing and caching
 * - File operations (CRUD)
 * - Upload progress tracking
 */

import { writable, derived, get } from 'svelte/store';

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

export interface FilesState {
	selectedProjectId: string | null;
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

// ============================================================================
// Helpers
// ============================================================================

function loadFromStorage(): Partial<FilesState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored);
			return {
				selectedProjectId: parsed.selectedProjectId || null,
				viewMode: parsed.viewMode || 'list',
				sortBy: parsed.sortBy || 'name',
				sortAsc: parsed.sortAsc ?? true,
				showHidden: parsed.showHidden ?? false
			};
		}
	} catch (e) {
		console.error('[Files] Failed to load state from localStorage:', e);
	}
	return null;
}

function saveToStorage(state: FilesState) {
	if (typeof window === 'undefined') return;

	try {
		const toSave = {
			selectedProjectId: state.selectedProjectId,
			viewMode: state.viewMode,
			sortBy: state.sortBy,
			sortAsc: state.sortAsc,
			showHidden: state.showHidden
		};
		localStorage.setItem(STORAGE_KEY, JSON.stringify(toSave));
	} catch (e) {
		console.error('[Files] Failed to save state to localStorage:', e);
	}
}

// ============================================================================
// Store Creation
// ============================================================================

function createFilesStore() {
	const persisted = loadFromStorage();

	const initialState: FilesState = {
		selectedProjectId: persisted?.selectedProjectId || null,
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
		showHidden: persisted?.showHidden ?? false
	};

	const { subscribe, set, update } = writable<FilesState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Project Selection
		// ========================================================================

		selectProject(projectId: string | null): void {
			update((state) => {
				const newState = {
					...state,
					selectedProjectId: projectId,
					currentPath: '',
					files: [],
					selectedFiles: new Set<string>(),
					error: null
				};
				saveToStorage(newState);
				return newState;
			});

			// Auto-load files if project selected
			if (projectId) {
				this.loadFiles();
			}
		},

		// ========================================================================
		// Navigation
		// ========================================================================

		navigateTo(path: string): void {
			update((state) => ({
				...state,
				currentPath: path,
				selectedFiles: new Set<string>()
			}));
			this.loadFiles();
		},

		navigateUp(): void {
			const state = get({ subscribe });
			if (!state.currentPath) return;

			const parts = state.currentPath.split('/');
			parts.pop();
			const parentPath = parts.join('/');

			this.navigateTo(parentPath);
		},

		// ========================================================================
		// File Loading
		// ========================================================================

		async loadFiles(): Promise<void> {
			const state = get({ subscribe });
			if (!state.selectedProjectId) return;

			update((s) => ({ ...s, loading: true, error: null }));

			try {
				const params = new URLSearchParams({
					path: state.currentPath,
					show_hidden: state.showHidden.toString()
				});

				const response = await fetch(
					`/api/v1/projects/${state.selectedProjectId}/files?${params}`,
					{
						credentials: 'include'
					}
				);

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Failed to load files');
				}

				const data = await response.json();
				update((s) => ({
					...s,
					files: data.files,
					loading: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load files';
				update((s) => ({
					...s,
					loading: false,
					error
				}));
			}
		},

		// ========================================================================
		// File Operations
		// ========================================================================

		async createFolder(name: string): Promise<boolean> {
			const state = get({ subscribe });
			if (!state.selectedProjectId) return false;

			try {
				const params = new URLSearchParams({ path: state.currentPath });

				const response = await fetch(
					`/api/v1/projects/${state.selectedProjectId}/files/folder?${params}`,
					{
						method: 'POST',
						headers: {
							'Content-Type': 'application/json'
						},
						credentials: 'include',
						body: JSON.stringify({ name })
					}
				);

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Failed to create folder');
				}

				await this.loadFiles();
				return true;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to create folder';
				update((s) => ({ ...s, error }));
				return false;
			}
		},

		async rename(path: string, newName: string): Promise<boolean> {
			const state = get({ subscribe });
			if (!state.selectedProjectId) return false;

			try {
				const params = new URLSearchParams({ path });

				const response = await fetch(
					`/api/v1/projects/${state.selectedProjectId}/files/rename?${params}`,
					{
						method: 'PUT',
						headers: {
							'Content-Type': 'application/json'
						},
						credentials: 'include',
						body: JSON.stringify({ new_name: newName })
					}
				);

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Failed to rename');
				}

				await this.loadFiles();
				return true;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to rename';
				update((s) => ({ ...s, error }));
				return false;
			}
		},

		async delete(path: string): Promise<boolean> {
			const state = get({ subscribe });
			if (!state.selectedProjectId) return false;

			try {
				const params = new URLSearchParams({ path });

				const response = await fetch(
					`/api/v1/projects/${state.selectedProjectId}/files?${params}`,
					{
						method: 'DELETE',
						credentials: 'include'
					}
				);

				if (!response.ok) {
					const error = await response.json();
					throw new Error(error.detail || 'Failed to delete');
				}

				await this.loadFiles();
				return true;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to delete';
				update((s) => ({ ...s, error }));
				return false;
			}
		},

		async deleteSelected(): Promise<boolean> {
			const state = get({ subscribe });
			if (!state.selectedProjectId || state.selectedFiles.size === 0) return false;

			const paths = Array.from(state.selectedFiles);
			let success = true;

			for (const path of paths) {
				const result = await this.delete(path);
				if (!result) success = false;
			}

			update((s) => ({ ...s, selectedFiles: new Set<string>() }));
			return success;
		},

		// ========================================================================
		// Upload
		// ========================================================================

		async uploadFiles(files: FileList): Promise<boolean> {
			const state = get({ subscribe });
			if (!state.selectedProjectId || files.length === 0) return false;

			update((s) => ({ ...s, uploading: true, uploadProgress: 0, error: null }));

			try {
				const total = files.length;
				let completed = 0;

				for (const file of Array.from(files)) {
					const formData = new FormData();
					formData.append('file', file);
					formData.append('path', state.currentPath);

					const response = await fetch(
						`/api/v1/projects/${state.selectedProjectId}/upload`,
						{
							method: 'POST',
							credentials: 'include',
							body: formData
						}
					);

					if (!response.ok) {
						const error = await response.json();
						throw new Error(error.detail || `Failed to upload ${file.name}`);
					}

					completed++;
					update((s) => ({
						...s,
						uploadProgress: Math.round((completed / total) * 100)
					}));
				}

				update((s) => ({ ...s, uploading: false, uploadProgress: 0 }));
				await this.loadFiles();
				return true;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to upload files';
				update((s) => ({
					...s,
					uploading: false,
					uploadProgress: 0,
					error
				}));
				return false;
			}
		},

		// ========================================================================
		// Download
		// ========================================================================

		async downloadFile(path: string, filename: string): Promise<void> {
			const state = get({ subscribe });
			if (!state.selectedProjectId) return;

			try {
				const params = new URLSearchParams({ path });

				const response = await fetch(
					`/api/v1/projects/${state.selectedProjectId}/files/download?${params}`,
					{
						credentials: 'include'
					}
				);

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
				update((s) => ({ ...s, error }));
			}
		},

		// ========================================================================
		// Selection
		// ========================================================================

		toggleSelection(path: string): void {
			update((state) => {
				const newSelection = new Set(state.selectedFiles);
				if (newSelection.has(path)) {
					newSelection.delete(path);
				} else {
					newSelection.add(path);
				}
				return { ...state, selectedFiles: newSelection };
			});
		},

		selectAll(): void {
			update((state) => ({
				...state,
				selectedFiles: new Set(state.files.map((f) => f.path))
			}));
		},

		clearSelection(): void {
			update((state) => ({
				...state,
				selectedFiles: new Set<string>()
			}));
		},

		// ========================================================================
		// View Settings
		// ========================================================================

		setViewMode(mode: 'grid' | 'list'): void {
			update((state) => {
				const newState = { ...state, viewMode: mode };
				saveToStorage(newState);
				return newState;
			});
		},

		setSortBy(sortBy: 'name' | 'size' | 'modified'): void {
			update((state) => {
				const newState = { ...state, sortBy };
				saveToStorage(newState);
				return newState;
			});
		},

		toggleSortDirection(): void {
			update((state) => {
				const newState = { ...state, sortAsc: !state.sortAsc };
				saveToStorage(newState);
				return newState;
			});
		},

		toggleShowHidden(): void {
			update((state) => {
				const newState = { ...state, showHidden: !state.showHidden };
				saveToStorage(newState);
				return newState;
			});
			this.loadFiles();
		},

		// ========================================================================
		// Error Handling
		// ========================================================================

		clearError(): void {
			update((state) => ({ ...state, error: null }));
		},

		// ========================================================================
		// Reset
		// ========================================================================

		reset(): void {
			set({
				selectedProjectId: null,
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
				showHidden: false
			});
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const files = createFilesStore();

// Derived: Current path breadcrumbs
export const breadcrumbs = derived(files, ($files) => {
	const parts = $files.currentPath.split('/').filter(Boolean);
	const crumbs = [{ name: 'Root', path: '' }];

	let currentPath = '';
	for (const part of parts) {
		currentPath = currentPath ? `${currentPath}/${part}` : part;
		crumbs.push({ name: part, path: currentPath });
	}

	return crumbs;
});

// Derived: Sorted files
export const sortedFiles = derived(files, ($files) => {
	const sorted = [...$files.files];

	sorted.sort((a, b) => {
		// Directories always first
		if (a.type !== b.type) {
			return a.type === 'directory' ? -1 : 1;
		}

		let comparison = 0;
		switch ($files.sortBy) {
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

		return $files.sortAsc ? comparison : -comparison;
	});

	return sorted;
});

// Derived: Has selection
export const hasSelection = derived(files, ($files) => $files.selectedFiles.size > 0);

// Derived: Selection count
export const selectionCount = derived(files, ($files) => $files.selectedFiles.size);
