/**
 * Project Memory (AGENTS.md) Management API Client
 *
 * Provides functions for managing AGENTS.md memory files in project directories.
 * Two locations per project:
 * - "project": <project>/AGENTS.md (shared, version controlled)
 * - "local":   <project>/.agents/AGENTS.md (private, gitignored)
 *
 * Legacy CLAUDE.md / .claude/CLAUDE.md files are read as a fallback when no
 * AGENTS.md exists; saves always write the AGENTS.md path.
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export type MemoryLocation = 'project' | 'local';

export interface MemoryFile {
	location: MemoryLocation;
	exists: boolean;
	content: string;
	file_path: string;
	size: number;
	updated_at: string | null;
}

/**
 * Agent-memory import: copies a provider's per-project memory dir
 * (~/.claude/projects/<slug>/memory, or the codex equivalent) into the
 * project's own fluid memory dir. Admin-only, non-destructive — an existing
 * destination file is reported as a conflict and left alone.
 */
export type MemoryImportProvider = 'claude' | 'codex';

export interface MemoryImportItem {
	relative_path: string;
	size: number;
	status: 'ready' | 'conflict';
}

export interface MemoryImportPreview {
	provider: MemoryImportProvider;
	source_dir: string | null;
	destination_dir: string | null;
	items: MemoryImportItem[];
	ready_count: number;
	conflict_count: number;
}

export interface MemoryImportSkip {
	relative_path: string;
	reason: string;
}

export interface MemoryImportResult {
	imported: string[];
	skipped: MemoryImportSkip[];
	source_dir: string | null;
	destination_dir: string | null;
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Get all memory file locations with their current state
 */
export async function getMemoryFiles(projectId: string): Promise<MemoryFile[]> {
	return api.get(`/projects/${projectId}/memory`);
}

/**
 * Create or update a memory file
 */
export async function saveMemoryFile(
	projectId: string,
	location: MemoryLocation,
	content: string
): Promise<MemoryFile> {
	return api.put(`/projects/${projectId}/memory/${location}`, { content });
}

/**
 * Delete a memory file
 */
export async function deleteMemoryFile(
	projectId: string,
	location: MemoryLocation
): Promise<void> {
	return api.delete(`/projects/${projectId}/memory/${location}`);
}

/**
 * List what an import from a provider's memory dir would copy.
 * A provider with no dir on disk previews as empty rather than failing.
 */
export async function previewMemoryImport(
	projectId: string,
	provider: MemoryImportProvider
): Promise<MemoryImportPreview> {
	return api.post(`/projects/${projectId}/memory-import/preview`, { provider });
}

/**
 * Copy provider memory files into the project's memory dir.
 * Omit relativePaths to import everything that is ready.
 */
export async function applyMemoryImport(
	projectId: string,
	provider: MemoryImportProvider,
	relativePaths?: string[]
): Promise<MemoryImportResult> {
	return api.post(`/projects/${projectId}/memory-import/apply`, {
		provider,
		relative_paths: relativePaths ?? null
	});
}
