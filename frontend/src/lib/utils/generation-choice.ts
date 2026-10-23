/**
 * Generation choice card: reading a unified generation tool result and
 * turning a pick into the reply the agent acts on.
 *
 * `generateImage` / `generateVideo` / `generateAudio` / `textTo3D` return
 * `status: "choose"` when nothing was named and nothing is remembered
 * (app/core/generation_resolver.py). The chat renders that result as a card.
 */
import type { ChatMessage } from '$lib/stores/types';
import { displayToolName } from './tool-icons';
import { stripUntrustedFence } from './untrusted-fence';

export type GenerationKind = 'image' | 'video' | 'audio' | '3d';

export const GENERATION_TOOLS: Record<string, GenerationKind> = {
	generateImage: 'image',
	generateVideo: 'video',
	generateAudio: 'audio',
	textTo3D: '3d'
};

export const KIND_LABEL: Record<GenerationKind, { noun: string; plural: string }> = {
	image: { noun: 'image', plural: 'images' },
	video: { noun: 'video', plural: 'videos' },
	audio: { noun: 'audio clip', plural: 'audio' },
	'3d': { noun: '3D model', plural: '3D models' }
};

/** Same threshold as the resolver: above it the agent asks before spending. */
export const CONFIRM_ABOVE_USD = 0.1;

export interface GenerationPrice {
	currency: string;
	unit: string;
	amount: string | null;
	total: string | null;
	status: 'estimated' | 'quoted' | 'included' | 'reported' | 'unknown' | string;
}

/** fal schema field (app/services/fal_schema.py ParamField). */
export interface SchemaField {
	name: string;
	type: string;
	title?: string;
	description?: string;
	required?: boolean;
	default?: unknown;
	enum?: unknown[] | null;
	minimum?: number | null;
	maximum?: number | null;
	is_file?: boolean;
	is_primary_text?: boolean;
}

/** A native capability value (app/core/native_generation_settings.py). */
export interface NativeValue {
	value: unknown;
	verified: boolean;
	selectable: boolean;
	reason?: string;
}

export interface GenerationOption {
	source: string;
	account: string;
	worker: string;
	model: string;
	kind: GenerationKind;
	options: Record<string, unknown>;
	verified: boolean;
	price: GenerationPrice;
	quick_settings?: Record<string, unknown>;
	schema?: { fields?: SchemaField[] | Record<string, NativeValue[]>; [key: string]: unknown } | null;
	available: boolean;
	reason?: string | null;
	label?: string;
}

export interface GenerationChoice {
	source?: string;
	account?: string;
	worker?: string;
	model: string;
	options: Record<string, unknown>;
	origin?: string;
}

export interface ChooseResult {
	kind: GenerationKind;
	tool: string;
	options: GenerationOption[];
	prompt: string;
	rememberedUnavailable: boolean;
}

export interface RememberedResult {
	kind: GenerationKind;
	choice: GenerationChoice;
	price: GenerationPrice | null;
}

/** The unified generation tool a row called, or null for any other tool. */
export function generationToolKind(toolName: string | undefined): GenerationKind | null {
	if (!toolName) return null;
	const bare = displayToolName(toolName);
	const kind = GENERATION_TOOLS[bare];
	if (!kind) return null;
	// Bare names (native loop) or the bundled ai-tools server only; a user MCP
	// server that happens to name a tool `generateImage` is not ours.
	if (bare === toolName || /(^|__)ai-tools__/.test(toolName)) return kind;
	return null;
}

function parseResult(text: string | undefined): Record<string, unknown> | null {
	const raw = stripUntrustedFence(text)?.trim();
	if (!raw || raw[0] !== '{') return null;
	try {
		const value = JSON.parse(raw);
		return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
	} catch {
		return null;
	}
}

function isOption(value: unknown): value is GenerationOption {
	if (!value || typeof value !== 'object') return false;
	const o = value as Record<string, unknown>;
	return (
		typeof o.source === 'string' &&
		typeof o.account === 'string' &&
		typeof o.worker === 'string' &&
		typeof o.model === 'string' &&
		!!o.price &&
		typeof o.price === 'object'
	);
}

