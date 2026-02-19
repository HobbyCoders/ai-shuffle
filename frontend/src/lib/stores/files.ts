/**
 * Files Store — Re-export shim.
 * Implementation lives in files.svelte.ts (Svelte 5 runes).
 */
export {
	files,
	breadcrumbs,
	sortedFiles,
	hasSelection,
	selectionCount,
} from './files.svelte';

export type {
	FileItem,
	FilesState,
} from './files.svelte';
