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

export type ModelFamily = 'haiku' | 'sonnet' | 'opus' | 'fable';

export interface ModelOption {
	/** Model identifier (an Agent SDK shorthand alias) persisted to storage and sent to the backend/SDK. */
	value: string;
	/** Human-facing label for dropdowns and badges. */
	label: string;
	/** Badge family — maps to `.card-badge--{family}` CSS classes. */
	family: ModelFamily;
	/** Whether this model can run with the 1M context window (`[1m]` suffix). */
	supports1m?: boolean;
}

/**
 * The 1M context window is not a separate model — it is a capability toggled
 * on a base model by appending this suffix to the stored model ID. The UI
 * exposes it as a checkbox rather than as duplicate dropdown entries.
 */
export const CONTEXT_1M_SUFFIX = '[1m]';

// ---------------------------------------------------------------------------
// Static fallback — mirrors `app/core/model_registry.py` at build time.
// Keep in sync; drift is logged via `warnOnDrift` when hydration completes.
// Only base models live here; the 1M variants are derived via CONTEXT_1M_SUFFIX.
// ---------------------------------------------------------------------------

const STATIC_MODELS: readonly ModelOption[] = [
	{ value: 'haiku', label: 'Haiku', family: 'haiku', supports1m: false },
	{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports1m: true },
	{ value: 'opus', label: 'Opus', family: 'opus', supports1m: true },
	{ value: 'fable', label: 'Fable', family: 'fable', supports1m: true }
] as const;

const STATIC_DEFAULT = 'sonnet';

const STATIC_LEGACY_MAP: Readonly<Record<string, string>> = {
	'claude-haiku-4-5': 'haiku',
	'claude-haiku-4-5-20251001': 'haiku',
	'claude-sonnet-4-6': 'sonnet',
	'claude-sonnet-4-6[1m]': 'sonnet[1m]',
	'claude-sonnet-5': 'sonnet',
	'claude-sonnet-5[1m]': 'sonnet[1m]',
	'claude-fable-5': 'fable',
	'claude-fable-5[1m]': 'fable[1m]',
	'claude-opus-4-6': 'opus',
	'claude-opus-4-6[1m]': 'opus[1m]',
	'claude-opus-4-7': 'opus',
	'claude-opus-4-7[1m]': 'opus[1m]',
	'claude-opus-4-8': 'opus',
	'claude-opus-4-8[1m]': 'opus[1m]'
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
 * - Legacy full model ID → shorthand alias
 * - Any other string → returned as-is (custom models pass through)
 */
export function normalizeModel(value: string | null | undefined): string {
	if (!value) return INHERIT_VALUE;
	return _registry.legacyMap[value] ?? value;
}

/** True when the (normalized) model opts into the 1M context window. */
export function has1MContext(value: string | null | undefined): boolean {
	const normalized = normalizeModel(value);
	return !!normalized && normalized.endsWith(CONTEXT_1M_SUFFIX);
}

/** The base model ID with any `[1m]` suffix removed (normalized first). */
export function getBaseModel(value: string | null | undefined): string {
	const normalized = normalizeModel(value);
	return normalized.endsWith(CONTEXT_1M_SUFFIX)
		? normalized.slice(0, -CONTEXT_1M_SUFFIX.length)
		: normalized;
}

/**
 * Apply or remove the `[1m]` suffix on a model ID.
 * Inherit / empty values are returned untouched (nothing to toggle).
 */
export function withContextSuffix(value: string | null | undefined, enabled: boolean): string {
	const base = getBaseModel(value);
	if (!base) return base;
	return enabled ? base + CONTEXT_1M_SUFFIX : base;
}

/** True when the base model supports the 1M context window. */
export function modelSupports1M(value: string | null | undefined): boolean {
	const base = getBaseModel(value);
	return _registry.models.find((m) => m.value === base)?.supports1m ?? false;
}

/** True when `value` maps to one of the selectable presets (1M variants included). */
export function isPresetModel(value: string | null | undefined): boolean {
	const base = getBaseModel(value);
	return _registry.models.some((m) => m.value === base);
}

/**
 * Human-readable label for a stored model value.
 * Falls back to the raw value, or `'Default'` when nothing is set.
 * 1M variants get a `" 1M"` suffix on the base label.
 */
export function getModelLabel(value: string | null | undefined): string {
	const base = getBaseModel(value);
	const match = _registry.models.find((m) => m.value === base);
	if (match) return has1MContext(value) ? `${match.label} 1M` : match.label;
	return value && value.length > 0 ? value : 'Default';
}

/** Resolved family for a stored model value, or `null` for non-preset values. */
export function getModelFamily(value: string | null | undefined): ModelFamily | null {
	const base = getBaseModel(value);
	return _registry.models.find((m) => m.value === base)?.family ?? null;
}

/**
 * Robust family inference: try the registry lookup first, then fall back to
 * substring inference on the base model id. The SDK can echo a dated
 * snapshot id (e.g. a future `claude-sonnet-X-YYYYMMDD`) that isn't in
 * `LEGACY_MODEL_MAP` yet — `getModelFamily` would return `null` for that,
 * making a genuine same-family id look like an unclassifiable reroute. This
 * still classifies those ids by checking which family name appears in the
 * lowercased base model string. Returns `null` for truly custom/unknown
 * models (no family substring present).
 */
export function inferModelFamily(value: string | null | undefined): ModelFamily | null {
	const registryFamily = getModelFamily(value);
	if (registryFamily) return registryFamily;
	const base = getBaseModel(value).toLowerCase();
	const families: ModelFamily[] = ['haiku', 'sonnet', 'opus', 'fable'];
	return families.find((family) => base.includes(family)) ?? null;
}

/**
 * CSS badge class for a stored model value. Pairs with the
 * `card-badge--{haiku,sonnet,opus,fable,inherit,muted}` classes in
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

interface RegistryModelPayload {
	value: string;
	label: string;
	family: ModelFamily;
	context_window?: number;
	supports_1m?: boolean;
}

interface RegistryPayload {
	models: RegistryModelPayload[];
	default: string;
	inherit_value?: string;
	context_1m_suffix?: string;
	legacy_map: Record<string, string>;
}

function mapPayloadModel(m: RegistryModelPayload): ModelOption {
	return { value: m.value, label: m.label, family: m.family, supports1m: m.supports_1m ?? false };
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
		const models = payload.models.map(mapPayloadModel);
		warnOnDrift(models, STATIC_MODELS);
		_registry.models = models;
		_registry.default = payload.default || STATIC_DEFAULT;
		_registry.legacyMap = payload.legacy_map ?? { ...STATIC_LEGACY_MAP };
		_registry.hydrated = true;
	} catch (e) {
		console.error('[model-registry] hydrate failed, keeping static fallback', e);
	}
}
