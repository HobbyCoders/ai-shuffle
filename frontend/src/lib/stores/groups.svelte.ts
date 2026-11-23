import { preferenceSync } from '$lib/services/preferences';
import { compatible } from '$lib/services/preference-values';
/**
 * Groups Store - User-defined grouping for projects, profiles, and subagents
 *
 * Synced to backend database for cross-device persistence.
 * Falls back to localStorage if backend is unavailable.
 * Each entity type (projects, profiles, subagents) has its own groups.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/env';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';

// ─── Types (re-exported) ─────────────────────────────────────────────────────

export type EntityType = 'projects' | 'profiles' | 'subagents';

interface GroupDefinition {
	name: string;
	collapsed: boolean;
}

interface EntityGroups {
	groups: GroupDefinition[];
	assignments: Record<string, string>; // entityId -> groupName
}

interface GroupsState {
	projects: EntityGroups;
	profiles: EntityGroups;
	subagents: EntityGroups;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const STORAGE_KEY = 'aihub_groups';
const API_PREFERENCE_KEY = 'groups';

const defaultState: GroupsState = {
	projects: { groups: [], assignments: {} },
	profiles: { groups: [], assignments: {} },
	subagents: { groups: [], assignments: {} }
};

// ─── Persistence Helpers ─────────────────────────────────────────────────────

/**
 * Load groups from localStorage (fallback/cache)
 */
function loadFromLocalStorage(): GroupsState {
	if (!browser) return defaultState;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored);
			// Merge with defaults to ensure all fields exist
			return {
				projects: { ...defaultState.projects, ...parsed.projects },
				profiles: { ...defaultState.profiles, ...parsed.profiles },
				subagents: { ...defaultState.subagents, ...parsed.subagents }
			};
		}
	} catch (e) {
		console.error('[Groups] Failed to load from localStorage:', e);
	}

	return defaultState;
}

/**
 * Save groups to localStorage (cache)
 */
function saveToLocalStorage(state: GroupsState) {
	if (!browser) return;

	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
	} catch (e) {
		console.error('[Groups] Failed to save to localStorage:', e);
	}
}

/**
 * Load groups from backend API
 */


/**
 * Save groups to backend API (debounced)
 */

/**
 * Immediately flush any pending save to backend (sync, for beforeunload)
 */


// Register beforeunload handler to flush pending saves when browser closes
// (guarded against HMR duplicate registration)
// Persistence lifecycle is owned by live-sync, not module-level unload listeners.

async function saveToBackend(state: GroupsState) {
 preferenceSync.save(API_PREFERENCE_KEY, state);
}

// ─── Reactive State ──────────────────────────────────────────────────────────

let _state = $state<GroupsState>(loadFromLocalStorage());

// Track if we've loaded from backend

// ─── Subscribe Shim (centralized via store-utils) ───────────────────────────

const { subscribe, notifySubscribers } =
	createSubscribers<GroupsState>(
		() => _state,
		(v) => { _state = v; }
	);

function _notifySubscribers() {
	// Auto-save to localStorage + debounced backend on every state change
	if (browser) {
		saveToLocalStorage(_state);
		saveToBackend(_state);
	}

	notifySubscribers();
}

