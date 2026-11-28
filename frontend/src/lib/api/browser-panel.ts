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

/** A Chrome or Edge connected through the AI Shuffle browser extension. */
export interface ExtensionBrowser {
	id: string;
	name: string;
	product?: string;
	version?: string;
}

/** Which browser a chat uses: `browser_id` null is the built-in browser. */
export interface BrowserTarget {
	browser_id: string | null;
	assigned: boolean;
	/** Browsers this caller may hand to the chat (their own account's). */
	browsers: ExtensionBrowser[];
	/** The chat runs on another worker, which picks its browser. */
	remote?: boolean;
}

function isBrowserTarget(value: unknown): value is BrowserTarget {
	const v = value as BrowserTarget | null;
	return !!v && typeof v === 'object' && (v.browser_id === null || typeof v.browser_id === 'string') &&
		Array.isArray(v.browsers) && v.browsers.every((b) => !!b && typeof b.id === 'string' && typeof b.name === 'string');
}

export async function getBrowserTarget(sessionId: string, signal?: AbortSignal): Promise<BrowserTarget | null> {
	const result = await api.get<unknown>(`/sessions/${encodeURIComponent(sessionId)}/browser/target`, { signal });
	return isBrowserTarget(result) ? result : null;
}

export async function setBrowserTarget(sessionId: string, browserId: string | null): Promise<BrowserTarget | null> {
	const result = await api.put<unknown>(`/sessions/${encodeURIComponent(sessionId)}/browser/target`, { browser_id: browserId });
	return isBrowserTarget(result) ? result : null;
}
