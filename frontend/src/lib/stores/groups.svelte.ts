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

import { browser } from '$app/environment';
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

/**
 * Helper function to organize items into groups
 * Returns { grouped: Map<groupName, items[]>, ungrouped: items[] }
 */
export function organizeByGroups<T extends { id: string }>(
	items: T[],
	entityType: EntityType,
	groupsState: GroupsState
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
		const groupName = entity.assignments[item.id];
		if (groupName && grouped.has(groupName)) {
			grouped.get(groupName)!.push(item);
		} else {
			ungrouped.push(item);
		}
	}

	return { grouped, ungrouped, groupOrder: entity.groups };
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

