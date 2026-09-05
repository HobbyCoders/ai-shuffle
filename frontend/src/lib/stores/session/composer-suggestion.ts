import { writable } from 'svelte/store';

/** Draft persistence can contain an empty Tiptap document rather than ''. */
export function hasComposerDraft(draft: string | null | undefined): boolean {
	if (!draft) return false;
	try {
		const doc = JSON.parse(draft);
		if (doc?.type !== 'doc' || !Array.isArray(doc.content)) return true;
		const occupied = (node: { type?: string; text?: string; content?: unknown[] }): boolean =>
			node.type === 'text'
				? !!node.text
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
}

/** One mounted composer, one observed turn. Nothing is persisted or restored. */
export function createComposerSuggestion() {
	const value = writable('');
	let context: SuggestionContext | null = null;
	let turn: string | null = null;
	let invalid = true;
	let received = false;
	let pending = '';
	const clear = () => {
		pending = '';
		value.set('');
	};
	function invalidate() {
		invalid = true;
		clear();
	}
	return {
		subscribe: value.subscribe,
		observe(next: SuggestionContext) {
			const switched =
				!context || next.sessionId !== context.sessionId || next.mode !== context.mode;
			const disabled = !next.enabled || next.mode !== 'code' || !next.sessionId;
			if (switched) {
				turn = null;
				invalidate();
			}
			// A local/remote optimistic start can precede the next turn ID. Do not
			// let the previous reply reappear if that attempt ends without a turn.
			if (next.streaming && !context?.streaming && !next.turnId) invalidate();
			// Only an observed live turn arms this composer. Enabling/revisiting an idle
			// session must not resurrect a suggestion from history.
			if (next.turnId && next.turnId !== turn) {
				turn = next.turnId;
				received = false;
				invalid = switched || disabled || next.blocked;
				clear();
			}
			if (disabled || next.blocked || (context && !context.enabled)) invalidate();
			context = next;
			value.set(!invalid && !next.streaming ? pending : '');
		},
		receive(frame: { session_id?: unknown; turn_id?: unknown; suggestion?: unknown }) {
			if (
				!context ||
				invalid ||
				received ||
				frame.session_id !== context.sessionId ||
				frame.turn_id !== turn ||
				typeof frame.suggestion !== 'string' ||
				!frame.suggestion.trim()
			)
				return;
			received = true;
			pending = frame.suggestion;
			if (!context.streaming) value.set(pending);
		},
		invalidate,
		reset() {
			context = null;
			turn = null;
			received = false;
			invalidate();
		},
		accept() {
			const text = !invalid && context && !context.streaming ? pending : '';
			invalidate();
			return text;
		}
	};
}

export const composerSuggestion = createComposerSuggestion();

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
	return event.key === 'Tab' || event.key === 'Enter' ? 'accept' : null;
}
