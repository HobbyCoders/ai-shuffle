/**
 * Theme store for dark/light mode management and theme presets
 *
 * Features:
 * - Persists preference in localStorage (for instant load, no flash)
 * - Syncs to server for cross-device persistence
 * - Detects system preference on first load
 * - Applies theme class to <html> element
 * - Provides reactive state for UI components
 * - Supports theme presets from tweakcn
 */

import { writable, derived, get } from 'svelte/store';
import { browser } from '$app/environment';
import { themePresets, getPresetById, type ThemePreset, type ThemeStyles } from '$lib/themes/presets';
import { getPreference, setPreference } from '$lib/api/client';

export type Theme = 'light' | 'dark' | 'system';

interface ThemeState {
	preference: Theme; // User's preference (light, dark, or system)
	resolved: 'light' | 'dark'; // Actual applied theme
	presetId: string | null; // Current theme preset ID (null = default)
}

const STORAGE_KEY = 'ai-shuffle-theme';
const PRESET_STORAGE_KEY = 'ai-shuffle-theme-preset';
const DIRTY_FLAG_KEY = 'ai-shuffle-theme-dirty';
const SERVER_PREFERENCE_KEY = 'theme';

// Server sync state
let hasSyncedFromServer = false;
let pendingThemeState: { preference: Theme; presetId: string | null } | null = null;

// Server preference structure
interface ServerThemePreference {
	preference: Theme;
	presetId: string | null;
}

/**
 * Load theme preference from server
 */
async function loadFromServer(): Promise<ServerThemePreference | null> {
	try {
		const response = await getPreference<ServerThemePreference>(SERVER_PREFERENCE_KEY);
		return response?.value as ServerThemePreference || null;
	} catch (e) {
		console.error('[Theme] Failed to load from server:', e);
		return null;
	}
}

/**
 * Mark local theme state as dirty (unsaved to server).
 * Checked by syncFromServer to avoid overwriting local changes with stale server data.
 */
function markDirty() {
	if (browser) localStorage.setItem(DIRTY_FLAG_KEY, 'true');
}

function clearDirty() {
	if (browser) localStorage.removeItem(DIRTY_FLAG_KEY);
}

function isDirty(): boolean {
	if (!browser) return false;
	return localStorage.getItem(DIRTY_FLAG_KEY) === 'true';
}

/**
 * Save theme preference to server immediately.
 * Sets a dirty flag before saving so syncFromServer won't overwrite
 * local changes if the save fails (e.g., during a container restart).
 */
function saveToServer(preference: Theme, presetId: string | null) {
	if (!browser) return;

	// Track pending state for flush on unload
	pendingThemeState = { preference, presetId };
	markDirty();

	// Don't write to server until initial sync is complete
	// This prevents localStorage data from overwriting newer server data
	if (!hasSyncedFromServer) {
		return;
	}

	// Save immediately — theme changes are infrequent user actions
	(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, { preference, presetId });
			clearDirty();
			pendingThemeState = null;
		} catch (e) {
			console.error('[Theme] Failed to save to server:', e);
		}
	})();
}

/**
 * Flush pending theme save immediately (for beforeunload)
 */
function flushPendingThemeSave() {
	if (!pendingThemeState || !hasSyncedFromServer || !browser) return;

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({
		key: SERVER_PREFERENCE_KEY,
		value: pendingThemeState
	});
	navigator.sendBeacon(
		'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
		new Blob([data], { type: 'application/json' })
	);
	pendingThemeState = null;
}

// Register beforeunload handler
if (browser) {
	window.addEventListener('beforeunload', flushPendingThemeSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingThemeSave();
		}
	});
}

