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
	/**
	 * Maximum input context in tokens, as the model natively runs. The current
	 * Claude slugs (sonnet/opus/fable) are 1M; haiku is 200k. There is no
	 * opt-in toggle — the old `[1m]` suffix mechanism is retired.
	 */
	context_window?: number;
}

/**
 * RETIRED MECHANISM — legacy-data normalizer only. The 1M window used to be an
 * opt-in capability toggled by appending this suffix to a stored model ID.
 * Current Claude slugs run 1M natively, so nothing writes it any more; stored
 * profiles can still hold e.g. `"opus[1m]"`, which `normalizeModel` strips.
 */
const LEGACY_1M_SUFFIX = '[1m]';

// ---------------------------------------------------------------------------
// Static fallback — mirrors `app/core/model_registry.py` at build time.
// Keep in sync; drift is logged via `warnOnDrift` when hydration completes.
// ---------------------------------------------------------------------------

const STATIC_MODELS: readonly ModelOption[] = [
	{ value: 'haiku', label: 'Haiku', family: 'haiku', context_window: 200_000 },
	{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 },
	{ value: 'opus', label: 'Opus', family: 'opus', context_window: 1_000_000 },
	{ value: 'fable', label: 'Fable', family: 'fable', context_window: 1_000_000 }
] as const;

const STATIC_DEFAULT = 'sonnet';

const STATIC_LEGACY_MAP: Readonly<Record<string, string>> = {
	'claude-haiku-4-5': 'haiku',
	'claude-haiku-4-5-20251001': 'haiku',
	'claude-sonnet-4-6': 'sonnet',
	'claude-sonnet-4-6[1m]': 'sonnet',
	'claude-sonnet-5': 'sonnet',
	'claude-sonnet-5[1m]': 'sonnet',
	'claude-fable-5': 'fable',
	'claude-fable-5[1m]': 'fable',
	'claude-opus-4-6': 'opus',
	'claude-opus-4-6[1m]': 'opus',
	'claude-opus-4-7': 'opus',
	'claude-opus-4-7[1m]': 'opus',
	'claude-opus-4-8': 'opus',
	'claude-opus-4-8[1m]': 'opus'
};

/** Sentinel value meaning "inherit from parent" (used by subagents). */
export const INHERIT_VALUE = '';

/** Sentinel value meaning "custom free-text entry" (used by the `Custom…` dropdown option). */
export const CUSTOM_VALUE = '__custom__';

/** Reserved key used by agent `provider_models` maps for native Claude. */
export const NATIVE_PROVIDER_KEY = '__native__';

/**
 * The voice-mode leg of an agent's subagent map (`config.provider_models`).
 * Not a provider row: a voice call is funded by an OpenAI Realtime provider,
 * but the workers it dispatches run wherever this entry says — a bare native
 * model id, or a `prov:<providerId>:<modelId>` composite. Mirrors the backend's
 * `VOICE_PROVIDER_KEY` (app/core/agent_provider_models.py).
 */
export const VOICE_PROVIDER_KEY = '__voice__';

// ---------------------------------------------------------------------------
// Provider-model composite codec (`prov:<providerId>:<modelId>`)
// ---------------------------------------------------------------------------

/** Prefix marking a value as a provider-scoped model composite. */
export const PROVIDER_MODEL_PREFIX = 'prov:';

/** The two halves of a decoded `prov:<providerId>:<modelId>` composite. */
export interface DecodedProviderModel {
	providerId: string;
	modelId: string;
}

/**
 * Encode a provider-scoped model selection.
 *
 * The composite is the ONE wire/storage shape for "this specific model on this
 * specific provider" — used by the profile editor's ModelPicker and by the
 * chat composer's model override (PA13). The backend decodes it in
 * `app/core/query_options.py`, where the decoded provider id REBINDS the turn's
 * provider.
 */
export function encodeProviderModel(providerId: string, modelId: string): string {
	return `${PROVIDER_MODEL_PREFIX}${providerId}:${modelId}`;
}

