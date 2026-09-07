/** Account-scoped expansion choices; popover visibility and usage data are not preferences. */
import { preferenceSync } from '$lib/services/preferences';
import type { PreferenceSync } from '$lib/services/preference-sync';
import { createStore } from './store-utils';

const BREAKDOWN_KEY = 'aihub.contextBreakdownOpen';
const SECTION_PREFIX = 'aihub.usageSectionOpen:';
const GROUPS_KEY = 'sidebar-collapsed-groups';

function booleanRecord(value: unknown): Record<string, boolean> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? Object.fromEntries(Object.entries(value).filter(([, open]) => typeof open === 'boolean')) : {};
}

export function createExpansionPreferences(sync: Pick<PreferenceSync, 'register' | 'save'>, storage?: Storage) {
	function cached<T>(read: (storage: Storage) => T, fallback: T): T {
		try { return storage ? read(storage) : fallback; } catch { return fallback; }
	}
	function preference<T>(key: string, initial: T, normalize: (value: unknown) => T, persist: (value: T) => void) {
		const state = createStore(initial);
		function apply(raw: unknown) {
			const value = normalize(raw);
			state.set(value);
			try { persist(value); } catch { /* A blocked cache does not prevent account sync. */ }
		}
		sync.register(key, { read: () => state.current, apply });
		return {
			subscribe: state.subscribe,
			get current() { return state.current; },
			set(value: T) { apply(value); sync.save(key, state.current); }
		};
	}

	const breakdown = preference('context_breakdown_open',
		cached(s => s.getItem(BREAKDOWN_KEY) !== '0', true),
		value => typeof value === 'boolean' ? value : true,
		value => storage?.setItem(BREAKDOWN_KEY, value ? '1' : '0'));
	const sections = preference('context_usage_sections', cached(s => {
		const values: [string, boolean][] = [];
		for (let i = 0; i < s.length; i++) {
			const key = s.key(i);
			if (key?.startsWith(SECTION_PREFIX)) values.push([key.slice(SECTION_PREFIX.length), s.getItem(key) !== '0']);
		}
		return Object.fromEntries(values);
	}, {}), booleanRecord, value => {
		// Remove absent keys too: account reset must not leave another owner's cache.
		const old = cached(s => Array.from({ length: s.length }, (_, i) => s.key(i)), []);
		for (const key of old) if (key?.startsWith(SECTION_PREFIX)) storage?.removeItem(key);
		for (const [id, open] of Object.entries(value)) storage?.setItem(SECTION_PREFIX + id, open ? '1' : '0');
	});
	const groups = preference('sidebar_collapsed_groups',
		cached(s => booleanRecord(JSON.parse(s.getItem(GROUPS_KEY) || '{}')), {}),
		booleanRecord, value => storage?.setItem(GROUPS_KEY, JSON.stringify(value)));
	return { breakdown, sections, groups };
}

let storage: Storage | undefined;
try { storage = globalThis.localStorage; } catch { /* SSR or blocked storage. */ }
export const { breakdown: contextBreakdown, sections: usageSections, groups: sidebarGroups } =
	createExpansionPreferences(preferenceSync, storage);
