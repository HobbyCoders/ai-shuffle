/**
 * Claude model registry — single source of truth for the frontend.
 *
 * Add/remove a model here and every dropdown, badge, label, and default
 * across the UI picks it up automatically.
 *
 * Callers should never hardcode a model ID inline — import `MODELS`, the
 * helper functions below, or one of the sentinel constants instead.
 *
 * NOTE: The backend also hardcodes these identifiers in several places
 * (see `app/mcp/config_profiles.py`, `app/core/generation/*`, etc.). A
 * follow-up ticket should unify that behind a `GET /api/models` endpoint
 * so backend and frontend share a single registry.
 */

export type ModelFamily = 'haiku' | 'sonnet' | 'opus';

export interface ModelOption {
	/** Canonical model ID persisted to storage and sent to the backend/SDK. */
	value: string;
	/** Human-facing label for dropdowns and badges. */
	label: string;
	/** Badge family — maps to `.card-badge--{family}` CSS classes. */
	family: ModelFamily;
}

/**
 * Ordered list of selectable models. Order here is the order shown in
 * every dropdown in the app.
 */
export const MODELS: readonly ModelOption[] = [
	{ value: 'claude-haiku-4-5', label: 'Haiku 4.5', family: 'haiku' },
	{ value: 'claude-sonnet-4-6', label: 'Sonnet 4.6', family: 'sonnet' },
	{ value: 'claude-sonnet-4-6[1m]', label: 'Sonnet 4.6 1M', family: 'sonnet' },
	{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus' },
	{ value: 'claude-opus-4-6[1m]', label: 'Opus 4.6 1M', family: 'opus' },
	{ value: 'claude-opus-4-7', label: 'Opus 4.7', family: 'opus' },
	{ value: 'claude-opus-4-7[1m]', label: 'Opus 4.7 1M', family: 'opus' }
] as const;

/** Default model used when nothing is configured yet. */
export const DEFAULT_MODEL = 'claude-sonnet-4-6';

/** Sentinel value meaning "inherit from parent" (used by subagents). */
export const INHERIT_VALUE = '';

/** Sentinel value meaning "custom free-text entry" (used by the `Custom…` dropdown option). */
export const CUSTOM_VALUE = '__custom__';

/**
 * Legacy short slugs that may still be persisted in user data from before
 * the canonical-ID migration. Map to the closest canonical model so older
 * profiles/subagents render correct labels and round-trip cleanly.
 *
 * When a legacy value is encountered we treat it as an alias for the
 * canonical value — we do NOT automatically rewrite storage. Rewriting
 * happens on the next save.
 */
export const LEGACY_MODEL_MAP: Readonly<Record<string, string>> = {
	haiku: 'claude-haiku-4-5',
	sonnet: 'claude-sonnet-4-6',
	'sonnet[1m]': 'claude-sonnet-4-6[1m]',
	opus: 'claude-opus-4-7',
	'opus[1m]': 'claude-opus-4-7[1m]'
};

/**
 * Resolve a stored model string to its canonical form.
 *
 * - `null | undefined | ''` → `''` (inherit / unset)
 * - Legacy short slug → canonical full ID
 * - Any other string → returned as-is (custom models pass through)
 */
export function normalizeModel(value: string | null | undefined): string {
	if (!value) return INHERIT_VALUE;
	return LEGACY_MODEL_MAP[value] ?? value;
}

/** True when `value` (after normalization) is one of the selectable presets. */
export function isPresetModel(value: string | null | undefined): boolean {
	const normalized = normalizeModel(value);
	return MODELS.some((m) => m.value === normalized);
}

/**
 * Human-readable label for a stored model value.
 * Falls back to the raw value, or `'Default'` when nothing is set.
 */
export function getModelLabel(value: string | null | undefined): string {
	const normalized = normalizeModel(value);
	const match = MODELS.find((m) => m.value === normalized);
	if (match) return match.label;
	return value && value.length > 0 ? value : 'Default';
}

/** Resolved family for a stored model value, or `null` for non-preset values. */
export function getModelFamily(value: string | null | undefined): ModelFamily | null {
	const normalized = normalizeModel(value);
	return MODELS.find((m) => m.value === normalized)?.family ?? null;
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
