/**
 * Composer draft persistence (per-tab).
 *
 * The chat composer draft normally lives only in an in-memory Map
 * (SessionEphemeralCache), so a full page reload — e.g. a silent PWA
 * service-worker update — wipes whatever the user was mid-way through typing.
 * This module mirrors the (already debounced) draft into `sessionStorage` so it
 * survives a reload.
 *
 * `sessionStorage` (NOT `localStorage`) is deliberate: every tab has its own
 * `sessionStorage`, matching the app's per-tab active session, whereas
 * `localStorage` is shared across tabs and would let drafts leak between them.
 *
 * Every access is wrapped in try/catch: storage can throw (quota exceeded, or
 * privacy modes where storage is disabled), and a persistence failure must
 * never break the composer — all helpers degrade to silent no-ops.
 */

const PREFIX = '__aihub_draft_';
/** Slot used before a real session id exists (the "new chat" composer). */
const NEW_KEY = '__aihub_draft_new';

/** sessionStorage key for a session's draft, or the "new chat" slot when null. */
export function draftKey(sessionId: string | null): string {
	return sessionId ? `${PREFIX}${sessionId}` : NEW_KEY;
}

/** Read a persisted draft. Returns null when absent or on any storage error. */
export function readDraft(sessionId: string | null): string | null {
	try {
		if (typeof sessionStorage === 'undefined') return null;
		return sessionStorage.getItem(draftKey(sessionId));
	} catch {
		return null;
	}
}

/**
 * Persist (or clear) a draft.
 *
 * Empty / whitespace-only text clears the entry instead of storing it. When a
 * draft is saved for a real session (non-null id) the stale `__aihub_draft_new`
 * slot is removed too, so a draft that started life in the "new chat" composer
 * migrates cleanly to its session once one exists.
 */
export function writeDraft(sessionId: string | null, text: string): void {
	try {
		if (typeof sessionStorage === 'undefined') return;
		const key = draftKey(sessionId);
		if (!text || !text.trim()) {
			sessionStorage.removeItem(key);
		} else {
			sessionStorage.setItem(key, text);
		}
		// Migrate away from the new-chat slot once a real session id exists.
		if (sessionId) sessionStorage.removeItem(NEW_KEY);
	} catch {
		// Storage unavailable / quota exceeded — silent no-op.
	}
}

/** Remove a persisted draft (message sent, or draft intentionally cleared). */
export function clearDraft(sessionId: string | null): void {
	try {
		if (typeof sessionStorage === 'undefined') return;
		sessionStorage.removeItem(draftKey(sessionId));
	} catch {
		// Storage unavailable — silent no-op.
	}
}
