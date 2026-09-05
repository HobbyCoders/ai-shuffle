import { preferenceSync } from '$lib/services/preferences';
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
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';
import {
	themePresets,
	getPresetById,
	type ThemePreset,
	type ThemeStyles
} from '$lib/themes/presets';
import { createSubscribers } from './store-utils';

type Theme = 'light' | 'dark' | 'system';

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

// Server preference structure
interface ServerThemePreference {
	preference: Theme;
	presetId: string | null;
}

// ============================================================================
// Helper functions (unchanged from original)
// ============================================================================

function clearDirty() {
	if (browser) localStorage.removeItem(DIRTY_FLAG_KEY);
}

function saveToServer(preference: Theme, presetId: string | null) {
 preferenceSync.save(SERVER_PREFERENCE_KEY, { preference, presetId });
}

// Register beforeunload handler (guarded against HMR duplicate registration)
// Persistence lifecycle is owned by live-sync, not module-level unload listeners.

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

function applyTheme(resolved: 'light' | 'dark') {
	if (!browser) return;

	const root = document.documentElement;
	if (resolved === 'dark') {
		root.classList.add('dark');
		root.classList.remove('light');
	} else {
		root.classList.add('light');
		root.classList.remove('dark');
	}

	const metaThemeColor = document.querySelector('meta[name="theme-color"]');
	if (metaThemeColor) {
		metaThemeColor.setAttribute('content', resolved === 'dark' ? '#0f0f0f' : '#ffffff');
	}
}

function applyPresetStyles(preset: ThemePreset | null, resolvedTheme: 'light' | 'dark') {
	if (!browser) return;

	const root = document.documentElement;

	root.removeAttribute('data-theme-preset');

	const style = root.style;
	const propsToRemove: string[] = [];
	for (let i = 0; i < style.length; i++) {
		const prop = style[i];
		if (prop.startsWith('--preset-')) {
			propsToRemove.push(prop);
		}
	}
	propsToRemove.forEach((prop) => style.removeProperty(prop));

	const cssVars = [
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
		'--chart-1',
		'--chart-2',
		'--chart-3',
		'--chart-4',
		'--chart-5',
		'--sidebar',
		'--sidebar-foreground',
		'--sidebar-primary',
		'--sidebar-primary-foreground',
		'--sidebar-accent',
		'--sidebar-accent-foreground',
		'--sidebar-border',
		'--sidebar-ring',
		'--font-sans',
		'--font-serif',
		'--font-mono',
		'--letter-spacing',
		'--radius',
		'--spacing',
		'--shadow-color',
		'--shadow-opacity',
		'--shadow-blur',
		'--shadow-spread',
		'--shadow-offset-x',
		'--shadow-offset-y',
		'--shadow-s',
		'--shadow-m',
		'--shadow-l'
	];
	cssVars.forEach((v) => style.removeProperty(v));

	if (!preset) return;

	const styles: ThemeStyles =
		resolvedTheme === 'dark' ? preset.styles.dark : preset.styles.light;

	root.setAttribute('data-theme-preset', preset.id);

	Object.entries(styles).forEach(([key, value]) => {
		if (value !== undefined && value !== null) {
			style.setProperty(`--${key}`, value);
		}
	});

	const shadowColor = styles['shadow-color'];
	const shadowOpacity = styles['shadow-opacity'];
	const shadowBlur = styles['shadow-blur'];
	const shadowSpread = styles['shadow-spread'];
	const shadowOffsetX = styles['shadow-offset-x'];
	const shadowOffsetY = styles['shadow-offset-y'];

	if (shadowColor && shadowOpacity && shadowBlur) {
		let colorWithOpacity: string;
		if (shadowColor.startsWith('hsl')) {
			colorWithOpacity = shadowColor.replace(')', ` / ${shadowOpacity})`);
		} else {
			colorWithOpacity = `color-mix(in srgb, ${shadowColor} ${parseFloat(shadowOpacity) * 100}%, transparent)`;
		}

		const offsetX = shadowOffsetX || '0px';
		const offsetY = shadowOffsetY || '4px';
		const blur = shadowBlur || '8px';
		const spread = shadowSpread || '0px';

		const shadowBase = `${offsetX} ${offsetY} ${blur} ${spread} ${colorWithOpacity}`;

		style.setProperty('--shadow-s', `0 1px 3px ${colorWithOpacity}`);
		style.setProperty('--shadow-m', shadowBase);
		style.setProperty(
			'--shadow-l',
			`${offsetX} calc(${offsetY} * 2) calc(${blur} * 1.5) ${spread} ${colorWithOpacity}`
		);
	}
}