/**
 * Decode a `prov:<providerId>:<modelId>` composite, or `null` when `value` is
 * not a composite (a native Claude alias, a custom id, empty, …).
 *
 * Splits on the FIRST colon after the prefix only: provider model ids routinely
 * contain both `/` and `:` themselves (e.g. OpenRouter's
 * `meta-llama/llama-3.1-8b-instruct:free`), so a naive `split(':')` corrupts
 * them. A composite with an empty provider id or an empty model id is rejected
 * (returns `null`) rather than silently producing a half-value.
 */
export function decodeProviderModel(
	value: string | null | undefined
): DecodedProviderModel | null {
	if (!value || !value.startsWith(PROVIDER_MODEL_PREFIX)) return null;
	const rest = value.slice(PROVIDER_MODEL_PREFIX.length);
	const sep = rest.indexOf(':');
	if (sep <= 0) return null;
	const providerId = rest.slice(0, sep);
	const modelId = rest.slice(sep + 1);
	if (!providerId || !modelId) return null;
	return { providerId, modelId };
}

/** True when `value` is a well-formed provider-model composite. */
export function isProviderModel(value: string | null | undefined): boolean {
	return decodeProviderModel(value) !== null;
}

/** A curated model offered by an admin-configured non-Anthropic gateway. */
export interface ProviderModel {
	id: string;
	label: string;
	/**
	 * Context window (tokens) resolved FOR THIS MODEL by the registry payload
	 * (`app/core/model_registry.resolve_provider_context_window`): the model's
	 * own curated/upstream value, then the built-in preset roster, then the
	 * provider row's column. Absent/null when nothing declares one — consumers
	 * fall back to the default window.
	 */
	context_window?: number | null;
}

/**
 * How a provider row authenticates: a ChatGPT/Codex OAuth subscription, or an
 * API key. Mirrors the backend's `auth_kind` (see `app/core/model_registry.py`).
 */
export type ProviderAuthKind = 'subscription' | 'api';

/**
 * An enabled `model_providers` row, as exposed by the registry (never
 * carries the encrypted auth token — see `app/db/model_providers.py`).
 */
export interface ProviderEntry {
	id: string;
	name: string;
	provider_type: string;
	models: ProviderModel[];
	default_model: string | null;
	/** Subscription vs API-key auth. Defaults to `'api'` on an older payload. */
	auth_kind: ProviderAuthKind;
	/**
	 * Subscription plan string for a subscription row (e.g. `'prolite'`), taken
	 * from the provider's latest usage snapshot. `null` for API-key rows and for
	 * a subscription row whose snapshot hasn't landed yet.
	 */
	plan_type: string | null;
}

/**
 * How NATIVE Claude authenticates on THIS install, as reported by the registry
 * (see `_claude_payload` in `app/core/model_registry.py`). There is no
 * `model_providers` row for Anthropic, so this block is the only carrier of the
 * Claude brand's auth kind + subscription plan.
 *
 * Both fields are `null` until hydration lands (and stay null if the backend
 * couldn't resolve them), which the display rules read as "nothing to show".
 */
export interface ClaudeAuthInfo {
	/** `'api'` on a stored Anthropic API key, `'subscription'` on OAuth account. */
	auth_kind: ProviderAuthKind | null;
	/** Subscription tier for account auth (e.g. `'max'`); null otherwise/unknown. */
	plan_type: string | null;
}

// ---------------------------------------------------------------------------
// Reactive state — hydrated from `/api/v1/models` on boot.
// ---------------------------------------------------------------------------

interface RegistryState {
	models: ModelOption[];
	default: string;
	legacyMap: Record<string, string>;
	providers: ProviderEntry[];
	claude: ClaudeAuthInfo;
	hydrated: boolean;
}

