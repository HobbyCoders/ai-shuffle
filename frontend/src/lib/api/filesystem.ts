/**
 * Filesystem browse API client
 *
 * Used for the directory tree browser in the Add Project modal.
 * Works in both local mode (full filesystem) and Docker mode (workspace only).
 */

import { api } from './client';

export interface DirectoryEntry {
    name: string;
    path: string;
    is_dir: boolean;
    has_children: boolean;
}

export interface BrowseResponse {
    path: string;
    parent: string | null;
    entries: DirectoryEntry[];
    is_local_mode: boolean;
}

/**
 * Browse the filesystem at the given path.
 * If no path is given, returns starting points (drives on Windows, home/root on Unix).
 */
export async function browseFilesystem(path?: string): Promise<BrowseResponse> {
    const params = path ? `?path=${encodeURIComponent(path)}` : '';
    return api.get<BrowseResponse>(`/filesystem/browse${params}`);
}
