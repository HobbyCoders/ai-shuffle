/**
 * Catalog Store — Dynamic fal.ai model catalog for tool settings
 *
 * Fetches models from the backend catalog API (which proxies fal.ai),
 * organized by tool_type. Provides per-tool model lists that update
 * automatically when fal.ai adds new models.
 *
 * NOTE: This is a plain .ts file (not .svelte.ts) to avoid dual module
 * evaluation issues with Svelte 5 runes. See KNOWN_ISSUES.md Root Cause 10.
 */

import { api } from '$lib/api/client';
import type { CatalogModel, CatalogCategory } from '$lib/types/ai-models';

// ─── State ───

let _models: CatalogModel[] = [];
let _categories: CatalogCategory[] = [];
let _loaded = false;
let _loading = false;
let _error = '';
let _catalogSize = 0;
let _subscribers = new Set<() => void>();

// ─── Public API ───

export const catalogStore = {
	/** Whether the catalog has been loaded at least once */
	get loaded() { return _loaded; },

	/** Whether a load/refresh is in progress */
	get loading() { return _loading; },

	/** Last error message, or empty string */
	get error() { return _error; },

	/** Total number of models in the catalog */
	get catalogSize() { return _catalogSize; },

	/** All categories with model counts */
	get categories() { return _categories; },

	/**
	 * Get all catalog models for a specific tool_type.
	 * Returns models that fal.ai categorizes under this tool type.
	 */
	getModelsForTool(toolType: string): CatalogModel[] {
		return _models.filter(m => m.tool_type === toolType);
	},

	/**
	 * Get all catalog models.
	 */
	getAll(): CatalogModel[] {
		return _models;
	},

	/**
	 * Search models by query string across all fields.
	 */
	search(query: string, toolType?: string): CatalogModel[] {
		let results = toolType
			? _models.filter(m => m.tool_type === toolType)
			: _models;

		if (query.trim()) {
			const q = query.toLowerCase();
			results = results.filter(m =>
				m.display_name.toLowerCase().includes(q) ||
				m.description.toLowerCase().includes(q) ||
				m.endpoint_id.toLowerCase().includes(q) ||
				m.tags.some(t => t.toLowerCase().includes(q))
			);
		}

		return results;
	},

	/**
	 * Check if a specific endpoint_id exists in the catalog.
	 */
	hasModel(endpointId: string): boolean {
		return _models.some(m => m.endpoint_id === endpointId);
	},

	/**
	 * Get a specific model by endpoint_id.
	 */
	getModel(endpointId: string): CatalogModel | undefined {
		return _models.find(m => m.endpoint_id === endpointId);
	},

	/**
	 * Load the catalog from the backend. Idempotent — won't re-fetch if
	 * already loaded unless force=true.
	 */
	async load(force = false): Promise<void> {
		if (_loaded && !force) return;
		if (_loading) return;

		_loading = true;
		_error = '';
		_notify();

		try {
			const [modelsRes, catsRes] = await Promise.all([
				api.get<{ models: CatalogModel[]; total: number; catalog_size: number }>('/catalog/models'),
				api.get<{ categories: CatalogCategory[]; total_models: number }>('/catalog/categories'),
			]);
			_models = modelsRes.models;
			_categories = catsRes.categories;
			_catalogSize = modelsRes.catalog_size;
			_loaded = true;
		} catch (e: unknown) {
			_error = e instanceof Error ? e.message : 'Failed to load catalog';
			console.error('Catalog load failed:', e);
		} finally {
			_loading = false;
			_notify();
		}
	},

	/**
	 * Force refresh the catalog from fal.ai's API.
	 */
	async refresh(): Promise<void> {
		_loading = true;
		_error = '';
		_notify();

		try {
			const result = await api.post<{
				success: boolean;
				models_fetched: number;
				categories: CatalogCategory[];
			}>('/catalog/refresh', {});

			_categories = result.categories;
			_catalogSize = result.models_fetched;

			// Reload all models
			const modelsRes = await api.get<{ models: CatalogModel[]; total: number }>('/catalog/models');
			_models = modelsRes.models;
			_loaded = true;
		} catch (e: unknown) {
			_error = e instanceof Error ? e.message : 'Failed to refresh catalog';
		} finally {
			_loading = false;
			_notify();
		}
	},

	/**
	 * Subscribe to state changes (for Svelte reactivity).
	 */
	subscribe(fn: () => void): () => void {
		_subscribers.add(fn);
		return () => _subscribers.delete(fn);
	},
};

function _notify() {
	for (const fn of _subscribers) {
		try { fn(); } catch { /* ignore */ }
	}
}
