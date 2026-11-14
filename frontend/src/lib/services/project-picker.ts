/**
 * The new-session screen's quick project picker, opened with Cmd/Ctrl+P.
 *
 * AppPage owns the shortcut (so it is listed in Keyboard shortcuts); the
 * new-session row provides the picker while it is on screen. With no row on
 * screen the shortcut declines, and the browser's own Cmd/Ctrl+P (print) runs.
 */
import { ariaKeyShortcut, formatShortcutHint } from './keyboard';

export const PROJECT_PICKER_SHORTCUT = { key: 'p', cmdOrCtrl: true } as const;

/** "⌘P" on a Mac, "Ctrl+P" elsewhere. */
export const projectPickerShortcutHint = () => formatShortcutHint(PROJECT_PICKER_SHORTCUT);
export const projectPickerAriaShortcut = () => ariaKeyShortcut(PROJECT_PICKER_SHORTCUT);

/** Opens the picker; false when there is none to open right now. */
type Opener = () => boolean;
const openers: Opener[] = [];

/** Offer a picker while mounted; returns the function that withdraws it. */
export function provideProjectPicker(open: Opener): () => void {
	openers.push(open);
	return () => {
		const index = openers.lastIndexOf(open);
		if (index >= 0) openers.splice(index, 1);
	};
}

/** Open the newest picker on offer. False when none is (nothing happened). */
export function openProjectPicker(): boolean {
	return openers.at(-1)?.() ?? false;
}

/** Marks the open picker, so Cmd/Ctrl+P typed in its search keeps it (not print). */
export const PROJECT_PICKER_ATTRIBUTE = 'data-project-picker';

/** True when a key event comes from inside the open project picker. */
export function isProjectPickerKey(e: KeyboardEvent): boolean {
	const target = e.target;
	return target instanceof Element && !!target.closest(`[${PROJECT_PICKER_ATTRIBUTE}]`);
}
