/**
 * Keyboard Shortcut Manager
 *
 * Provides a centralized way to register and handle keyboard shortcuts
 * with support for modifier keys (Cmd/Ctrl, Shift, Alt) and proper
 * Mac vs Windows key handling.
 */

export interface KeyboardShortcut {
	/** Unique identifier for the shortcut */
	id: string;
	/** Description for help display */
	description: string;
	/** Key code (e.g., 'k', 'Enter', 'Escape') */
	key: string;
	/** Require Cmd (Mac) or Ctrl (Windows) */
	cmdOrCtrl?: boolean;
	/** Require Shift key */
	shift?: boolean;
	/** Require Alt/Option key */
	alt?: boolean;
	/** Category for grouping in help modal */
	category: 'navigation' | 'editing' | 'chat' | 'general';
	/** Callback when shortcut is triggered */
	action: () => void;
	/** Whether to prevent default browser behavior */
	preventDefault?: boolean;
	/** Whether shortcut should work when in input fields */
	allowInInput?: boolean;
}

export interface ShortcutRegistration {
	id: string;
	unregister: () => void;
}

// Check if running on Mac
const isMac = typeof navigator !== 'undefined' && /Mac|iPod|iPhone|iPad/.test(navigator.platform);

// Store registered shortcuts
const shortcuts: Map<string, KeyboardShortcut> = new Map();

// Track if the global listener is attached
let listenerAttached = false;

// Track if shortcuts are enabled (can be disabled temporarily)
let shortcutsEnabled = true;

/**
 * Generate a unique key for matching shortcuts
 */
function getShortcutKey(e: KeyboardEvent): string {
	const parts: string[] = [];

	// Use metaKey on Mac, ctrlKey on Windows/Linux for "Cmd/Ctrl"
	if (e.metaKey || e.ctrlKey) parts.push('mod');
	if (e.shiftKey) parts.push('shift');
	if (e.altKey) parts.push('alt');
	parts.push(e.key.toLowerCase());

	return parts.join('+');
}

/**
 * Generate the key string for a shortcut definition
 */
function getDefinitionKey(shortcut: KeyboardShortcut): string {
	const parts: string[] = [];

	if (shortcut.cmdOrCtrl) parts.push('mod');
	if (shortcut.shift) parts.push('shift');
	if (shortcut.alt) parts.push('alt');
	parts.push(shortcut.key.toLowerCase());

	return parts.join('+');
}

/**
 * Check if the event target is an input element
 */
function isInputElement(target: EventTarget | null): boolean {
	if (!target || !(target instanceof Element)) return false;

	const tagName = target.tagName.toLowerCase();
	if (tagName === 'input' || tagName === 'textarea' || tagName === 'select') {
		return true;
	}

	// Check for contenteditable
	if (target instanceof HTMLElement && target.isContentEditable) {
		return true;
	}

	return false;
}

/**
 * Handle keydown events
 */
function handleKeyDown(e: KeyboardEvent): void {
	if (!shortcutsEnabled) return;

	const eventKey = getShortcutKey(e);

	// Find matching shortcut
	const shortcut = shortcuts.get(eventKey);
	if (shortcut) {
		// Check if we should ignore when in input fields
		if (!shortcut.allowInInput && isInputElement(e.target)) {
			// Exception: Allow Escape to always work
			if (shortcut.key.toLowerCase() !== 'escape') {
				return;
			}
		}

		// Prevent default if specified
		if (shortcut.preventDefault !== false) {
			e.preventDefault();
		}

		// Execute the action
		try {
			shortcut.action();
		} catch (err) {
			console.error(`Error executing shortcut ${shortcut.id}:`, err);
		}
	}
}

/**
 * Attach the global event listener
 */
function attachListener(): void {
	if (listenerAttached || typeof window === 'undefined') return;

	window.addEventListener('keydown', handleKeyDown);
	listenerAttached = true;
}

/**
 * Detach the global event listener
 */
