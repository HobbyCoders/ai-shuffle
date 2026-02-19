/**
 * Hotkey Preferences Store -- Re-export shim.
 * Implementation lives in hotkeyPreferences.svelte.ts (Svelte 5 runes).
 */
export {
	hotkeyPreferences,
	DEFAULT_PREFERENCES,
	DEFAULT_HOTKEY,
} from './hotkeyPreferences.svelte';

export type {
	HotkeyConfig,
	HotkeyPreferences,
} from './hotkeyPreferences.svelte';
