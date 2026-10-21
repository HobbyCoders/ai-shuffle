/**
 * Per-model local tuning for a custom OpenAI-dialect provider that declares a
 * llama.cpp or vLLM server. Mirrors `app/core/local_model_settings.py`, so the
 * form rejects what the API would reject before it is sent:
 *  - temperature in [0, 2]; top_p and min_p in [0, 1];
 *  - top_k a whole number >= 0 (0 turns top-k off); repeat_penalty > 0;
 *  - finite numbers only; a blank field keeps the server default.
 * Presets are explicit opt-ins with the Qwen3 model card's values; explicit
 * fields and the reasoning switch override them. Nothing here guesses a server
 * or preset from a URL or model name.
 */
import type {
	LocalModelSettings,
	LocalPreset,
	LocalSamplingKey,
	LocalServer,
	ModelProvider,
	ProviderModelEntry
} from '$lib/api/model-providers';
import { passthroughModels } from './curation';
import { classifyRow } from './providers-model';

/** Only a real custom gateway (not a ChatGPT/Grok sign-in row) speaking OpenAI chat. */
export function supportsLocalTuning(
	row: Pick<ModelProvider, 'provider_type' | 'api_dialect' | 'auth_mode'> | null | undefined
): boolean {
	return !!row && row.provider_type === 'custom' && classifyRow(row) === 'custom' && row.api_dialect === 'openai';
}

export const SERVERS: { value: LocalServer; label: string }[] = [
	{ value: 'llama_cpp', label: 'llama.cpp' },
	{ value: 'vllm', label: 'vLLM' }
];

export const serverLabel = (server: LocalServer | undefined) =>
	SERVERS.find((s) => s.value === server)?.label ?? 'Not set';

export interface PresetInfo {
	value: LocalPreset;
	label: string;
	sampling: Partial<Record<LocalSamplingKey, number>>;
	reasoning: boolean;
}

/** Source: https://huggingface.co/Qwen/Qwen3-8B#best-practices */
export const PRESETS: PresetInfo[] = [
	{
		value: 'qwen3-thinking',
		label: 'Qwen3 thinking',
		sampling: { temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0 },
		reasoning: true
	},
	{
		value: 'qwen3-non-thinking',
		label: 'Qwen3 non-thinking',
		sampling: { temperature: 0.7, top_p: 0.8, top_k: 20, min_p: 0 },
		reasoning: false
	}
];

export const PRESET_SOURCE = 'https://huggingface.co/Qwen/Qwen3-8B#best-practices';

export const presetOf = (value: LocalPreset | undefined) => PRESETS.find((p) => p.value === value);

export interface SamplingField {
	key: LocalSamplingKey;
	label: string;
	/** The accepted range, as the row's hint. */
	range: string;
	min: number;
	max: number | null;
	/** repeat_penalty: strictly above `min`. */
	exclusiveMin?: boolean;
	integer?: boolean;
	help: string;
}

export const SAMPLING_FIELDS: SamplingField[] = [
	{
		key: 'temperature',
		label: 'Temperature',
		range: '0 to 2',
		min: 0,
		max: 2,
		help: 'Higher values make replies more varied, lower values more predictable.'
	},
	{
		key: 'top_p',
		label: 'Top P',
		range: '0 to 1',
		min: 0,
		max: 1,
		help: 'Samples only from the most likely tokens whose probabilities add up to this share.'
	},
	{
		key: 'top_k',
		label: 'Top K',
		range: 'Whole number, 0 is off',
		min: 0,
		max: null,
		integer: true,
		help: 'Samples only from this many of the most likely tokens. 0 turns the limit off (vLLM receives -1).'
	},
	{
		key: 'min_p',
		label: 'Min P',
		range: '0 to 1',
		min: 0,
		max: 1,
		help: 'Drops tokens less likely than this share of the most likely token.'
	},
	{
		key: 'repeat_penalty',
		label: 'Repeat penalty',
		range: 'Above 0, 1 is off',
		min: 0,
		max: null,
		exclusiveMin: true,
		help: 'Above 1 discourages repeating recent tokens. vLLM receives it as repetition_penalty.'
	}
];

export type ParsedSampling = { ok: true; value: number | null } | { ok: false; error: string };