function getSystemTheme(): 'light' | 'dark' {
	if (!browser) return 'dark';
	return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function getStoredPreference(): Theme {
	if (!browser) return 'system';
	const stored = localStorage.getItem(STORAGE_KEY);
	if (stored === 'light' || stored === 'dark' || stored === 'system') {
		return stored;
	}
	return 'system';
}

function getStoredPresetId(): string | null {
	if (!browser) return null;
	return localStorage.getItem(PRESET_STORAGE_KEY);
}

function resolveTheme(preference: Theme): 'light' | 'dark' {
	if (preference === 'system') {
		return getSystemTheme();
	}
	return preference;
}

function applyTheme(theme: 'light' | 'dark') {
	if (!browser) return;

	const root = document.documentElement;
	if (theme === 'dark') {
		root.classList.add('dark');
		root.classList.remove('light');
	} else {
		root.classList.add('light');
		root.classList.remove('dark');
	}

	// Update theme-color meta tag for mobile browsers
	const metaThemeColor = document.querySelector('meta[name="theme-color"]');
	if (metaThemeColor) {
		metaThemeColor.setAttribute('content', theme === 'dark' ? '#0f0f0f' : '#ffffff');
	}
}

/**
 * Apply a theme preset's CSS variables to the document
 */
function applyPresetStyles(preset: ThemePreset | null, resolvedTheme: 'light' | 'dark') {
	if (!browser) return;

	const root = document.documentElement;

	// Remove any existing preset styles
	root.removeAttribute('data-theme-preset');

	// Remove all custom properties set by previous presets
	const style = root.style;
	const propsToRemove: string[] = [];
	for (let i = 0; i < style.length; i++) {
		const prop = style[i];
		if (prop.startsWith('--preset-')) {
			propsToRemove.push(prop);
		}
	}
	propsToRemove.forEach((prop) => style.removeProperty(prop));

	// Also clear the main CSS variables that presets set
	const cssVars = [
		// Colors
		'--background',
		'--foreground',
		'--card',
		'--card-foreground',
		'--popover',
		'--popover-foreground',
		'--primary',
		'--primary-foreground',
		'--secondary',
		'--secondary-foreground',
		'--muted',
		'--muted-foreground',
		'--accent',
		'--accent-foreground',
		'--destructive',
		'--destructive-foreground',
		'--border',
		'--input',
		'--ring',
		// Chart colors
		'--chart-1',
		'--chart-2',
		'--chart-3',
		'--chart-4',
		'--chart-5',
		// Sidebar colors
		'--sidebar',
		'--sidebar-foreground',
		'--sidebar-primary',
		'--sidebar-primary-foreground',
		'--sidebar-accent',
		'--sidebar-accent-foreground',
		'--sidebar-border',
		'--sidebar-ring',
		// Typography
		'--font-sans',
		'--font-serif',
		'--font-mono',
		'--letter-spacing',
		// Layout
		'--radius',
		'--spacing',
		// Shadows (individual properties from tweakcn)
		'--shadow-color',
		'--shadow-opacity',
		'--shadow-blur',
		'--shadow-spread',
		'--shadow-offset-x',
		'--shadow-offset-y',
		// Composed shadows (our system, also set by preset application)
		'--shadow-s',
		'--shadow-m',
		'--shadow-l'
	];
	cssVars.forEach((v) => style.removeProperty(v));

	if (!preset) {
		// No preset, use default theme from app.css
		return;
	}

	// Apply the preset's styles for the current theme mode
	const styles: ThemeStyles = resolvedTheme === 'dark' ? preset.styles.dark : preset.styles.light;

	root.setAttribute('data-theme-preset', preset.id);

	// Apply each CSS variable
	Object.entries(styles).forEach(([key, value]) => {
		if (value !== undefined && value !== null) {
			style.setProperty(`--${key}`, value);
		}
	});

	// Compose shadow variables from individual properties if provided
	// tweakcn uses: shadow-color, shadow-opacity, shadow-blur, shadow-spread, shadow-offset-x, shadow-offset-y
	const shadowColor = styles['shadow-color'];
	const shadowOpacity = styles['shadow-opacity'];
	const shadowBlur = styles['shadow-blur'];
	const shadowSpread = styles['shadow-spread'];
	const shadowOffsetX = styles['shadow-offset-x'];
	const shadowOffsetY = styles['shadow-offset-y'];

	if (shadowColor && shadowOpacity && shadowBlur) {
		// Parse shadow color and apply opacity
		// Support both hsl() and hex formats
		let colorWithOpacity: string;
		if (shadowColor.startsWith('hsl')) {
			// hsl(0 0% 0%) -> hsl(0 0% 0% / opacity)
			colorWithOpacity = shadowColor.replace(')', ` / ${shadowOpacity})`);
		} else {
			// Hex or other - wrap in color-mix for opacity
			colorWithOpacity = `color-mix(in srgb, ${shadowColor} ${parseFloat(shadowOpacity) * 100}%, transparent)`;
		}

		const offsetX = shadowOffsetX || '0px';
		const offsetY = shadowOffsetY || '4px';
		const blur = shadowBlur || '8px';
		const spread = shadowSpread || '0px';

		// Create shadow variants (small, medium, large) based on the preset's shadow
		const shadowBase = `${offsetX} ${offsetY} ${blur} ${spread} ${colorWithOpacity}`;

		// Scale for different sizes
		style.setProperty('--shadow-s', `0 1px 3px ${colorWithOpacity}`);
		style.setProperty('--shadow-m', shadowBase);
		style.setProperty('--shadow-l', `${offsetX} calc(${offsetY} * 2) calc(${blur} * 1.5) ${spread} ${colorWithOpacity}`);
	}
}

function createThemeStore() {
	const initialPreference = getStoredPreference();
	const initialResolved = resolveTheme(initialPreference);
	const initialPresetId = getStoredPresetId();

	const { subscribe, set, update } = writable<ThemeState>({
		preference: initialPreference,
		resolved: initialResolved,
		presetId: initialPresetId
	});

	// Apply initial theme and preset
	if (browser) {
		applyTheme(initialResolved);
		const preset = initialPresetId ? getPresetById(initialPresetId) : null;
		applyPresetStyles(preset ?? null, initialResolved);
	}

	// Listen for system theme changes
	if (browser) {
		const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
		mediaQuery.addEventListener('change', () => {
			update((state) => {
				if (state.preference === 'system') {
					const newResolved = getSystemTheme();
					applyTheme(newResolved);
					// Re-apply preset for new theme mode
					const preset = state.presetId ? getPresetById(state.presetId) : null;
					applyPresetStyles(preset ?? null, newResolved);
					return { ...state, resolved: newResolved };
				}
				return state;
			});
		});
	}

	return {
		subscribe,

		/**
		 * Set theme preference
		 */
		setTheme(preference: Theme) {
			update((state) => {
				const resolved = resolveTheme(preference);

				if (browser) {
					localStorage.setItem(STORAGE_KEY, preference);
					applyTheme(resolved);
					// Re-apply preset for new theme mode
					const preset = state.presetId ? getPresetById(state.presetId) : null;
					applyPresetStyles(preset ?? null, resolved);
					// Sync to server
					saveToServer(preference, state.presetId);
				}

				return { ...state, preference, resolved };
			});
		},

		/**
		 * Toggle between light and dark (ignores system)
		 */
		toggle() {
			update((state) => {
				const newPreference: Theme = state.resolved === 'dark' ? 'light' : 'dark';
				const newResolved = newPreference;

				if (browser) {
					localStorage.setItem(STORAGE_KEY, newPreference);
					applyTheme(newResolved);
					// Re-apply preset for new theme mode
					const preset = state.presetId ? getPresetById(state.presetId) : null;
					applyPresetStyles(preset ?? null, newResolved);
					// Sync to server
					saveToServer(newPreference, state.presetId);
				}

				return { ...state, preference: newPreference, resolved: newResolved };
			});
		},

		/**
		 * Set theme preset
		 */
		setPreset(presetId: string | null) {
			update((state) => {
				if (browser) {
					if (presetId) {
						localStorage.setItem(PRESET_STORAGE_KEY, presetId);
					} else {
						localStorage.removeItem(PRESET_STORAGE_KEY);
					}
					const preset = presetId ? getPresetById(presetId) : null;
					applyPresetStyles(preset ?? null, state.resolved);
					// Sync to server
					saveToServer(state.preference, presetId);
				}

				return { ...state, presetId };
			});
		},

		/**
		 * Initialize theme on mount (call this in layout)
		 * Applies theme from localStorage immediately to prevent flash
		 */
		init() {
			if (browser) {
				const preference = getStoredPreference();
				const resolved = resolveTheme(preference);
				const presetId = getStoredPresetId();
				applyTheme(resolved);
				const preset = presetId ? getPresetById(presetId) : null;
				applyPresetStyles(preset ?? null, resolved);
				set({ preference, resolved, presetId });
			}
		},

		/**
		 * Sync theme from server (call after authentication)
		 * Server is the source of truth UNLESS local has unsaved changes (dirty flag).
		 * If server is empty but local has data, push local to server (migration).
		 */
		async syncFromServer() {
			if (!browser) return;

			const serverState = await loadFromServer();
			const localState = get({ subscribe });
			const localIsDirty = isDirty();

			if (localIsDirty) {
				// Local has unsaved changes — local wins, push to server
				hasSyncedFromServer = true;
				saveToServer(localState.preference, localState.presetId);
				return;
			}

			if (serverState) {
				// Server has data and local is clean — server is source of truth
				const { preference, presetId } = serverState;

				// Check if server differs from local
				if (preference !== localState.preference || presetId !== localState.presetId) {
					// Update localStorage
					localStorage.setItem(STORAGE_KEY, preference);
					if (presetId) {
						localStorage.setItem(PRESET_STORAGE_KEY, presetId);
					} else {
						localStorage.removeItem(PRESET_STORAGE_KEY);
					}

					// Update store and apply
					const resolved = resolveTheme(preference);
					applyTheme(resolved);
					const preset = presetId ? getPresetById(presetId) : null;
					applyPresetStyles(preset ?? null, resolved);
					set({ preference, resolved, presetId });
				}
			} else {
				// No server data - push local data to server (migration)
				// Will be saved after hasSyncedFromServer is set
			}

			// Mark sync as complete - now server writes are allowed
			hasSyncedFromServer = true;

			// If there was no server data and local has non-default state, save it now
			if (!serverState && (localState.preference !== 'system' || localState.presetId)) {
				saveToServer(localState.preference, localState.presetId);
			}
		}
	};
}

export const theme = createThemeStore();

// Derived stores for convenience
export const themePreference = derived(theme, ($theme) => $theme.preference);
export const isDark = derived(theme, ($theme) => $theme.resolved === 'dark');
export const isLight = derived(theme, ($theme) => $theme.resolved === 'light');
export const currentPresetId = derived(theme, ($theme) => $theme.presetId);

// Re-export theme presets for use in UI
export { themePresets, getPresetById };
export type { ThemePreset };
