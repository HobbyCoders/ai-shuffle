/**
 * Hooks Management Store
 *
 * Manages state for project hooks:
 * - Custom hooks per project
 * - Plugin-contributed hooks (read-only)
 * - Hook enable/disable state
 * - Hook testing
 */

import { writable, derived, get } from 'svelte/store';
import type { Hook, HookCreate, HookUpdate, HookTestResult, PluginHookInfo } from '$lib/api/client';
import * as api from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

export interface HooksState {
	hooks: Hook[];
	pluginHooks: PluginHookInfo[];
	currentProjectId: string | null;
	loading: boolean;
	testing: Record<string, boolean>; // hook id -> testing state
	saving: Record<string, boolean>; // hook id -> saving state
	error: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: HooksState = {
	hooks: [],
	pluginHooks: [],
	currentProjectId: null,
	loading: false,
	testing: {},
	saving: {},
	error: null
};

// ============================================================================
// Store Creation
// ============================================================================

function createHooksStore() {
	const { subscribe, set, update } = writable<HooksState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Project Hooks Operations
		// ========================================================================

		/**
		 * Load hooks for a project
		 */
		async loadProjectHooks(projectId: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

			try {
				const hooks = await api.getProjectHooks(projectId);
				update((state) => ({ ...state, hooks, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load hooks';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Create a new hook
		 */
		async createHook(projectId: string, data: HookCreate): Promise<Hook> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const hook = await api.createHook(projectId, data);
				update((state) => ({
					...state,
					hooks: [hook, ...state.hooks],
					loading: false
				}));
				return hook;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to create hook';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Update a hook
		 */
		async updateHook(hookId: string, data: HookUpdate): Promise<Hook> {
			update((state) => ({
				...state,
				saving: { ...state.saving, [hookId]: true },
				error: null
			}));

			try {
				const hook = await api.updateHook(hookId, data);
				update((state) => ({
					...state,
					hooks: state.hooks.map((h) => (h.id === hookId ? hook : h)),
					saving: { ...state.saving, [hookId]: false }
				}));
				return hook;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to update hook';
				update((state) => ({
					...state,
					saving: { ...state.saving, [hookId]: false },
					error
				}));
				throw e;
			}
		},

		/**
		 * Delete a hook
		 */
		async deleteHook(hookId: string): Promise<void> {
			update((state) => ({
				...state,
				saving: { ...state.saving, [hookId]: true },
				error: null
			}));

			try {
				await api.deleteHook(hookId);
				update((state) => ({
					...state,
					hooks: state.hooks.filter((h) => h.id !== hookId),
					saving: { ...state.saving, [hookId]: false }
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to delete hook';
				update((state) => ({
					...state,
					saving: { ...state.saving, [hookId]: false },
					error
				}));
				throw e;
			}
		},

		/**
		 * Toggle hook enabled state
		 */
		async toggleHook(hookId: string): Promise<void> {
			const state = get({ subscribe });
			const hook = state.hooks.find((h) => h.id === hookId);
			if (hook) {
				await this.updateHook(hookId, { enabled: !hook.enabled });
			}
		},

		/**
		 * Test a hook with sample event data
		 */
		async testHook(hookId: string): Promise<HookTestResult> {
			update((state) => ({
				...state,
				testing: { ...state.testing, [hookId]: true },
				error: null
			}));

			try {
				const result = await api.testHook(hookId);
				update((state) => ({
					...state,
					testing: { ...state.testing, [hookId]: false }
				}));
				return result;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to test hook';
				update((state) => ({
					...state,
					testing: { ...state.testing, [hookId]: false },
					error
				}));
				throw e;
			}
		},

		// ========================================================================
		// Plugin Hooks Operations
		// ========================================================================

		/**
		 * Load all plugin-contributed hooks
		 */
		async loadPluginHooks(): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const pluginHooks = await api.getPluginHooks();
				update((state) => ({ ...state, pluginHooks, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load plugin hooks';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load all hooks (project + plugin)
		 */
		async loadAll(projectId: string): Promise<void> {
			update((state) => ({
				...state,
				loading: true,
				error: null,
				currentProjectId: projectId
			}));

			try {
				const [hooks, pluginHooks] = await Promise.all([
					api.getProjectHooks(projectId),
					api.getPluginHooks()
				]);

				update((state) => ({
					...state,
					hooks,
					pluginHooks,
					loading: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load hooks';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Clear error state
		 */
		clearError(): void {
			update((state) => ({ ...state, error: null }));
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			set(initialState);
		},

		/**
		 * Set current project (for switching without reloading)
		 */
		setCurrentProject(projectId: string | null): void {
			update((state) => ({
				...state,
				currentProjectId: projectId,
				hooks: projectId === state.currentProjectId ? state.hooks : []
			}));
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const hooks = createHooksStore();

// All custom hooks for current project
export const projectHooks = derived(hooks, ($hooks) => $hooks.hooks);

// Enabled hooks only
export const enabledHooks = derived(hooks, ($hooks) =>
	$hooks.hooks.filter((h) => h.enabled)
);

// Disabled hooks only
export const disabledHooks = derived(hooks, ($hooks) =>
	$hooks.hooks.filter((h) => !h.enabled)
);

// Hooks grouped by event type
export const hooksByEvent = derived(hooks, ($hooks) => {
	const groups: Record<string, Hook[]> = {};
	for (const hook of $hooks.hooks) {
		if (!groups[hook.event]) {
			groups[hook.event] = [];
		}
		groups[hook.event].push(hook);
	}
	return groups;
});

// Plugin hooks
export const pluginHooks = derived(hooks, ($hooks) => $hooks.pluginHooks);

// Plugin hooks grouped by plugin
export const pluginHooksByPlugin = derived(hooks, ($hooks) => {
	const groups: Record<string, PluginHookInfo[]> = {};
	for (const hook of $hooks.pluginHooks) {
		if (!groups[hook.plugin_id]) {
			groups[hook.plugin_id] = [];
		}
		groups[hook.plugin_id].push(hook);
	}
	return groups;
});

// Current project ID
export const currentProjectId = derived(hooks, ($hooks) => $hooks.currentProjectId);

// Loading state
export const hooksLoading = derived(hooks, ($hooks) => $hooks.loading);

// Error state
export const hooksError = derived(hooks, ($hooks) => $hooks.error);

// Testing state for hooks
export const hookTesting = derived(hooks, ($hooks) => $hooks.testing);

// Saving state for hooks
export const hookSaving = derived(hooks, ($hooks) => $hooks.saving);

// Stats
export const hookStats = derived(hooks, ($hooks) => ({
	totalCustom: $hooks.hooks.length,
	totalEnabled: $hooks.hooks.filter((h) => h.enabled).length,
	totalPlugin: $hooks.pluginHooks.length,
	byType: {
		bash: $hooks.hooks.filter((h) => h.hook_type === 'bash').length,
		webhook: $hooks.hooks.filter((h) => h.hook_type === 'webhook').length
	}
}));
