/**
 * Hooks Management Store
 *
 * Manages state for project hooks:
 * - Custom hooks per project
 * - Plugin-contributed hooks (read-only)
 * - Hook enable/disable state
 * - Hook testing
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type { Hook, HookCreate, HookUpdate, HookTestResult, PluginHookInfo } from '$lib/api/client';
import * as api from '$lib/api/client';
import { untrack } from 'svelte';

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
// Reactive State
// ============================================================================

let _state = $state<HooksState>({ ...initialState });

// ============================================================================
// Subscribe Shim
// ============================================================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<HooksState>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: HooksState) => HooksState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const hooks = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<HooksState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

	// ========================================================================
	// Project Hooks Operations
	// ========================================================================

	/**
	 * Load hooks for a project
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
	 * Create a new hook
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
	 * Load all hooks (project + plugin)
	 */
	async loadAll(projectId: string): Promise<void> {
		_update((state) => ({
			...state,
			loading: true,
			error: null,
			currentProjectId: projectId
		}));

		try {
			// Load project hooks, global plugin hooks, and project plugin hooks
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
// Derived Stores
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<HooksState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

// All custom hooks for current project
export const projectHooks = createDerived(() => _state.hooks);

// Enabled hooks only
export const enabledHooks = createDerived(() => _state.hooks.filter((h) => h.enabled));

// Disabled hooks only
export const disabledHooks = createDerived(() => _state.hooks.filter((h) => !h.enabled));

// Hooks grouped by event type
export const hooksByEvent = createDerived(() => {
	const groups: Record<string, Hook[]> = {};
	for (const hook of _state.hooks) {
		if (!groups[hook.event]) {
			groups[hook.event] = [];
		}
		groups[hook.event].push(hook);
	}
	return groups;
});

// Plugin hooks
export const pluginHooks = createDerived(() => _state.pluginHooks);

// Plugin hooks grouped by plugin
export const pluginHooksByPlugin = createDerived(() => {
	const groups: Record<string, PluginHookInfo[]> = {};
	for (const hook of _state.pluginHooks) {
		if (!groups[hook.plugin_id]) {
			groups[hook.plugin_id] = [];
		}
		groups[hook.plugin_id].push(hook);
	}
	return groups;
});

// Current project ID
export const currentProjectId = createDerived(() => _state.currentProjectId);

// Loading state
export const hooksLoading = createDerived(() => _state.loading);

// Error state
export const hooksError = createDerived(() => _state.error);

// Testing state for hooks
export const hookTesting = createDerived(() => _state.testing);

// Saving state for hooks
export const hookSaving = createDerived(() => _state.saving);

// Stats
export const hookStats = createDerived(() => ({
	totalCustom: _state.hooks.length,
	totalEnabled: _state.hooks.filter((h) => h.enabled).length,
	totalPlugin: _state.pluginHooks.length,
	byType: {
		bash: _state.hooks.filter((h) => h.hook_type === 'bash').length,
		webhook: _state.hooks.filter((h) => h.hook_type === 'webhook').length
	}
}));
