import { ApiClient } from './client';
import { apiBase, withAuth } from './runtime';
import { isBrowserWorkspaceState, type BrowserWorkspaceState } from '$lib/browser-panel/workspace';

export async function uploadPanelFiles(sessionId: string, identity: {
  viewer_id: string; runtime_id: string; tab_id: string; field: string;
}, files: File[], signal: AbortSignal): Promise<void> {
  if (!files.length || files.length > 10 || files.reduce((size, f) => size + f.size, 0) > 10 * 1024 * 1024)
    throw new Error('Choose up to 10 files, 10 MB total');
  const body = new FormData();
  for (const file of files) body.append('files', file);
  const query = new URLSearchParams(identity);
  const response = await fetch(`${apiBase()}/sessions/${encodeURIComponent(sessionId)}/browser/panel/upload?${query}`, withAuth({ method: 'POST', body, signal }));
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === 'string' ? error.detail : 'File upload failed');
  }
}

const api = new ApiClient();
export async function startPanelBrowser(sessionId: string, signal: AbortSignal): Promise<BrowserWorkspaceState> {
	const result = await api.post<unknown>(
		`/sessions/${encodeURIComponent(sessionId)}/browser/panel/start`, undefined, { signal },
	);
	const state = result;
	if (!isBrowserWorkspaceState(state)) throw new Error('Browser did not return a workspace. Try again.');
	return state;
}
