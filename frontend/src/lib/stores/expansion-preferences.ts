/** Account-scoped expansion choices; popover visibility and usage data are not preferences. */
import { preferenceSync } from '$lib/services/preferences';
import { createPersistedStore, type PersistedSync } from './persisted';

const BREAKDOWN_KEY = 'aihub.contextBreakdownOpen';
const SECTION_PREFIX = 'aihub.usageSectionOpen:';
const GROUPS_KEY = 'sidebar-collapsed-groups';

function booleanRecord(value: unknown): Record<string, boolean> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? Object.fromEntries(Object.entries(value).filter(([, open]) => typeof open === 'boolean')) : {};
}
const flag = (open: boolean) => open ? '1' : '0';
function keysWithPrefix(storage: Storage, prefix: string): string[] {
	return Array.from({ length: storage.length }, (_, i) => storage.key(i))
		.filter((key): key is string => !!key?.startsWith(prefix));
}

export function createExpansionPreferences(sync: PersistedSync, storage?: Storage) {
	// The breakdown and section caches predate the one-JSON-key convention ('0'/'1' flags,
	// one key per section) and components read them as-is, so they keep their layout.
	const breakdown = createPersistedStore<boolean>({
		key: 'context_breakdown_open', initial: true,
		normalize: value => typeof value === 'boolean' ? value : undefined,
		cache: {
			read: s => { const stored = s.getItem(BREAKDOWN_KEY); return stored === null ? undefined : stored !== '0'; },
			write: (s, open) => s.setItem(BREAKDOWN_KEY, flag(open))
		}
	}, sync, storage);
	const sections = createPersistedStore<Record<string, boolean>>({
		key: 'context_usage_sections', initial: {}, normalize: booleanRecord,
		cache: {
			// Every cached section, including those whose component or provider catalog has not mounted yet.
			read: s => {
				const keys = keysWithPrefix(s, SECTION_PREFIX);
				return keys.length ? Object.fromEntries(keys.map(key => [key.slice(SECTION_PREFIX.length), s.getItem(key) !== '0'])) : undefined;
			},
			write: (s, value) => {
				// Remove absent keys too: account reset must not leave another owner's cache.
				for (const key of keysWithPrefix(s, SECTION_PREFIX)) s.removeItem(key);
				for (const [id, open] of Object.entries(value)) s.setItem(SECTION_PREFIX + id, flag(open));
			}
		}
	}, sync, storage);
	const groups = createPersistedStore<Record<string, boolean>>({
		key: 'sidebar_collapsed_groups', initial: {}, normalize: booleanRecord, storageKey: GROUPS_KEY
	}, sync, storage);
	return { breakdown, sections, groups };
}

let storage: Storage | undefined;
try { storage = globalThis.localStorage; } catch { /* SSR or blocked storage. */ }
export const { breakdown: contextBreakdown, sections: usageSections, groups: sidebarGroups } =
	createExpansionPreferences(preferenceSync, storage);
