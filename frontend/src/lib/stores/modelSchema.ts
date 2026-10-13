/**
 * Model Schema Store — Dynamic, per-model parameter schemas
 *
 * Fetches a fal.ai model's normalized OpenAPI input schema from the backend
 * (`GET /catalog/schema?endpoint_id=…`) so the settings UI can render
 * model-specific parameter controls instead of hardcoded ones.
 *
 * Schemas are cached per endpoint_id for the lifetime of the page — they
 * change rarely and the backend caches them too.
 *
 * Plain .ts (not .svelte.ts) — see catalog.ts for the rationale.
 */

import { api } from '$lib/api/client';

export interface ParamField {
	name: string;
	type: 'string' | 'integer' | 'number' | 'boolean' | 'enum' | 'array' | 'file';
	title: string;
	description: string;
	required: boolean;
	default: unknown;
	enum: (string | number)[] | null;
	minimum: number | null;
	maximum: number | null;
	format: string | null;
	is_file: boolean;
	is_primary_text: boolean;
}

export interface ModelSchema {
	endpoint_id: string;
	primary_text_field: string | null;
	fields: ParamField[];
}

interface SchemaResponse {
	found: boolean;
	endpoint_id: string;
	schema?: ModelSchema;
}

// endpoint_id -> resolved schema (null = fetched but model has no schema)
const _cache = new Map<string, ModelSchema | null>();
const _inflight = new Map<string, Promise<ModelSchema | null>>();

export const modelSchemaStore = {
	/**
	 * Fetch (and cache) the normalized schema for a model. Resolves to null
	 * when the model has no discoverable schema, so callers fall back to a
	 * plain prompt-only form. Never rejects.
	 */
	async fetch(endpointId: string): Promise<ModelSchema | null> {
		const id = (endpointId || '').trim();
		if (!id) return null;
		if (_cache.has(id)) return _cache.get(id) ?? null;

		const existing = _inflight.get(id);
		if (existing) return existing;

		const p = (async () => {
			try {
				const res = await api.get<SchemaResponse>(
					`/catalog/schema?endpoint_id=${encodeURIComponent(id)}`
				);
				const schema = res.found && res.schema ? res.schema : null;
				_cache.set(id, schema);
				return schema;
			} catch {
				// Treat fetch failure as "no schema" but don't poison the cache,
				// so a later retry can succeed.
				return null;
			} finally {
				_inflight.delete(id);
			}
		})();

		_inflight.set(id, p);
		return p;
	},

	/** The fields a user should configure (everything except the prompt text). */
	configurableFields(schema: ModelSchema | null): ParamField[] {
		if (!schema) return [];
		return schema.fields.filter((f) => !f.is_primary_text);
	},
};
