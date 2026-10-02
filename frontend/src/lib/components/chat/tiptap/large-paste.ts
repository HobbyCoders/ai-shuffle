/**
 * Large pastes become a text-file attachment instead of flooding the composer.
 *
 * A log or a whole source file pasted inline makes the editor slow and the
 * message hard to read back. At or above the threshold the paste is uploaded as
 * a `.txt` attachment (a file chip). Ctrl/Cmd+Shift+V keeps any paste inline.
 */

/** Pastes at or above this many UTF-8 bytes become an attachment. */
export const LARGE_PASTE_BYTES = 32 * 1024;

export function pasteByteLength(text: string): number {
	return new TextEncoder().encode(text).length;
}

export function isLargePaste(text: string): boolean {
	// Every UTF-16 unit is at least one UTF-8 byte, and at most three.
	if (text.length >= LARGE_PASTE_BYTES) return true;
	if (text.length * 3 < LARGE_PASTE_BYTES) return false;
	return pasteByteLength(text) >= LARGE_PASTE_BYTES;
}

function pad(n: number): string {
	return String(n).padStart(2, '0');
}

/** The attachment a large paste becomes: `pasted-text-20261002-101530.txt`. */
export function pastedTextFile(text: string, now: Date = new Date()): File {
	const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
		`${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
	return new File([text], `pasted-text-${stamp}.txt`, { type: 'text/plain' });
}

/**
 * Tracks Ctrl/Cmd+Shift+V. The paste event itself carries no modifier state,
 * so the shortcut's keydown arms a one-shot flag the paste handler consumes.
 */
export function createPlainPasteIntent(now: () => number = () => Date.now()) {
	let armedAt: number | null = null;
	return {
		/** Feed every editor keydown. */
		keydown(event: Pick<KeyboardEvent, 'key' | 'shiftKey' | 'ctrlKey' | 'metaKey' | 'altKey'>) {
			if ((event.ctrlKey || event.metaKey) && event.shiftKey && !event.altKey && event.key.toLowerCase() === 'v') {
				armedAt = now();
			}
		},
		/** True once for the paste that follows the shortcut. */
		consume(): boolean {
			const armed = armedAt !== null && now() - armedAt < 1000;
			armedAt = null;
			return armed;
		}
	};
}
