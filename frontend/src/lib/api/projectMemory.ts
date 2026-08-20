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

export interface SaveMemoryRequest {
	content: string;
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
 * Get a specific memory file by location
 */
export async function getMemoryFile(
	projectId: string,
	location: MemoryLocation
): Promise<MemoryFile> {
	return api.get(`/projects/${projectId}/memory/${location}`);
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