/** A typed field value: blank is the server default (null); anything the API rejects is an error. */
export function parseSampling(field: SamplingField, raw: string): ParsedSampling {
	const text = raw.trim();
	if (!text) return { ok: true, value: null };
	// Number('') and Number(' ') are 0, and Number('0x1') is 1: accept plain decimals only.
	if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text)) {
		return { ok: false, error: `${field.label} must be a number (${field.range}).` };
	}
	const n = Number(text);
	if (!Number.isFinite(n)) return { ok: false, error: `${field.label} must be a finite number.` };
	// 1e21 is an integer here but reaches the API as the float 1e+21.
	if (field.integer && !Number.isSafeInteger(n)) return { ok: false, error: `${field.label} must be a whole number.` };
	const tooLow = field.exclusiveMin ? n <= field.min : n < field.min;
	if (tooLow || (field.max !== null && n > field.max)) {
		return { ok: false, error: `${field.label} must be ${field.range.toLowerCase()}.` };
	}
	return { ok: true, value: n };
}

/** What the server receives for a field: the explicit value, else the preset's, else null (server default). */
export function effectiveSampling(settings: LocalModelSettings | null | undefined, key: LocalSamplingKey): number | null {
	return settings?.sampling?.[key] ?? presetOf(settings?.preset)?.sampling[key] ?? null;
}

/** The reasoning switch the server receives; null keeps the template default. */
export function effectiveReasoning(settings: LocalModelSettings | null | undefined): boolean | null {
	if (settings?.reasoning !== undefined) return settings.reasoning;
	return presetOf(settings?.preset)?.reasoning ?? null;
}

/** Drop empty parts so the stored entry stays minimal. */
function tidy(settings: LocalModelSettings): LocalModelSettings {
	const out: LocalModelSettings = { server: settings.server };
	if (settings.preset) out.preset = settings.preset;
	const sampling = Object.fromEntries(
		Object.entries(settings.sampling ?? {}).filter(([, v]) => v !== undefined && v !== null)
	);
	if (Object.keys(sampling).length) out.sampling = sampling;
	if (settings.reasoning !== undefined) out.reasoning = settings.reasoning;
	return out;
}

export function withServer(settings: LocalModelSettings | null | undefined, server: LocalServer): LocalModelSettings {
	return tidy({ ...(settings ?? {}), server });
}

export function withSampling(
	settings: LocalModelSettings,
	key: LocalSamplingKey,
	value: number | null
): LocalModelSettings {
	const sampling = { ...(settings.sampling ?? {}) };
	if (value === null) delete sampling[key];
	else sampling[key] = value;
	return tidy({ ...settings, sampling });
}

export function withPreset(settings: LocalModelSettings, preset: LocalPreset | null): LocalModelSettings {
	const next = { ...settings };
	if (preset) next.preset = preset;
	else delete next.preset;
	return tidy(next);
}

export function withReasoning(settings: LocalModelSettings, reasoning: boolean | null): LocalModelSettings {
	const next = { ...settings };
	if (reasoning === null) delete next.reasoning;
	else next.reasoning = reasoning;
	return tidy(next);
}

/** The Sampling group's count: a preset and explicit values (the switch is under Reasoning). */
export function samplingCount(settings: LocalModelSettings | null | undefined): number {
	if (!settings) return 0;
	return (settings.preset ? 1 : 0) + Object.keys(settings.sampling ?? {}).length;
}

/** Overrides beyond the server declaration (a preset, a value or the switch). */
export function overrideCount(settings: LocalModelSettings | null | undefined): number {
	if (!settings) return 0;
	return (settings.preset ? 1 : 0) + Object.keys(settings.sampling ?? {}).length + (settings.reasoning !== undefined ? 1 : 0);
}

/**
 * The full models array for a PATCH, with only `modelId`'s local settings
 * replaced (null clears them). Every other entry and field passes through.
 */
export function modelsWithLocalSettings(
	models: ProviderModelEntry[] | undefined,
	modelId: string,
	settings: LocalModelSettings | null
): ProviderModelEntry[] {
	return passthroughModels(models).map((m) => {
		if (m.id !== modelId) return m;
		const { local_settings: _drop, ...rest } = m;
		return settings ? { ...rest, local_settings: tidy(settings) } : rest;
	});
}

/** Models with saved tuning: a dialect change must clear these first. */
export const tunedModels = (models: ProviderModelEntry[] | undefined) =>
	(models ?? []).filter((m) => !!m.local_settings);