function _update(fn: (s: GroupsState) => GroupsState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

function _set(value: GroupsState) {
	_state = value;
	_notifySubscribers();
}

// ─── Exported Store ──────────────────────────────────────────────────────────

export const groups = {
	get current() { return _state; },

	subscribe,

	/**
	 * Force reload from backend
	 */
	async reload() {
 await preferenceSync.refresh(API_PREFERENCE_KEY);
},

	/**
	 * Create a new group for an entity type
	 */
	createGroup(entityType: EntityType, groupName: string) {
		_update((state) => {
			const entity = state[entityType];
			// Don't create duplicate groups
			if (entity.groups.some((g) => g.name === groupName)) {
				return state;
			}
			return {
				...state,
				[entityType]: {
					...entity,
					groups: [...entity.groups, { name: groupName, collapsed: true }]
				}
			};
		});
	},

	/**
	 * Delete a group (items become ungrouped)
	 */
	deleteGroup(entityType: EntityType, groupName: string) {
		_update((state) => {
			const entity = state[entityType];
			// Remove group
			const groups = entity.groups.filter((g) => g.name !== groupName);
			// Remove assignments to this group
			const assignments: Record<string, string> = {};
			for (const [id, group] of Object.entries(entity.assignments)) {
				if (group !== groupName) {
					assignments[id] = group;
				}
			}
			return {
				...state,
				[entityType]: { groups, assignments }
			};
		});
	},

	/**
	 * Assign an item to a group
	 */
	assignToGroup(entityType: EntityType, itemId: string, groupName: string) {
		_update((state) => {
			const entity = state[entityType];
			// Create group if it doesn't exist
			let groups = entity.groups;
			if (!groups.some((g) => g.name === groupName)) {
				groups = [...groups, { name: groupName, collapsed: true }];
			}
			return {
				...state,
				[entityType]: {
					groups,
					assignments: {
						...entity.assignments,
						[itemId]: groupName
					}
				}
			};
		});
	},

	/**
	 * Remove an item from its group (make ungrouped)
	 */
	removeFromGroup(entityType: EntityType, itemId: string) {
		_update((state) => {
			const entity = state[entityType];
			const { [itemId]: _, ...assignments } = entity.assignments;
			return {
				...state,
				[entityType]: { ...entity, assignments }
			};
		});
	},

	/**
	 * Reset all groups (for testing/debugging)
	 */
	reset() {
		_set(defaultState);
	}
};

// ─── Pure Utility Function (re-exported as-is) ──────────────────────────────

/** The ids an item's group may be saved under (its own id, then any alias). */
export type GroupIds<T> = (item: T) => readonly (string | null | undefined)[];

/**
 * The group an item is in: the first of `ids` assigned to a group that still
 * exists. A picker row can stand for several local clones of one repository;
 * the group may be saved on any of them.
 */
export function groupOf(
	ids: readonly (string | null | undefined)[],
	entityType: EntityType,
	groupsState: GroupsState
): string | null {
	const entity = groupsState[entityType];
	for (const id of ids) {
		const name = id ? entity.assignments[id] : undefined;
		if (name && entity.groups.some((g) => g.name === name)) return name;
	}
	return null;
}

/**
 * Helper function to organize items into groups
 * Returns { grouped: Map<groupName, items[]>, ungrouped: items[] }
 */
export function organizeByGroups<T extends { id: string }>(
	items: T[],
	entityType: EntityType,
	groupsState: GroupsState,
	idsOf: GroupIds<T> = (item) => [item.id]
): { grouped: Map<string, T[]>; ungrouped: T[]; groupOrder: GroupDefinition[] } {
	const entity = groupsState[entityType];
	const grouped = new Map<string, T[]>();
	const ungrouped: T[] = [];

	// Initialize all groups (even if empty)
	for (const group of entity.groups) {
		grouped.set(group.name, []);
	}

	// Distribute items
	for (const item of items) {
		const groupName = groupOf(idsOf(item), entityType, groupsState);
		if (groupName) {
			grouped.get(groupName)!.push(item);
		} else {
			ungrouped.push(item);
		}
	}

	return { grouped, ungrouped, groupOrder: entity.groups };
}

/** A native select's options, in the shape SelectField's `groups` takes. */
export interface ProjectSelectGroup {
	label: string;
	options: { value: string; label: string; disabled?: boolean }[];
}

/**
 * A project select's options split by the user's project groups, for a
 * native <select> (which cannot drill into a folder): `leading` options
 * first ("All projects", "None"), then one labelled section per group that
 * has projects, then "Other projects". With no groups in use, one flat list.
 */
export function projectSelectGroups(
	projects: readonly { id: string; name: string }[],
	groupsState: GroupsState,
	leading: ProjectSelectGroup['options'] = []
): ProjectSelectGroup[] {
	const { grouped, ungrouped, groupOrder } = organizeByGroups([...projects], 'projects', groupsState);
	const option = (p: { id: string; name: string }) => ({ value: p.id, label: p.name });
	const sections = groupOrder
		.map((g) => ({ label: g.name, options: (grouped.get(g.name) ?? []).map(option) }))
		.filter((g) => g.options.length > 0);
	if (sections.length === 0) return [{ label: '', options: [...leading, ...projects.map(option)] }];
	const result: ProjectSelectGroup[] = [];
	if (leading.length) result.push({ label: '', options: leading });
	result.push(...sections);
	if (ungrouped.length) result.push({ label: 'Other projects', options: ungrouped.map(option) });
	return result;
}

preferenceSync.register(API_PREFERENCE_KEY, {
 read: () => _state,
 apply(value) {
  _state = compatible(defaultState, value);
  for (const key of ['projects', 'profiles', 'subagents'] as const) {
   _state[key].groups = _state[key].groups.filter(g => g && typeof g.name === 'string').map(g => ({ ...g, collapsed: !!g.collapsed }));
   _state[key].assignments = Object.fromEntries(Object.entries(_state[key].assignments).filter(([, v]) => typeof v === 'string'));
  }
  saveToLocalStorage(_state); notifySubscribers();
 }
});