// ============================================================================
// Reactive State
// ============================================================================

const initialPreference = getStoredPreference();
const initialResolved = resolveTheme(initialPreference);
const initialPresetId = getStoredPresetId();

let _state = $state<ThemeState>({
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

// Listen for system theme changes (guarded against HMR duplicate registration)
if (browser && !(window as any).__theme_media_listener) {
	(window as any).__theme_media_listener = true;
	const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
	mediaQuery.addEventListener('change', () => {
		if (_state.preference === 'system') {
			const newResolved = getSystemTheme();
			applyTheme(newResolved);
			const preset = _state.presetId ? getPresetById(_state.presetId) : null;
			applyPresetStyles(preset ?? null, newResolved);
			_state = { ..._state, resolved: newResolved };
			_notifySubscribers();
		}
	});
}

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<ThemeState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const theme = {
	get current() {
		return _state;
	},

	subscribe,

	/** Set theme preference */
	setTheme(preference: Theme) {
		const resolved = resolveTheme(preference);

		if (browser) {
			localStorage.setItem(STORAGE_KEY, preference);
			applyTheme(resolved);
			const preset = _state.presetId ? getPresetById(_state.presetId) : null;
			applyPresetStyles(preset ?? null, resolved);
			saveToServer(preference, _state.presetId);
		}

		_state = { ..._state, preference, resolved };
		_notifySubscribers();
	},

	/** Toggle between light and dark (ignores system) */
	toggle() {
		const newPreference: Theme = _state.resolved === 'dark' ? 'light' : 'dark';
		const newResolved = newPreference;

		if (browser) {
			localStorage.setItem(STORAGE_KEY, newPreference);
			applyTheme(newResolved);
			const preset = _state.presetId ? getPresetById(_state.presetId) : null;
			applyPresetStyles(preset ?? null, newResolved);
			saveToServer(newPreference, _state.presetId);
		}

		_state = { ..._state, preference: newPreference, resolved: newResolved };
		_notifySubscribers();
	},

	/** Set theme preset */
	setPreset(presetId: string | null) {
		if (browser) {
			if (presetId) {
				localStorage.setItem(PRESET_STORAGE_KEY, presetId);
			} else {
				localStorage.removeItem(PRESET_STORAGE_KEY);
			}
			const preset = presetId ? getPresetById(presetId) : null;
			applyPresetStyles(preset ?? null, _state.resolved);
			saveToServer(_state.preference, presetId);
		}

		_state = { ..._state, presetId };
		_notifySubscribers();
	},

	/** Initialize theme on mount (call this in layout) */
	init() {
		if (browser) {
			const preference = getStoredPreference();
			const resolved = resolveTheme(preference);
			const presetId = getStoredPresetId();
			applyTheme(resolved);
			const preset = presetId ? getPresetById(presetId) : null;
			applyPresetStyles(preset ?? null, resolved);
			_state = { preference, resolved, presetId };
			_notifySubscribers();
		}
	},

	/**
	 * Sync theme from server (call after authentication).
	 * Server is the source of truth UNLESS local has unsaved changes (dirty flag).
	 */
	async syncFromServer() {
 await preferenceSync.refresh(SERVER_PREFERENCE_KEY);
}
};

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

export const themePreference = createDerived(() => _state.preference);
export const currentPresetId = createDerived(() => _state.presetId);

// Re-export theme presets for use in UI
export { themePresets };
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => ({ preference: _state.preference, presetId: _state.presetId }),
 apply(value) {
  const raw = value as Partial<ServerThemePreference> | undefined;
  const preference = raw && ['system', 'dark', 'light'].includes(raw.preference!) ? raw.preference! : 'system';
  const presetId = typeof raw?.presetId === 'string' ? raw.presetId : null;
  const resolved = resolveTheme(preference);
  if (browser) {
   localStorage.setItem(STORAGE_KEY, preference);
   if (presetId) localStorage.setItem(PRESET_STORAGE_KEY, presetId); else localStorage.removeItem(PRESET_STORAGE_KEY);
   clearDirty(); applyTheme(resolved); applyPresetStyles(presetId ? getPresetById(presetId) ?? null : null, resolved);
  }
  _state = { preference, presetId, resolved }; _notifySubscribers();
 }
});
