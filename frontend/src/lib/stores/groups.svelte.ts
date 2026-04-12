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

export interface GroupDefinition {
	name: string;
	collapsed: boolean;
}

export interface EntityGroups {
	groups: GroupDefinition[];
	assignments: Record<string, string>; // entityId -> groupName
}

export interface GroupsState {
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
async function loadFromBackend(): Promise<GroupsState | null> {
	if (!browser) return null;

	try {
		const response = await fetch(`/api/v1/preferences/${API_PREFERENCE_KEY}`, {
			credentials: 'include'
		});

		if (response.ok) {
			const data = await response.json();
			if (data && data.value) {
				// Merge with defaults to ensure all fields exist
				return {
					projects: { ...defaultState.projects, ...data.value.projects },
					profiles: { ...defaultState.profiles, ...data.value.profiles },
					subagents: { ...defaultState.subagents, ...data.value.subagents }
				};
			}
		} else if (response.status === 404) {
			// No groups saved yet, that's fine
			return null;
		}
	} catch (e) {
		console.error('[Groups] Failed to load from backend:', e);
	}

	return null;
}

/**
 * Save groups to backend API (debounced)
 */
let saveTimeout: ReturnType<typeof setTimeout> | null = null;
let pendingState: GroupsState | null = null;

/**
 * Immediately flush any pending save to backend (sync, for beforeunload)
 */
function flushPendingGroupsSave() {
	if (!pendingState) return;

	// Cancel the debounce timer
	if (saveTimeout) {
		clearTimeout(saveTimeout);
		saveTimeout = null;
	}

	// Use sendBeacon for reliable delivery during page unload
	// sendBeacon is designed for this use case and won't be cancelled
	const data = JSON.stringify({ key: API_PREFERENCE_KEY, value: pendingState });
	navigator.sendBeacon(`/api/v1/preferences/${API_PREFERENCE_KEY}`, new Blob([data], { type: 'application/json' }));

	pendingState = null;
}

// Register beforeunload handler to flush pending saves when browser closes
// (guarded against HMR duplicate registration)
if (browser && !(window as any).__groups_listeners) {
	(window as any).__groups_listeners = true;
	window.addEventListener('beforeunload', flushPendingGroupsSave);
	// Also handle visibility change (tab switch, minimize) - more reliable on mobile
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingGroupsSave();
		}
	});
}

async function saveToBackend(state: GroupsState) {
	if (!browser) return;

	// Store the pending state
	pendingState = state;

	// Debounce saves to avoid excessive API calls
	if (saveTimeout) {
		clearTimeout(saveTimeout);
	}

	saveTimeout = setTimeout(async () => {
		if (!pendingState) return;

		try {
			await fetch(`/api/v1/preferences/${API_PREFERENCE_KEY}`, {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ key: API_PREFERENCE_KEY, value: pendingState })
			});
		} catch (e) {
			console.error('[Groups] Failed to save to backend:', e);
		}

		pendingState = null;
		saveTimeout = null;
	}, 500); // 500ms debounce
}

// ─── Reactive State ──────────────────────────────────────────────────────────

let _state = $state<GroupsState>(loadFromLocalStorage());

// Track if we've loaded from backend
let backendLoaded = false;

// ─── Subscribe Shim (centralized via store-utils) ───────────────────────────

const { subscribe, notifySubscribers, createDerived } =
	createSubscribers<GroupsState>(
		() => _state,
		(v) => { _state = v; }
	);

function _notifySubscribers() {
	// Auto-save to localStorage + debounced backend on every state change
	if (browser) {
		saveToLocalStorage(_state);
		if (backendLoaded) {
			saveToBackend(_state);
		}
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

// ─── Async Backend Initialization ────────────────────────────────────────────

if (browser) {
	loadFromBackend().then(async (backendState) => {
		if (backendState) {
			// Backend has data, use it (more authoritative)
			_state = backendState;
			saveToLocalStorage(backendState);
			backendLoaded = true;
			// Notify subscribers of the backend-loaded state
			notifySubscribers();
		} else {
			// No backend data, sync localStorage to backend
			if (_state.projects.groups.length > 0 ||
				_state.profiles.groups.length > 0 ||
				_state.subagents.groups.length > 0) {
				// Wait for the save to complete before marking as loaded
				// This prevents race conditions where changes might be lost
				try {
					await fetch(`/api/v1/preferences/${API_PREFERENCE_KEY}`, {
						method: 'PUT',
						headers: { 'Content-Type': 'application/json' },
						credentials: 'include',
						body: JSON.stringify({ key: API_PREFERENCE_KEY, value: _state })
					});
				} catch (e) {
					console.error('[Groups] Failed to sync initial state to backend:', e);
				}
			}
			backendLoaded = true;
		}
	});
}

// ─── Exported Store ──────────────────────────────────────────────────────────

export const groups = {
	get current() { return _state; },

	subscribe,

	/**
	 * Force reload from backend
	 */
	async reload() {
		const backendState = await loadFromBackend();
		if (backendState) {
			_set(backendState);
			saveToLocalStorage(backendState);
		}
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
	 * Rename a group
	 */
	renameGroup(entityType: EntityType, oldName: string, newName: string) {
		_update((state) => {
			const entity = state[entityType];
			// Update group name
			const groups = entity.groups.map((g) =>
				g.name === oldName ? { ...g, name: newName } : g
			);
			// Update all assignments using this group
			const assignments: Record<string, string> = {};
			for (const [id, group] of Object.entries(entity.assignments)) {
				assignments[id] = group === oldName ? newName : group;
			}
			return {
				...state,
				[entityType]: { groups, assignments }
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
	 * Toggle group collapsed state
	 */
	toggleGroupCollapsed(entityType: EntityType, groupName: string) {
		_update((state) => {
			const entity = state[entityType];
			const groups = entity.groups.map((g) =>
				g.name === groupName ? { ...g, collapsed: !g.collapsed } : g
			);
			return {
				...state,
				[entityType]: { ...entity, groups }
			};
		});
	},

	/**
	 * Set group collapsed state
	 */
	setGroupCollapsed(entityType: EntityType, groupName: string, collapsed: boolean) {
		_update((state) => {
			const entity = state[entityType];
			const groups = entity.groups.map((g) =>
				g.name === groupName ? { ...g, collapsed } : g
			);
			return {
				...state,
				[entityType]: { ...entity, groups }
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
	 * Get the group name for an item (or undefined if ungrouped)
	 */
	getItemGroup(entityType: EntityType, itemId: string): string | undefined {
		return _state[entityType].assignments[itemId];
	},

	/**
	 * Reorder groups
	 */
	reorderGroups(entityType: EntityType, groupDefs: GroupDefinition[]) {
		_update((state) => ({
			...state,
			[entityType]: {
				...state[entityType],
				groups: groupDefs
			}
		}));
	},

	/**
	 * Clean up assignments for items that no longer exist
	 */
	cleanupAssignments(entityType: EntityType, existingIds: string[]) {
		_update((state) => {
			const entity = state[entityType];
			const existingSet = new Set(existingIds);
			const assignments: Record<string, string> = {};
			for (const [id, group] of Object.entries(entity.assignments)) {
				if (existingSet.has(id)) {
					assignments[id] = group;
				}
			}
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

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────

export const projectGroups = createDerived(() => _state.projects);
export const profileGroups = createDerived(() => _state.profiles);
export const subagentGroups = createDerived(() => _state.subagents);
