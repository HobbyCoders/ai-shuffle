/**
 * The chat message editor as a shortcut target. Kept out of keyboard.ts so a
 * test that stubs the shortcut registry still gets the real check.
 */

/** Marks the chat message editor, so a shortcut can work there but not in other fields. */
export const MESSAGE_EDITOR_ATTRIBUTE = 'data-message-editor';

/**
 * True when a key event comes from the chat message editor and is a finished
 * key press (not part of an IME composition). Lets a modifier shortcut that
 * types nothing, such as Ctrl/Cmd+N, work while the user is writing.
 */
export function isMessageEditorKey(e: KeyboardEvent): boolean {
	if (e.isComposing || e.keyCode === 229) return false;
	const target = e.target;
	return target instanceof Element && !!target.closest(`[${MESSAGE_EDITOR_ATTRIBUTE}]`);
}