/** A `choose` result on a unified generation tool row, else null. */
export function chooseResult(message: Pick<ChatMessage, 'type' | 'toolName' | 'toolResult' | 'toolInput'>): ChooseResult | null {
	if (message.type !== 'tool_use') return null;
	const kind = generationToolKind(message.toolName);
	if (!kind) return null;
	const result = parseResult(message.toolResult);
	if (!result || result.status !== 'choose' || !Array.isArray(result.options)) return null;
	const options = result.options.filter(isOption).map((o) => ({
		...o,
		kind,
		options: o.options && typeof o.options === 'object' ? o.options : {},
		available: o.available !== false
	}));
	if (!options.length) return null;
	const prompt = typeof message.toolInput?.prompt === 'string' ? message.toolInput.prompt : '';
	return {
		kind,
		tool: displayToolName(message.toolName!),
		options,
		prompt,
		rememberedUnavailable: result.remembered_unavailable === true
	};
}

/** A completed/failed/confirmation result that ran the remembered choice. */
export function rememberedResult(message: Pick<ChatMessage, 'type' | 'toolName' | 'toolResult'>): RememberedResult | null {
	if (message.type !== 'tool_use') return null;
	const kind = generationToolKind(message.toolName);
	if (!kind) return null;
	const result = parseResult(message.toolResult);
	if (!result || result.remembered !== true) return null;
	const choice = result.choice as GenerationChoice | undefined;
	if (!choice || typeof choice.model !== 'string') return null;
	return { kind, choice, price: (result.price as GenerationPrice) ?? null };
}

/**
 * A tool row the transcript keeps on screen after the turn settles: a choice
 * card asks the reader to decide, and a run that spent on the remembered
 * choice without asking keeps its "Ask me next time / Change" line in view.
 */
export function isGenerationChoiceRow(message: ChatMessage): boolean {
	return chooseResult(message) !== null || rememberedResult(message) !== null;
}

// ── Labels ─────────────────────────────────────────────────────────────

const SOURCE_LABEL: Record<string, string> = {
	fal: 'fal.ai',
	chatgpt: 'ChatGPT plan',
	supergrok: 'SuperGrok',
	openai: 'OpenAI API',
	xai: 'xAI API'
};

export function sourceLabel(source: string | undefined): string {
	if (!source) return 'Saved account';
	return SOURCE_LABEL[source] ?? source;
}

/** `fal-ai/flux/dev` → `Flux Dev`; native `default` → `Plan default`. */
export function modelLabel(model: string): string {
	if (!model) return 'Model';
	if (model === 'default') return 'Plan default';
	const tail = model.includes('/') ? model.split('/').filter(Boolean).slice(1).join(' ') || model : model;
	return tail
		.replace(/[-_]+/g, ' ')
		.replace(/\s+/g, ' ')
		.trim()
		.replace(/\b([a-z])/g, (c) => c.toUpperCase())
		.replace(/\b[A-Z][a-z0-9]*\b/g, (word) => ACRONYMS.get(word.toLowerCase()) ?? word);
}

/** Words a title-cased model id would get wrong (`Gpt Image 2` → `GPT Image 2`). */
const ACRONYMS = new Map(['gpt', 'hd', 'sd', 'sdxl', 'ai', 'xai', 'tts', 'ltx', 'v2v', 'i2v', 't2v', '3d'].map((w) => [w, w.toUpperCase()]));

function money(value: string | null): number | null {
	if (value === null || value === undefined) return null;
	const n = Number(value);
	return Number.isFinite(n) ? n : null;
}

export function formatUsd(n: number): string {
	if (n === 0) return '$0';
	if (n < 0.01) return `$${n.toFixed(4).replace(/0+$/, '')}`;
	if (n < 1) return `$${n.toFixed(3).replace(/0$/, '')}`;
	return `$${n.toFixed(2)}`;
}

