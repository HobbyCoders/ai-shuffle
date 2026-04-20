/**
 * Claude model registry — single source of truth for the frontend.
 *
 * Wrapped in Svelte 5 `$state` so the registry can be hydrated at runtime
 * from `GET /api/v1/models` (the canonical backend registry). Consumers
 * read through the exported helpers (`getModels`, `getDefaultModel`,
 * `getModelLabel`, ...) and automatically see the hydrated values once
 * the boot fetch resolves.
 *
 * If the boot fetch never completes (offline, auth error, network flake),
 * the module falls back to the compiled-in `STATIC_*` constants — which
 * mirror `app/core/model_registry.py` at build time. That keeps the app
 * fully functional without a backend connection.
 *
 * Callers should never hardcode a model ID inline — import `getModels`,
 * the helper functions below, or one of the sentinel constants instead.
 */

import { api } from '$lib/api/client';

export type ModelFamily = 'haiku' | 'sonnet' | 'opus';

export interface ModelOption {
	/** Canonical model ID persisted to storage and sent to the backend/SDK. */
	value: string;
	/** Human-facing label for dropdowns and badges. */
	label: string;
	/** Badge family — maps to `.card-badge--{family}` CSS classes. */
	family: ModelFamily;
}

// ---------------------------------------------------------------------------
// Static fallback — mirrors `app/core/model_registry.py` at build time.
// Keep in sync; drift is logged via `warnOnDrift` when hydration completes.
// ---------------------------------------------------------------------------

const STATIC_MODELS: readonly ModelOption[] = [
	{ value: 'claude-haiku-4-5', label: 'Haiku 4.5', family: 'haiku' },
	{ value: 'claude-sonnet-4-6', label: 'Sonnet 4.6', family: 'sonnet' },
	{ value: 'claude-sonnet-4-6[1m]', label: 'Sonnet 4.6 1M', family: 'sonnet' },
	{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus' },
	{ value: 'claude-opus-4-6[1m]', label: 'Opus 4.6 1M', family: 'opus' },
	{ value: 'claude-opus-4-7', label: 'Opus 4.7', family: 'opus' },
	{ value: 'claude-opus-4-7[1m]', label: 'Opus 4.7 1M', family: 'opus' }
] as const;

const STATIC_DEFAULT = 'claude-sonnet-4-6';

const STATIC_LEGACY_MAP: Readonly<Record<string, string>> = {
	haiku: 'claude-haiku-4-5',
	sonnet: 'claude-sonnet-4-6',
	'sonnet[1m]': 'claude-sonnet-4-6[1m]',
	opus: 'claude-opus-4-7',
	'opus[1m]': 'claude-opus-4-7[1m]'
};

/** Sentinel value meaning "inherit from parent" (used by subagents). */
export const INHERIT_VALUE = '';

/** Sentinel value meaning "custom free-text entry" (used by the `Custom…` dropdown option). */
export const CUSTOM_VALUE = '__custom__';

// ---------------------------------------------------------------------------
// Reactive state — hydrated from `/api/v1/models` on boot.
// ---------------------------------------------------------------------------

interface RegistryState {
	models: ModelOption[];
	default: string;
	legacyMap: Record<string, string>;
	hydrated: boolean;
}

const _registry = $state<RegistryState>({
	models: [...STATIC_MODELS],
	default: STATIC_DEFAULT,
	legacyMap: { ...STATIC_LEGACY_MAP },
	hydrated: false
});

// ---------------------------------------------------------------------------
// Public accessors — prefer these over touching `_registry` directly.
// ---------------------------------------------------------------------------

/** Ordered list of selectable models. Reactive in Svelte component contexts. */
export function getModels(): readonly ModelOption[] {
	return _registry.models;
}

/** Default model used when nothing is configured yet. Reactive. */
export function getDefaultModel(): string {
	return _registry.default;
}

/** True once the registry has been hydrated from the backend. */
export function isRegistryHydrated(): boolean {
	return _registry.hydrated;
}

// ---------------------------------------------------------------------------
// Helpers — unchanged signatures, rewired to read live registry.
// ---------------------------------------------------------------------------

/**
 * Resolve a stored model string to its canonical form.
 *
 * - `null | undefined | ''` → `''` (inherit / unset)
 * - Legacy short slug → canonical full ID
 * - Any other string → returned as-is (custom models pass through)
 */
export function normalizeModel(value: string | null | undefined): string {
	if (!value) return INHERIT_VALUE;
	return _registry.legacyMap[value] ?? value;
}

/** True when `value` (after normalization) is one of the selectable presets. */
export function isPresetModel(value: string | null | undefined): boolean {
	const normalized = normalizeModel(value);
	return _registry.models.some((m) => m.value === normalized);
}

/**
 * Human-readable label for a stored model value.
 * Falls back to the raw value, or `'Default'` when nothing is set.
 */
export function getModelLabel(value: string | null | undefined): string {
	const normalized = normalizeModel(value);
	const match = _registry.models.find((m) => m.value === normalized);
	if (match) return match.label;
	return value && value.length > 0 ? value : 'Default';
}

/** Resolved family for a stored model value, or `null` for non-preset values. */
export function getModelFamily(value: string | null | undefined): ModelFamily | null {
	const normalized = normalizeModel(value);
	return _registry.models.find((m) => m.value === normalized)?.family ?? null;
}

/**
 * CSS badge class for a stored model value. Pairs with the
 * `card-badge--{haiku,sonnet,opus,inherit,muted}` classes in
 * `card-design-system.css`.
 */
export function getModelBadgeClass(value: string | null | undefined): string {
	if (!value) return 'card-badge--inherit';
	const family = getModelFamily(value);
	return family ? `card-badge--${family}` : 'card-badge--muted';
}

// ---------------------------------------------------------------------------
// Hydration
// ---------------------------------------------------------------------------

interface RegistryPayload {
	models: ModelOption[];
	default: string;
	inherit_value?: string;
	legacy_map: Record<string, string>;
}

function warnOnDrift(hydrated: ModelOption[], baseline: readonly ModelOption[]): void {
	const H = new Set(hydrated.map((m) => m.value));
	const B = new Set(baseline.map((m) => m.value));
	const added = [...H].filter((v) => !B.has(v));
	const removed = [...B].filter((v) => !H.has(v));
	if (added.length || removed.length) {
		console.warn('[model-registry] static bundle drift vs. backend', { added, removed });
	}
}

/**
 * Fetch the canonical registry from the backend and swap it into the
 * reactive state. On any failure, the static fallback remains in place.
 *
 * Safe to call multiple times — each call issues one fetch and replaces
 * state atomically. Intended to run once from the root layout after auth.
 */
export async function hydrateModelRegistry(): Promise<void> {
	try {
		const payload = await api.get<RegistryPayload>('/models');
		if (!payload || !Array.isArray(payload.models) || payload.models.length === 0) {
			console.warn('[model-registry] hydrate returned empty payload, keeping static fallback');
			return;
		}
		warnOnDrift(payload.models, STATIC_MODELS);
		_registry.models = payload.models;
		_registry.default = payload.default || STATIC_DEFAULT;
		_registry.legacyMap = payload.legacy_map ?? { ...STATIC_LEGACY_MAP };
		_registry.hydrated = true;
	} catch (e) {
		console.error('[model-registry] hydrate failed, keeping static fallback', e);
	}
}
