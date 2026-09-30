import { writable } from 'svelte/store';

/** Draft persistence can contain an empty Tiptap document rather than ''. */
export function hasComposerDraft(draft: string | null | undefined): boolean {
	if (!draft?.trim()) return false;
	try {
		const doc = JSON.parse(draft);
		if (doc?.type !== 'doc' || !Array.isArray(doc.content)) return true;
		const occupied = (node: { type?: string; text?: string; content?: unknown[] }): boolean =>
			node.type === 'text'
				? !!node.text?.trim()
				: node.type === 'hardBreak' ? false
				: !['doc', 'paragraph'].includes(node.type ?? '') ||
					(node.content ?? []).some(child => occupied(child as Parameters<typeof occupied>[0]));
		return occupied(doc);
	} catch {
		return true;
	}
}

export interface SuggestionContext {
	sessionId: string | null;
	mode: string;
	enabled: boolean;
	turnId: string | null;
	streaming: boolean;
	blocked: boolean;
	connected?: boolean;
}

export interface ComposerSuggestion {
	session_id: string;
	turn_id: string;
	suggestion: string;
}

/** The server retains hints; the mounted composer only owns their presentation. */
export function createComposerSuggestion(
	load: (sessionId: string) => Promise<ComposerSuggestion | null> = async () => null
) {
	const value = writable('');
	let context: SuggestionContext | null = null;
	let current: ComposerSuggestion | null = null;
	let revision = 0;
	// Keep acceptance/dismissal across composer remounts and session switches.
	const dismissed = new Map<string, string>();
	const eligible = (c: SuggestionContext | null) =>
		!!c?.sessionId && c.enabled && c.mode === 'code' && !c.streaming && !c.blocked;
	function hide() {
		revision++;
		current = null;
		value.set('');
	}
	async function refresh() {
		if (!eligible(context)) return;
		const sessionId = context!.sessionId!;
		const request = ++revision;
		try {
			const hint = await load(sessionId);
			if (request !== revision || context?.sessionId !== sessionId || !eligible(context)) return;
			current = hint && hint.session_id === sessionId && typeof hint.turn_id === 'string' &&
				typeof hint.suggestion === 'string' && hint.suggestion.trim() ? hint : null;
			value.set(current && dismissed.get(sessionId) !== current.turn_id ? current.suggestion : '');
		} catch {
			// Optional UI assistance; a reconnect/switch retries this read, never generation.
		}
	}
	function invalidate() {
		const turnId = current?.turn_id ?? context?.turnId;
		if (context?.sessionId && turnId) {
			dismissed.delete(context.sessionId);
			dismissed.set(context.sessionId, turnId);
			if (dismissed.size > 256) dismissed.delete(dismissed.keys().next().value!);
		}
		hide();
	}
	return {
		subscribe: value.subscribe,
		observe(next: SuggestionContext) {
			const changed = next.sessionId !== context?.sessionId || next.mode !== context?.mode ||
				next.enabled !== context?.enabled || next.streaming !== context?.streaming || next.turnId !== context?.turnId ||
				next.blocked !== context?.blocked || next.connected !== context?.connected;
			context = next;
			if (changed) {
				hide();
				void refresh();
			}
		},
		receive(frame: { session_id?: unknown }) {
			// A WS notification is only a wake for an authoritative read. Do not
			// depend on observing turn.started (SSE folds may coalesce it away).
			if (frame.session_id === context?.sessionId) void refresh();
		},
		refresh,
		invalidate,
		detach() { context = null; hide(); },
		reset() { context = null; dismissed.clear(); hide(); },
		accept() {
			const text = eligible(context) && current && dismissed.get(current.session_id) !== current.turn_id
				? current.suggestion : '';
			invalidate();
			return text;
		}
	};
}

export const composerSuggestion = createComposerSuggestion(async sessionId => {
	const { api } = await import('$lib/api/client');
	return api.get<ComposerSuggestion | null>(`/sessions/${encodeURIComponent(sessionId)}/composer-suggestion`);
});

export function suggestionKey(
	event: Pick<
		KeyboardEvent,
		'key' | 'shiftKey' | 'ctrlKey' | 'altKey' | 'metaKey' | 'isComposing' | 'repeat' | 'keyCode'
	>
): 'accept' | 'dismiss' | null {
	if (
		event.isComposing ||
		event.keyCode === 229 ||
		event.repeat ||
		event.shiftKey ||
		event.ctrlKey ||
		event.altKey ||
		event.metaKey
	)
		return null;
	if (event.key === 'Escape') return 'dismiss';
	return event.key === 'Tab' ? 'accept' : null;
}
