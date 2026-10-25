/**
 * Review-scope rules for the changes panel: which files a scope shows, when
 * the "Changes vs base" scope is available, and how its rows are labelled.
 * Pure helpers so the panel's scope switching is testable without a DOM.
 */
import type { FileChangeInfo } from '$lib/api/git';

/** Sub-filter inside the uncommitted scope. */
export type WorkingFilter = 'all' | 'staged' | 'unstaged';

export function filterWorkingFiles(files: readonly FileChangeInfo[], filter: WorkingFilter): FileChangeInfo[] {
	if (filter === 'all') return [...files];
	return files.filter(f => (filter === 'staged' ? f.category === 'staged' : f.category !== 'staged'));
}

/**
 * Base review compares a session's own checkout with its merge base, so it
 * needs a chat session. Returns why it is off, or null when it can run.
 */
export function baseScopeUnavailableReason(sessionId: string | null | undefined): string | null {
	return sessionId ? null : 'Available in a chat with its own checkout';
}

/**
 * Line comments go to the chat that owns the panel. With no session, a send
 * would start a new chat (and clear the composer draft); with a different live
 * session it would land in the wrong chat. Returns why it is off, or null.
 */
export function askAgentUnavailableReason(
	panelSessionId: string | null | undefined,
	liveSessionId: string | null | undefined,
): string | null {
	if (!panelSessionId) return 'Line comments need an open chat';
	if (liveSessionId !== panelSessionId) return 'Line comments need this chat to be active';
	return null;
}

/** Badge text for a base-scope row's origin. */
export function originLabel(origin: FileChangeInfo['change_origin']): string {
	switch (origin) {
		case 'committed': return 'Committed';
		case 'uncommitted': return 'Uncommitted';
		case 'both': return 'Committed + local';
		default: return '';
	}
}

/** Status word for a row; empty for a plain modification. */
export function statusLabel(file: FileChangeInfo): string {
	if (file.category === 'staged') return 'Staged';
	switch (file.status) {
		case 'M': return '';
		case '?': return 'New';
		case 'A': return 'Added';
		case 'D': return 'Deleted';
		case 'R': return 'Renamed';
		case 'U': return 'Conflict';
		default: return file.category === 'base' ? '' : 'Added';
	}
}

/** Error text from an API failure, falling back to a plain sentence. */
export function errorMessage(error: unknown, fallback: string): string {
	if (error && typeof error === 'object') {
		const e = error as { detail?: unknown; message?: unknown };
		if (typeof e.detail === 'string' && e.detail) return e.detail;
		if (typeof e.message === 'string' && e.message) return e.message;
	}
	return fallback;
}
