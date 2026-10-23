import { NOT_HANDLED, type KeyboardShortcut } from '$lib/services/keyboard';

/** One app-level modal the global Escape can close. */
export interface ModalLayer {
	isOpen: () => boolean;
	close: () => void;
}

/**
 * AppPage's global Escape shortcut (`close-modal`). It closes the first open
 * layer, in the order given. With nothing open it returns `NOT_HANDLED`, so
 * Escape stays un-prevented and the side panel (browser, terminal, phone) can
 * take it.
 */
export function closeModalShortcut(layers: ModalLayer[]): KeyboardShortcut {
	return {
		id: 'close-modal',
		description: 'Close modal',
		key: 'Escape',
		category: 'general',
		allowInInput: true,
		action: () => {
			const layer = layers.find((l) => l.isOpen());
			if (!layer) return NOT_HANDLED;
			layer.close();
		}
	};
}