const _registry = $state<RegistryState>({
	models: [...STATIC_MODELS],
	default: STATIC_DEFAULT,
	legacyMap: { ...STATIC_LEGACY_MAP },
	providers: [],
	claude: { auth_kind: null, plan_type: null },
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

/**
 * Enabled non-Anthropic model gateways (admin-configured in "AI Providers").
 * Empty until hydration completes — there is no static fallback, since
 * providers are entirely db-sourced. Reactive in Svelte component contexts.
 */
export function getProviders(): readonly ProviderEntry[] {
	return _registry.providers;
}

/**
 * How native Claude authenticates on THIS install (auth kind + subscription
 * plan). Nulls until hydration lands. Reactive in Svelte component contexts.
 *
 * It describes the LOCAL install only — a per-install usage snapshot's own
 * `planType` is authoritative for any other install.
 */
export function getClaudeAuth(): ClaudeAuthInfo {
	return _registry.claude;
}

// ---------------------------------------------------------------------------
// Provider display naming — the ONE place the brand/subtext rules live.
//
// Every surface that names a provider (the context/usage popover, the chat
// composer's model dropdown, the agents-UI ModelPicker) renders a BRAND NAME
// plus a SUBTEXT saying how that row authenticates:
//
//   provider_type 'chatgpt'  → "OpenAI" · <plan>   (subscription, e.g. "prolite")
//   provider_type 'openai'   → "OpenAI" · "API"    (API key)
//   provider_type 'grok'     → "Grok"   · <plan>   (SuperGrok subscription)
//   provider_type 'xai'      → "Grok"   · "API"    (xAI API key)
//   any other row            → <row name> · "API"
//   native Claude            → "Claude" · <plan> for a subscription account,
//                              "Claude" · "API" for Anthropic API-key auth
//
// The two OpenAI rows deliberately keep their own groups/sections — the
// subtext is what disambiguates them; nothing here merges or dedupes rows.
// ---------------------------------------------------------------------------

/** Brand name for the native Claude group/section. */
export const CLAUDE_BRAND = 'Claude';

/** Subtext used for any API-key-authenticated row. */
export const API_SUBTEXT = 'API';

/** Fallback subtext for a subscription row whose plan string isn't known yet. */
export const SUBSCRIPTION_SUBTEXT = 'Subscription';

/** Brand overrides keyed by `provider_type` — rows whose stored name is not the
 *  brand we want to show (both OpenAI rows display as "OpenAI"). */
const PROVIDER_BRANDS: Readonly<Record<string, string>> = {
	chatgpt: 'OpenAI',
	openai: 'OpenAI',
	grok: 'Grok',
	xai: 'Grok'
};

/** A provider's two-part display label. */
export interface ProviderDisplay {
	/** Brand name (the primary label). */
	name: string;
	/** Auth-kind subtext, or `null` when nothing is known to show. */
	subtext: string | null;
}

/** The subset of a provider row the naming rules actually read. */
export interface ProviderDisplayInput {
	name?: string | null;
	provider_type?: string | null;
	auth_kind?: ProviderAuthKind | null;
	plan_type?: string | null;
}

/**
 * Brand name + auth-kind subtext for a provider row.
 *
 * `auth_kind` is authoritative when present (the backend derives it from the
 * row's `auth_mode`, so a `custom` row carrying Codex auth is classified
 * correctly); callers with only a `provider_type` to hand get the same answer
 * for every non-`custom` row.
 */
export function getProviderDisplay(entry: ProviderDisplayInput): ProviderDisplay {
	const type = entry.provider_type ?? '';
	const name = PROVIDER_BRANDS[type] || entry.name || type || 'Provider';
	const kind: ProviderAuthKind =
		entry.auth_kind ?? (type === 'chatgpt' || type === 'grok' ? 'subscription' : 'api');
	if (kind === 'subscription') {
		return { name, subtext: entry.plan_type || SUBSCRIPTION_SUBTEXT };
	}
	return { name, subtext: API_SUBTEXT };
}

/**
 * Brand name + subtext for the native Claude group/section.
 *
 * `planType` is the subscription tier ("max" / "pro"); pass `authKind: 'api'`
 * when the install runs on an Anthropic API key. With neither known the group
 * degrades to a bare "Claude" rather than inventing a subtext — the app has no
 * store carrying the Claude plan today (see the PR notes).
 */
export function getClaudeDisplay(opts?: {
	planType?: string | null;
	authKind?: ProviderAuthKind | null;
}): ProviderDisplay {
	if (opts?.authKind === 'api') return { name: CLAUDE_BRAND, subtext: API_SUBTEXT };
	const plan = opts?.planType?.trim();
	return { name: CLAUDE_BRAND, subtext: plan ? plan : null };
}

/** One-line `"Brand · subtext"` rendering — for titles/aria labels. */
export function formatProviderDisplay(display: ProviderDisplay): string {
	return display.subtext ? `${display.name} · ${display.subtext}` : display.name;
}

// ---------------------------------------------------------------------------
// Helpers — unchanged signatures, rewired to read live registry.
// ---------------------------------------------------------------------------

/**
 * Resolve a stored model string to its canonical form.
 *
 * - `null | undefined | ''` → `''` (inherit / unset)
 * - Legacy full model ID → shorthand alias
 * - A trailing legacy `[1m]` suffix → stripped (retired mechanism)
 * - Any other string → returned as-is (custom models pass through)
 */
export function normalizeModel(value: string | null | undefined): string {
	if (!value) return INHERIT_VALUE;
	const mapped = _registry.legacyMap[value];
	if (mapped !== undefined) return mapped;
	return value.endsWith(LEGACY_1M_SUFFIX)
		? value.slice(0, -LEGACY_1M_SUFFIX.length)
		: value;
}

/** True when `value` maps to one of the selectable presets. */
export function isPresetModel(value: string | null | undefined): boolean {
	const base = normalizeModel(value);
	return _registry.models.some((m) => m.value === base);
}

/**
 * Registered context window (tokens) for a Claude preset model, or `null` when
 * the value isn't a preset. Claude slugs carry their NATIVE window here
 * (sonnet/opus/fable = 1M, haiku = 200k) — there is no opt-in suffix.
 */
export function getClaudeContextWindow(value: string | null | undefined): number | null {
	const base = normalizeModel(value);
	if (!base) return null;
	return _registry.models.find((m) => m.value === base)?.context_window ?? null;
}

/**
 * `provider_type` values that identify an OpenAI/Codex row — the only gateway
 * kinds whose GPT-5 models expose the fast-output service tier. `chatgpt` is
 * the Codex subscription row, `openai` the API-key one.
 */
const OPENAI_PROVIDER_TYPES: readonly string[] = ['chatgpt', 'openai'];

/** Provider model ids that carry the fast tier (`gpt-5`, `gpt-5.6-sol`, …). */
const FAST_PROVIDER_MODEL_RE = /^gpt-5/i;

/**
 * True when `value` names a model that supports "fast mode" — the higher-priced
 * service tier that trades prompt-cache continuity for lower latency.
 *
 * Two shapes qualify:
 *  - a native Claude preset in the `opus` family;
 *  - a `prov:<providerId>:<modelId>` composite whose model id starts with
 *    `gpt-5` AND whose provider row is an OpenAI/Codex kind. When the provider
 *    row isn't in the hydrated registry (pre-hydration, or a row that was
 *    disabled after the pick was stored) the id match alone decides, so the
 *    toggle doesn't blink out while `/models` is still in flight.
 *
 * Everything else — haiku/sonnet/fable, bare custom ids, unset — is false.
 */
export function isFastModeCapable(value: string | null): boolean {
	if (!value) return false;
	const composite = decodeProviderModel(value);
	if (composite) {
		if (!FAST_PROVIDER_MODEL_RE.test(composite.modelId)) return false;
		const provider = _registry.providers.find((p) => p.id === composite.providerId);
		if (!provider) return true;
		return OPENAI_PROVIDER_TYPES.includes(provider.provider_type);
	}
	const base = normalizeModel(value);
	if (!base) return false;
	return _registry.models.find((m) => m.value === base)?.family === 'opus';
}

/**
 * Human-readable label for a stored model value.
 * Falls back to the raw value, or `'Default'` when nothing is set.
 *
 * A `prov:<providerId>:<modelId>` composite (the composer's cross-provider
 * override, the profile editor's selection) resolves inside its OWN provider so
 * the pill round-trips the encoded value; otherwise the Claude catalog is
 * checked first, then the hydrated provider registry (for bare non-Anthropic
 * gateway model ids like `openai/gpt-5.6`), before falling back to the raw id.
 */
export function getModelLabel(value: string | null | undefined): string {
	const composite = decodeProviderModel(value);
	if (composite) {
		const provider = _registry.providers.find((p) => p.id === composite.providerId);
		const match = provider?.models.find((m) => m.id === composite.modelId);
		return match?.label ?? composite.modelId;
	}
	const base = normalizeModel(value);
	const match = _registry.models.find((m) => m.value === base);
	if (match) return match.label;
	for (const provider of _registry.providers) {
		const providerMatch = provider.models.find((m) => m.id === base);
		if (providerMatch) return providerMatch.label;
	}
	return value && value.length > 0 ? value : 'Default';
}

/** Resolved family for a stored model value, or `null` for non-preset values. */
export function getModelFamily(value: string | null | undefined): ModelFamily | null {
	const base = normalizeModel(value);
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
	const base = normalizeModel(value).toLowerCase();
	const families: ModelFamily[] = ['haiku', 'sonnet', 'opus', 'fable'];
	return families.find((family) => base.includes(family)) ?? null;
}

/**
 * Curated models offered by a specific enabled gateway provider.
 * Empty when the id is unknown or the registry hasn't hydrated.
 *
 * NOTE: this used to scope the composer's live model-switch list to the
 * session's OWN provider (R7: cross-provider = profile edit + respawn). R7 is
 * SUPERSEDED by PA13 — the composer now offers every provider group and encodes
 * cross-provider picks with `encodeProviderModel`. This accessor survives as a
 * plain per-provider lookup.
 */
export function getProviderModels(providerId: string | null | undefined): readonly ProviderModel[] {
	if (!providerId) return [];
	return _registry.providers.find((p) => p.id === providerId)?.models ?? [];
}

/**
 * Provider-declared context window for a bare model id, across all hydrated
 * providers (optionally scoped to one provider). null when unknown/unhydrated.
 */
export function getProviderContextWindow(
	modelId: string | null | undefined,
	providerId?: string | null
): number | null {
	if (!modelId) return null;
	const providers = providerId
		? _registry.providers.filter((p) => p.id === providerId)
		: _registry.providers;
	for (const p of providers) {
		const m = p.models.find((mm) => mm.id === modelId);
		if (m?.context_window) return m.context_window;
	}
	return null;
}

/**
 * Live-reroute predicate for the composer's model pill.
 *
 * A "reroute" means the SDK's MAIN lane is running a DIFFERENT Claude family
 * than was requested for that turn. It compares at FAMILY level (an exact-id
 * check false-flags dated snapshot ids). When `providerId` is non-null the
 * session targets a non-Anthropic gateway (e.g. `openai/gpt-5.6`), which never
 * maps to a Claude family and would otherwise false-positive — so a provider
 * session is NEVER considered rerouted (R1, deterministic guard).
 */
export function isReroute(
	actualModel: string | null | undefined,
	requestedModel: string | null | undefined,
	providerId: string | null | undefined
): boolean {
	if (providerId) return false;
	if (actualModel == null || requestedModel == null) return false;
	const actualFamily = inferModelFamily(actualModel);
	const requestedFamily = inferModelFamily(requestedModel);
	if (actualFamily == null || requestedFamily == null) return false;
	return actualFamily !== requestedFamily;
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
}

interface RegistryProviderPayload {
	id: string;
	name: string;
	provider_type: string;
	models: ProviderModel[];
	default_model: string | null;
	auth_kind?: ProviderAuthKind | null;
	plan_type?: string | null;
}

interface RegistryClaudePayload {
	auth_kind?: string | null;
	plan_type?: string | null;
}

interface RegistryPayload {
	models: RegistryModelPayload[];
	default: string;
	inherit_value?: string;
	legacy_map: Record<string, string>;
	providers?: RegistryProviderPayload[];
	claude?: RegistryClaudePayload;
}

function mapPayloadModel(m: RegistryModelPayload): ModelOption {
	return { value: m.value, label: m.label, family: m.family, context_window: m.context_window };
}

/**
 * Normalise a provider entry from the payload. `auth_kind`/`plan_type` are
 * additive — an older backend omits them, so default to API-key auth (the
 * pre-existing behaviour for every non-ChatGPT row) rather than leaving the
 * field undefined for consumers.
 */
function mapPayloadProvider(p: RegistryProviderPayload): ProviderEntry {
	return {
		id: p.id,
		name: p.name,
		provider_type: p.provider_type,
		models: p.models ?? [],
		default_model: p.default_model ?? null,
		auth_kind: p.auth_kind ?? (p.provider_type === 'chatgpt' || p.provider_type === 'grok' ? 'subscription' : 'api'),
		plan_type: p.plan_type ?? null
	};
}

/**
 * Normalise the payload's `claude` block. Additive — an older backend omits it
 * entirely, and only the two known auth kinds are accepted (anything else is
 * treated as unknown rather than passed through to the display rules).
 */
function mapPayloadClaude(c: RegistryClaudePayload | undefined): ClaudeAuthInfo {
	const kind = c?.auth_kind;
	return {
		auth_kind: kind === 'api' || kind === 'subscription' ? kind : null,
		plan_type: c?.plan_type ?? null
	};
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
 *
 * Returns true when the swap happened (registry is now hydrated), false on an
 * empty payload or a failed fetch — the caller uses that to decide whether to
 * retry (see `ensureModelRegistryHydrated`).
 */
export async function hydrateModelRegistry(): Promise<boolean> {
	try {
		const payload = await api.get<RegistryPayload>('/models');
		if (!payload || !Array.isArray(payload.models) || payload.models.length === 0) {
			console.warn('[model-registry] hydrate returned empty payload, keeping static fallback');
			return false;
		}
		const models = payload.models.map(mapPayloadModel);
		// Drift-checked against the static Claude bundle only — providers are
		// entirely db-sourced (no static baseline to drift from).
		warnOnDrift(models, STATIC_MODELS);
		_registry.models = models;
		_registry.default = payload.default || STATIC_DEFAULT;
		_registry.legacyMap = payload.legacy_map ?? { ...STATIC_LEGACY_MAP };
		_registry.providers = Array.isArray(payload.providers)
			? payload.providers.map(mapPayloadProvider)
			: [];
		_registry.claude = mapPayloadClaude(payload.claude);
		_registry.hydrated = true;
		return true;
	} catch (e) {
		console.error('[model-registry] hydrate failed, keeping static fallback', e);
		return false;
	}
}

/** Backoff schedule for the background hydration retry chain. */
const HYDRATE_RETRY_DELAYS_MS = [2000, 5000, 15000, 30000];

/** At most one background retry chain may be in flight at a time. */
let _retryChainActive = false;

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Hydrate the registry, and keep trying in the background if the first attempt
 * fails.
 *
 * The boot hydrate is a single GET issued from the root layout. A page that
 * boots during a deploy/server-restart window loses that one shot, and the app
 * would then sit on the static Claude-only fallback forever (no provider
 * models, stale defaults) until the user manually reloaded. This retries on a
 * [2s, 5s, 15s, 30s] backoff, and the WebSocket `onConnected` hook in
 * `active-session.ts` re-kicks it on every (re)connect — a live socket means
 * the backend is reachable again, so a boot-time failure heals itself.
 *
 * Resolves as soon as the immediate attempt settles; the retry chain is
 * fire-and-forget so callers (e.g. the layout's boot `Promise.all`) are never
 * blocked on it. Concurrent calls each perform their own immediate attempt
 * (idempotent — each issues one fetch and swaps state atomically) but only one
 * background chain ever exists.
 */
export async function ensureModelRegistryHydrated(): Promise<void> {
	if (_registry.hydrated) return;
	if (await hydrateModelRegistry()) return;
	// Another concurrent caller may have won the race while we were in flight.
	if (_registry.hydrated) return;
	if (_retryChainActive) return;

	_retryChainActive = true;
	void (async () => {
		try {
			for (const delay of HYDRATE_RETRY_DELAYS_MS) {
				await sleep(delay);
				if (_registry.hydrated) return;
				if (await hydrateModelRegistry()) return;
			}
			console.warn('[model-registry] hydration retries exhausted, keeping static fallback');
		} finally {
			_retryChainActive = false;
		}
	})();
}
