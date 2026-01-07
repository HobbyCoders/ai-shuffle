/**
 * Theme store for dark/light mode management
 *
 * Features:
 * - Persists preference in localStorage
 * - Detects system preference on first load
 * - Applies theme class to <html> element
 * - Provides reactive state for UI components
 */

import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';

export type Theme = 'light' | 'dark' | 'system';

interface ThemeState {
	preference: Theme; // User's preference (light, dark, or system)
	resolved: 'light' | 'dark'; // Actual applied theme
}

const STORAGE_KEY = 'ai-hub-theme';

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

function createThemeStore() {
	const initialPreference = getStoredPreference();
	const initialResolved = resolveTheme(initialPreference);

	const { subscribe, set, update } = writable<ThemeState>({
		preference: initialPreference,
		resolved: initialResolved
	});

	// Apply initial theme
	if (browser) {
		applyTheme(initialResolved);
	}

	// Listen for system theme changes
	if (browser) {
		const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
		mediaQuery.addEventListener('change', () => {
			update(state => {
				if (state.preference === 'system') {
					const newResolved = getSystemTheme();
					applyTheme(newResolved);
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
			const resolved = resolveTheme(preference);

			if (browser) {
				localStorage.setItem(STORAGE_KEY, preference);
				applyTheme(resolved);
			}

			set({ preference, resolved });
		},

		/**
		 * Toggle between light and dark (ignores system)
		 */
		toggle() {
			update(state => {
				const newPreference: Theme = state.resolved === 'dark' ? 'light' : 'dark';
				const newResolved = newPreference;

				if (browser) {
					localStorage.setItem(STORAGE_KEY, newPreference);
					applyTheme(newResolved);
				}

				return { preference: newPreference, resolved: newResolved };
			});
		},

		/**
		 * Initialize theme on mount (call this in layout)
		 */
		init() {
			if (browser) {
				const preference = getStoredPreference();
				const resolved = resolveTheme(preference);
				applyTheme(resolved);
				set({ preference, resolved });
			}
		}
	};
}

export const theme = createThemeStore();

// Derived stores for convenience
export const themePreference = derived(theme, $theme => $theme.preference);
export const isDark = derived(theme, $theme => $theme.resolved === 'dark');
export const isLight = derived(theme, $theme => $theme.resolved === 'light');
