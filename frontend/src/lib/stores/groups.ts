/**
 * Groups Store - User-defined grouping for projects, profiles, and subagents
 *
 * Synced to backend database for cross-device persistence.
 * Falls back to localStorage if backend is unavailable.
 * Each entity type (projects, profiles, subagents) has its own groups.
 */

import { writable, derived, get } from 'svelte/store';
import { browser } from '$app/environment';

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

const STORAGE_KEY = 'aihub_groups';
const API_PREFERENCE_KEY = 'groups';

const defaultState: GroupsState = {
	projects: { groups: [], assignments: {} },
	profiles: { groups: [], assignments: {} },
	subagents: { groups: [], assignments: {} }
};

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
			if (data.value) {
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

function createGroupsStore() {
	// Start with localStorage data (fast initial load)
	const { subscribe, set, update } = writable<GroupsState>(loadFromLocalStorage());

	// Track if we've loaded from backend
	let backendLoaded = false;

	// Load from backend asynchronously and merge/update
	if (browser) {
		loadFromBackend().then(async (backendState) => {
			if (backendState) {
				// Backend has data, use it (more authoritative)
				set(backendState);
				saveToLocalStorage(backendState);
				backendLoaded = true;
			} else {
				// No backend data, sync localStorage to backend
				const localState = get({ subscribe });
				if (localState.projects.groups.length > 0 ||
					localState.profiles.groups.length > 0 ||
					localState.subagents.groups.length > 0) {
					// Wait for the save to complete before marking as loaded
					// This prevents race conditions where changes might be lost
					try {
						await fetch(`/api/v1/preferences/${API_PREFERENCE_KEY}`, {
							method: 'PUT',
							headers: { 'Content-Type': 'application/json' },
							credentials: 'include',
							body: JSON.stringify({ key: API_PREFERENCE_KEY, value: localState })
						});
					} catch (e) {
						console.error('[Groups] Failed to sync initial state to backend:', e);
					}
				}
				backendLoaded = true;
			}
		});
	}

	// Auto-save on changes (to both localStorage and backend)
	subscribe((state) => {
		saveToLocalStorage(state);
		if (backendLoaded) {
			saveToBackend(state);
		}
	});

	return {
		subscribe,

		/**
		 * Force reload from backend
		 */
		async reload() {
			const backendState = await loadFromBackend();
			if (backendState) {
				set(backendState);
				saveToLocalStorage(backendState);
			}
		},

		/**
		 * Create a new group for an entity type
		 */
		createGroup(entityType: EntityType, groupName: string) {
			update((state) => {
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
			update((state) => {
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
			update((state) => {
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
			update((state) => {
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
			update((state) => {
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
			update((state) => {
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
			update((state) => {
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
			const state = get({ subscribe });
			return state[entityType].assignments[itemId];
		},

		/**
		 * Reorder groups
		 */
		reorderGroups(entityType: EntityType, groups: GroupDefinition[]) {
			update((state) => ({
				...state,
				[entityType]: {
					...state[entityType],
					groups
				}
			}));
		},

		/**
		 * Clean up assignments for items that no longer exist
		 */
		cleanupAssignments(entityType: EntityType, existingIds: string[]) {
			update((state) => {
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
			set(defaultState);
		}
	};
}

export const groups = createGroupsStore();

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

/**
 * Derived store helpers for specific entity types
 */
export const projectGroups = derived(groups, ($groups) => $groups.projects);
export const profileGroups = derived(groups, ($groups) => $groups.profiles);
export const subagentGroups = derived(groups, ($groups) => $groups.subagents);
