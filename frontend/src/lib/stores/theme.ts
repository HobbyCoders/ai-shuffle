/**
 * Theme store — Re-export shim.
 * Implementation lives in theme.svelte.ts (Svelte 5 runes).
 */
export {
	theme,
	themePreference,
	isDark,
	isLight,
	currentPresetId,
	themePresets,
	getPresetById,
} from './theme.svelte';

export type {
	Theme,
	ThemePreset,
} from './theme.svelte';
