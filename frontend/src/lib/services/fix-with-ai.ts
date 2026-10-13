/**
 * "Fix with AI" — turn an error toast into a ready-to-send debugging chat.
 *
 * Opens a fresh chat (keeping the user's persisted profile/project) and
 * pre-fills the input with a prompt that tells the agent to pull the recent
 * errors via the ``GetAppErrors`` MCP tool and fix the root cause. Review-first:
 * the draft is *not* auto-sent — the user reads it and hits send.
 *
 * Mirrors the new-chat navigation in ``routes/+page.svelte`` (newChat → openChat,
 * collapsing the sidebar first when it's an overlay).
 */

import { activeSession } from '$lib/stores/active-session';
import { layout } from '$lib/stores/layout.svelte';

export interface FixWithAiError {
	message: string;
	source?: string;
	detail?: string;
	url?: string;
	status_code?: number;
	session_id?: string;
}

/** Build the pre-filled prompt. Exported for tests. */
export function buildFixPrompt(err: FixWithAiError): string {
	const lines: string[] = [
		'I hit this error in AI Shuffle. Use the `GetAppErrors` tool to pull the recent errors',
		'(pass `this_session_only: true` if it helps narrow things down), find the root cause, and fix it.',
		'',
		'Error details:',
		`- message: ${err.message}`,
	];
	if (err.source) lines.push(`- source: ${err.source}`);
	if (err.status_code) lines.push(`- status: ${err.status_code}`);
	if (err.url) lines.push(`- url: ${err.url}`);
	if (err.detail) {
		lines.push('', 'Detail:', '```', err.detail.slice(0, 4000), '```');
	}
	return lines.join('\n');
}

/**
 * Start a new chat pre-filled with a prompt to debug ``err``. Does not send —
 * the user reviews the draft and submits it themselves.
 */
export function startFixChat(err: FixWithAiError): void {
	const prompt = buildFixPrompt(err);

	activeSession.newChat();
	activeSession.setDraft(prompt);

	// Match +page.svelte: on an overlay sidebar, collapse it first, then open
	// the chat on the next frame so the layout settles.
	if (layout.current.sidebarOverlay && !layout.current.sidebarCollapsed) {
		layout.setSidebarCollapsed(true);
		requestAnimationFrame(() => layout.openChat(null));
	} else {
		layout.openChat(null);
	}
}