function detachListener(): void {
	if (!listenerAttached || typeof window === 'undefined') return;

	window.removeEventListener('keydown', handleKeyDown);
	listenerAttached = false;
}

/**
 * Register a keyboard shortcut
 */
export function registerShortcut(shortcut: KeyboardShortcut): ShortcutRegistration {
	attachListener();

	const key = getDefinitionKey(shortcut);
	shortcuts.set(key, shortcut);

	return {
		id: shortcut.id,
		unregister: () => {
			shortcuts.delete(key);
			if (shortcuts.size === 0) {
				detachListener();
			}
		}
	};
}

/**
 * Unregister a shortcut by ID
 */
export function unregisterShortcut(id: string): void {
	let keyToDelete: string | null = null;
	shortcuts.forEach((shortcut, key) => {
		if (shortcut.id === id) {
			keyToDelete = key;
		}
	});
	if (keyToDelete) {
		shortcuts.delete(keyToDelete);
	}

	if (shortcuts.size === 0) {
		detachListener();
	}
}

/**
 * Get all registered shortcuts (for help display)
 */
export function getShortcuts(): KeyboardShortcut[] {
	const result: KeyboardShortcut[] = [];
	shortcuts.forEach((shortcut) => {
		result.push(shortcut);
	});
	return result;
}

/**
 * Get shortcuts by category
 */
export function getShortcutsByCategory(): Record<string, KeyboardShortcut[]> {
	const byCategory: Record<string, KeyboardShortcut[]> = {
		navigation: [],
		editing: [],
		chat: [],
		general: []
	};

	shortcuts.forEach((shortcut) => {
		if (byCategory[shortcut.category]) {
			byCategory[shortcut.category].push(shortcut);
		}
	});

	return byCategory;
}

/**
 * Temporarily disable all shortcuts
 */
export function disableShortcuts(): void {
	shortcutsEnabled = false;
}

/**
 * Re-enable shortcuts
 */
export function enableShortcuts(): void {
	shortcutsEnabled = true;
}

/**
 * Check if shortcuts are currently enabled
 */
export function areShortcutsEnabled(): boolean {
	return shortcutsEnabled;
}

/**
 * Format a shortcut for display (returns display string like "Cmd+K" or "Ctrl+K")
 */
export function formatShortcut(shortcut: KeyboardShortcut): string {
	const parts: string[] = [];

	if (shortcut.cmdOrCtrl) {
		parts.push(isMac ? 'Cmd' : 'Ctrl');
	}
	if (shortcut.shift) {
		parts.push('Shift');
	}
	if (shortcut.alt) {
		parts.push(isMac ? 'Option' : 'Alt');
	}

	// Format the key nicely
	let keyDisplay = shortcut.key;
	switch (shortcut.key.toLowerCase()) {
		case 'enter':
			keyDisplay = 'Enter';
			break;
		case 'escape':
			keyDisplay = 'Esc';
			break;
		case 'arrowup':
			keyDisplay = 'Up';
			break;
		case 'arrowdown':
			keyDisplay = 'Down';
			break;
		case 'arrowleft':
			keyDisplay = 'Left';
			break;
		case 'arrowright':
			keyDisplay = 'Right';
			break;
		case ' ':
			keyDisplay = 'Space';
			break;
		case '/':
			keyDisplay = '/';
			break;
		case '?':
			keyDisplay = '?';
			break;
		default:
			keyDisplay = shortcut.key.toUpperCase();
	}

	parts.push(keyDisplay);

	return parts.join('+');
}

/**
 * Get the modifier key name for the current platform
 */
export function getModifierKey(): string {
	return isMac ? 'Cmd' : 'Ctrl';
}

/**
 * Check if we're on a Mac
 */
export function isMacPlatform(): boolean {
	return isMac;
}

/**
 * Clear all shortcuts (useful for testing or cleanup)
 */
export function clearAllShortcuts(): void {
	shortcuts.clear();
	detachListener();
}
