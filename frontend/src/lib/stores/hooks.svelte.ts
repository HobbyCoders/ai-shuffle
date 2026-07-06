/**
 * Hooks Management Store
 *
 * Manages state for hooks:
 * - App-level hooks (project_id is null)
 * - Project-scoped hooks
 * - Plugin-contributed hooks (read-only)
 * - Hook enable/disable state
 * - Hook testing
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type { Hook, HookCreate, HookUpdate, HookTestResult, PluginHookInfo } from '$lib/api/client';
import * as api from '$lib/api/client';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

interface HooksState {
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
// Reactive State
// ============================================================================

let _state = $state<HooksState>({ ...initialState });

// ============================================================================
// Subscribe Shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<HooksState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const hooks = {
	get current() {
		return _state;
	},

	subscribe,

	// ========================================================================
	// Project Hooks Operations
	// ========================================================================

	/**
	 * Load hooks for a project (includes app-level hooks)
	 */
	async loadProjectHooks(projectId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

		try {
			const hooksData = await api.getProjectHooks(projectId);
			_update((state) => ({ ...state, hooks: hooksData, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load hooks';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load app-level hooks only (no project context)
	 */
	async loadAppHooks(): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const [appHooksData, pluginHooksData] = await Promise.all([
				api.getAppHooks(),
				api.getPluginHooks()
			]);
			_update((state) => ({
				...state,
				hooks: appHooksData,
				pluginHooks: pluginHooksData,
				loading: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load app hooks';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Create a new project-scoped hook
	 */
	async createHook(projectId: string, data: HookCreate): Promise<Hook> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const hook = await api.createHook(projectId, data);
			_update((state) => ({
				...state,
				hooks: [hook, ...state.hooks],
				loading: false
			}));
			return hook;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to create hook';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Create a new app-level hook (no project)
	 */
	async createAppHook(data: HookCreate): Promise<Hook> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const hook = await api.createAppHook(data);
			_update((state) => ({
				...state,
				hooks: [hook, ...state.hooks],
				loading: false
			}));
			return hook;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to create app hook';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Update a hook
	 */
	async updateHook(hookId: string, data: HookUpdate): Promise<Hook> {
		_update((state) => ({
			...state,
			saving: { ...state.saving, [hookId]: true },
			error: null
		}));

		try {
			const hook = await api.updateHook(hookId, data);
			_update((state) => ({
				...state,
				hooks: state.hooks.map((h) => (h.id === hookId ? hook : h)),
				saving: { ...state.saving, [hookId]: false }
			}));
			return hook;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to update hook';
			_update((state) => ({
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
		_update((state) => ({
			...state,
			saving: { ...state.saving, [hookId]: true },
			error: null
		}));

		try {
			await api.deleteHook(hookId);
			_update((state) => ({
				...state,
				hooks: state.hooks.filter((h) => h.id !== hookId),
				saving: { ...state.saving, [hookId]: false }
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to delete hook';
			_update((state) => ({
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
		const hook = _state.hooks.find((h) => h.id === hookId);
		if (hook) {
			await hooks.updateHook(hookId, { enabled: !hook.enabled });
		}
	},

	/**
	 * Test a hook with sample event data
	 */
	async testHook(hookId: string): Promise<HookTestResult> {
		_update((state) => ({
			...state,
			testing: { ...state.testing, [hookId]: true },
			error: null
		}));

		try {
			const result = await api.testHook(hookId);
			_update((state) => ({
				...state,
				testing: { ...state.testing, [hookId]: false }
			}));
			return result;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to test hook';
			_update((state) => ({
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
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const pluginHooks = await api.getPluginHooks();
			_update((state) => ({ ...state, pluginHooks, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load plugin hooks';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load all hooks (project + app + plugin)
	 */
	async loadAll(projectId: string): Promise<void> {
		_update((state) => ({
			...state,
			loading: true,
			error: null,
			currentProjectId: projectId
		}));

		try {
			// Load project hooks (includes app hooks), global plugin hooks, and project plugin hooks
			const [hooksData, globalPluginHooks, projectPluginHooks] = await Promise.all([
				api.getProjectHooks(projectId),
				api.getPluginHooks(),
				api.getProjectPluginHooks(projectId)
			]);

			// Combine global and project plugin hooks
			const pluginHooks = [...globalPluginHooks, ...projectPluginHooks];

			_update((state) => ({
				...state,
				hooks: hooksData,
				pluginHooks,
				loading: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load hooks';
			_update((state) => ({ ...state, error, loading: false }));
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
		_update((state) => ({ ...state, error: null }));
	},

	/**
	 * Reset store to initial state
	 */
	reset(): void {
		_state = { ...initialState };
		_notifySubscribers();
	},

	/**
	 * Set current project (for switching without reloading)
	 */
	setCurrentProject(projectId: string | null): void {
		_update((state) => ({
			...state,
			currentProjectId: projectId,
			hooks: projectId === state.currentProjectId ? state.hooks : []
		}));
	}
};

// ============================================================================
// Derived Stores (via store-utils createDerived)
// ============================================================================

// All hooks (both app + project)
export const projectHooks = createDerived(() => _state.hooks);

// Plugin hooks
export const pluginHooks = createDerived(() => _state.pluginHooks);

// Loading state
export const hooksLoading = createDerived(() => _state.loading);

// Error state
export const hooksError = createDerived(() => _state.error);

// Testing state for hooks
export const hookTesting = createDerived(() => _state.testing);

// Saving state for hooks
export const hookSaving = createDerived(() => _state.saving);

// Stats
export const hookStats = createDerived(() => {
	let totalEnabled = 0;
	let totalApp = 0;
	let totalProject = 0;
	let bash = 0;
	let webhook = 0;
	let message = 0;

	for (const h of _state.hooks) {
		if (h.enabled) totalEnabled++;
		if (!h.project_id) totalApp++;
		else totalProject++;
		if (h.hook_type === 'bash') bash++;
		else if (h.hook_type === 'webhook') webhook++;
		else if (h.hook_type === 'message') message++;
	}

	return {
		totalCustom: _state.hooks.length,
		totalEnabled,
		totalApp,
		totalProject,
		totalPlugin: _state.pluginHooks.length,
		byType: { bash, webhook, message }
	};
});
