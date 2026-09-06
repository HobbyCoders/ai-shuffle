import { ApiClient } from './client';
import { isBrowserWorkspaceState, type BrowserWorkspaceState } from '$lib/browser-panel/workspace';

const api = new ApiClient();
export async function startPanelBrowser(sessionId: string, signal: AbortSignal): Promise<BrowserWorkspaceState> {
	const result = await api.post<unknown>(
		`/sessions/${encodeURIComponent(sessionId)}/browser/panel/start`, undefined, { signal },
	);
	const state = result;
	if (!isBrowserWorkspaceState(state)) throw new Error('Browser did not return a workspace. Try again.');
	return state;
}
