import { sidePanel, sessionPanelKey } from './side-panel.svelte';

/** The HTML artifact a session's side panel shows beside the chat. */
export interface HtmlArtifactRef {
	href: string;
	filename: string;
}

let artifacts = $state<Record<string, HtmlArtifactRef>>({});

export const htmlArtifactPanel = {
	forSession(sessionId: string | null | undefined): HtmlArtifactRef | null {
		return sessionId ? artifacts[sessionPanelKey(sessionId)] ?? null : null;
	},
	open(sessionId: string | null | undefined, artifact: HtmlArtifactRef) {
		if (!sessionId) return;
		const key = sessionPanelKey(sessionId);
		artifacts[key] = { ...artifact };
		sidePanel.open(key, 'artifact');
	},
	isShowing(sessionId: string | null | undefined, href: string): boolean {
		if (!sessionId) return false;
		const key = sessionPanelKey(sessionId);
		return sidePanel.forScope(key).active === 'artifact' && artifacts[key]?.href === href;
	},
	close(sessionId: string | null | undefined) {
		if (sessionId) sidePanel.close(sessionPanelKey(sessionId), 'artifact');
	}
};