export interface PriceView {
	/** Big figure: `$0.025`, `Included`, `Unknown`. */
	figure: string;
	/** Small print: `per image`, `in your plan`, `3 × $0.02`. */
	note: string;
	/** Can run (a known price); unknown prices are refused by the resolver. */
	priced: boolean;
	/** Above the confirmation threshold: the agent asks before spending. */
	asksFirst: boolean;
	total: number | null;
}

export function priceView(price: GenerationPrice | null | undefined, kind: GenerationKind): PriceView {
	const noun = KIND_LABEL[kind].noun;
	if (!price || price.currency !== 'USD') return { figure: 'Unknown', note: 'price unavailable', priced: false, asksFirst: false, total: null };
	if (price.status === 'included') return { figure: 'Included', note: 'in your plan', priced: true, asksFirst: false, total: 0 };
	const amount = money(price.amount);
	const total = money(price.total);
	if (amount === null || total === null || price.status === 'unknown') {
		return { figure: 'Unknown', note: 'price unavailable', priced: false, asksFirst: false, total: null };
	}
	const count = amount > 0 ? Math.round(total / amount) : 1;
	const note = count > 1 ? `${count} × ${formatUsd(amount)}` : `per ${noun}${price.status === 'estimated' ? ', estimated' : ''}`;
	return {
		figure: formatUsd(total),
		note,
		priced: true,
		asksFirst: Math.max(total, amount) > CONFIRM_ABOVE_USD,
		total
	};
}

export function formatValue(value: unknown): string {
	if (value === null || value === undefined || value === '') return 'default';
	if (typeof value === 'boolean') return value ? 'on' : 'off';
	if (typeof value === 'object') return JSON.stringify(value);
	return String(value);
}

/** Up to `max` short `name value` chips for the quick settings line. */
export function quickChips(option: Pick<GenerationOption, 'quick_settings' | 'options'>, max = 3): string[] {
	const source = option.quick_settings && Object.keys(option.quick_settings).length ? option.quick_settings : option.options;
	return Object.entries(source ?? {})
		.filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
		.slice(0, max)
		.map(([k, v]) => `${k.replace(/_/g, ' ')} ${formatValue(v)}`);
}

export function choiceOf(option: GenerationOption, options: Record<string, unknown> = option.options): GenerationChoice {
	return { source: option.source, account: option.account, worker: option.worker, model: option.model, options };
}

export function sameChoice(a: Partial<GenerationChoice> | null | undefined, b: Partial<GenerationChoice> | null | undefined): boolean {
	if (!a || !b) return false;
	return (['source', 'account', 'worker', 'model'] as const).every((k) => !a[k] || !b[k] || a[k] === b[k]) && a.model === b.model;
}

/** The price figure a `pickMessage` reply names (`$0.08`, `Included`), or null. */
export function repliedFigure(reply: string): string | null {
	const firstLine = reply.split('\n', 1)[0];
	if (/^Use .+ for this .+, included in my plan\./.test(firstLine)) return 'Included';
	return firstLine.match(/^Use .+ for this .+? \((\$\d+(?:\.\d+)?)\)\./)?.[1] ?? null;
}

/**
 * The user's reply: a plain sentence they can read, with the exact arguments
 * the agent passes back (the resolver needs account, model, worker, options).
 */
export function pickMessage(tool: string, kind: GenerationKind, option: GenerationOption, options: Record<string, unknown>, price: PriceView, remember: boolean): string {
	const label = `${sourceLabel(option.source)} · ${modelLabel(option.model)}`;
	const cost = price.priced ? (price.figure === 'Included' ? ', included in my plan' : ` (${price.figure})`) : '';
	const args = JSON.stringify({ source: option.account, model: option.model, worker: option.worker, options });
	const memory = remember ? ` I saved it as my choice for ${KIND_LABEL[kind].plural}.` : '';
	return `Use ${label} for this ${KIND_LABEL[kind].noun}${cost}.${memory}\nCall ${tool} again with the same prompt and ${args}.`;
}
