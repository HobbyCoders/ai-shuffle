/**
 * The data-ui-* attributes the UI flight recorder reads
 * (browser-mcp/src/recorder/flight-recorder.js; guide:
 * docs/internal/flight-recorder.md). This file is the one list of them.
 *
 * They only describe what the view is showing; nothing in the app reads them,
 * so adding or changing one never changes behaviour. Values must stay short,
 * stable and free of user data: no titles, message text, paths or full ids.
 *
 * State carrier: ChatView's root, `data-ui-state="chat"`. The recorder records
 * every data-ui-* attribute on a carrier each animation frame:
 *   data-ui-surface    new | cleared | loading | active  (classifyChatSurface)
 *   data-ui-session    none | s:<6-char hash of the bound session id>
 *   data-ui-composer   centre (new-chat hero) | bottom (docked) | hidden (archived)
 *   data-ui-messages   transcript message count, pending peer messages included
 *   data-ui-loading    true while history loads (open, boot restore, deep link)
 *   data-ui-streaming  true while the session streams a reply
 *
 * Mount markers, `data-ui-mount="<Name>"`, counted as they mount and unmount:
 *   ChatView, ChatInput, NewSessionHero, MessageArea, SidePanel
 *
 * Transcript rows, `data-ui-row`: every message row carries its message id,
 * at the top level and inside work logs and system groups (rows without an id
 * carry none); a top-level group carries its group key. The same value on the
 * page twice is a duplicate row.
 *
 * Adding a field: put `data-ui-<name>` on the carrier (or another element with
 * data-ui-state), feed it a short enum-like value built here, and list it above
 * and in docs/internal/flight-recorder.md.
 */

export const UI_STATE_CHAT = 'chat';

export const UI_MOUNT = {
	chatView: 'ChatView',
	chatInput: 'ChatInput',
	newSessionHero: 'NewSessionHero',
	messageArea: 'MessageArea',
	sidePanel: 'SidePanel'
} as const;

export type UiComposer = 'centre' | 'bottom' | 'hidden';

/**
 * A short, stable tag for the bound session: tells two sessions apart in a
 * recording without putting the id on the page. FNV-1a, base36, 6 chars.
 */
export function uiSessionTag(sessionId: string | null | undefined): string {
	if (!sessionId) return 'none';
	let hash = 0x811c9dc5;
	for (let i = 0; i < sessionId.length; i += 1) {
		hash ^= sessionId.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return `s:${hash.toString(36).padStart(6, '0').slice(-6)}`;
}

/** Where the composer sits: under the new-chat hero, docked at the bottom, or replaced (archived chat). */
export function uiComposer(input: { archived: boolean; emptyHero: boolean }): UiComposer {
	if (input.archived) return 'hidden';
	return input.emptyHero ? 'centre' : 'bottom';
}

export function uiFlag(value: unknown): 'true' | 'false' {
	return value ? 'true' : 'false';
}
