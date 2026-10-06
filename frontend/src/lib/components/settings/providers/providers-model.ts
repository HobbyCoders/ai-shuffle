/**
 * Providers model: turns the raw credentials (model_providers rows, the
 * Claude Code mode summary, the fal.ai key) into the list Settings shows.
 *
 * Rules (docs/settings-redesign.md, "Providers"):
 * - A provider lists only the credentials the user connected. Providers with
 *   nothing connected sit in a "Not connected" group.
 * - Credentials are independent. Every enabled OpenAI, Grok, OpenRouter and
 *   gateway row is its own group in the model picker; there is no fallback.
 *   Each connected row has its own switch; providers have none.
 * - Claude is the exception: one credential per worker, chosen by the Claude
 *   Code mode (Subscription | API key | Off). Its rows have no switches.
 * - fal.ai has one credential, the media key, with its own on/off flag.
 *
 * Pure functions only: the components and the tests feed it plain data.
 */
import type { ModelProvider } from '$lib/api/model-providers';
import type { ClaudeAuthMode, ClaudeAuthSummary } from '$lib/api/claude-auth';
import type { ProviderGlyphKind } from '$lib/components/settings/ProviderGlyph.svelte';
import type { StatusTone } from '$lib/settings/types';

// ============================================================================
// Ids
// ============================================================================

export type BuiltInProviderId = 'claude' | 'openai' | 'grok' | 'openrouter' | 'fal';
/** Built-in providers, then one `gw:<rowId>` per custom gateway (`gw:draft-<n>` while new). */
export type ProviderId = BuiltInProviderId | `gw:${string}`;

/** Collection item id of a provider (its group header and detail). */
export const providerItem = (id: ProviderId): string => `provider:${id}`;
/** Collection item id of a model_providers row. */
export const rowItem = (rowId: string): string => `mp:${rowId}`;

/** Stable item ids of the credentials that are not model_providers rows. */
export const CLAUDE_SUB_ITEM = 'claude:subscription';
export const CLAUDE_KEY_ITEM = 'claude:api-key';
export const FAL_KEY_ITEM = 'fal:key';
export const CLAUDE_PROVIDER_ITEM = 'provider:claude';

export function parseProviderItem(item: string | null | undefined): ProviderId | null {
	if (!item?.startsWith('provider:')) return null;
	return item.slice('provider:'.length) as ProviderId;
}

export function parseRowItem(item: string | null | undefined): string | null {
	return item?.startsWith('mp:') ? item.slice(3) : null;
}

// ============================================================================
// Credential kinds
// ============================================================================

/**
 * How a credential authenticates:
 * cli (Claude subscription), anthropic (Anthropic API key), device
 * (ChatGPT/SuperGrok sign-in), key (OpenAI/xAI/OpenRouter API key),
 * gateway (custom endpoint), fal (fal.ai media key).
 */
export type CredentialFlavor = 'cli' | 'anthropic' | 'device' | 'key' | 'gateway' | 'fal';

export interface CredentialKindDef {
	/** Menu and row label: "Subscription", "API key". */
	label: string;
	flavor: CredentialFlavor;
	/** Several of this kind may exist (API keys). */
	multiple: boolean;
	/** At most 5 words. */
	hint: string;
}

const KINDS: Record<BuiltInProviderId | 'gateway', CredentialKindDef[]> = {
	claude: [
		{ label: 'Subscription', flavor: 'cli', multiple: false, hint: 'Claude Code CLI sign-in' },
		{ label: 'API key', flavor: 'anthropic', multiple: false, hint: 'Pay per use' }
	],
	openai: [
		{ label: 'Subscription', flavor: 'device', multiple: false, hint: 'ChatGPT sign-in, no key' },
		{ label: 'API key', flavor: 'key', multiple: true, hint: 'Paste a key' }
	],
	grok: [
		{ label: 'Subscription', flavor: 'device', multiple: false, hint: 'SuperGrok sign-in, no key' },
		{ label: 'API key', flavor: 'key', multiple: true, hint: 'Paste a key' }
	],
	openrouter: [{ label: 'API key', flavor: 'key', multiple: true, hint: 'One key, many models' }],
	fal: [{ label: 'Media key', flavor: 'fal', multiple: false, hint: 'Also reads the balance' }],
	gateway: [{ label: 'Gateway key', flavor: 'gateway', multiple: false, hint: 'Endpoint, dialect, token' }]
};

export function kindsOf(providerId: ProviderId): CredentialKindDef[] {
	return isGatewayId(providerId) ? KINDS.gateway : KINDS[providerId];
}

export const isGatewayId = (id: ProviderId): id is `gw:${string}` => id.startsWith('gw:');

// ============================================================================
// Row classification
// ============================================================================

export type RowClass = 'chatgpt' | 'grok' | 'openai' | 'xai' | 'openrouter' | 'custom';

/**
 * Which slot a model_providers row belongs to. auth_mode wins (the Codex row
 * can be provider_type 'custom' with auth_mode 'chatgpt'), then provider_type.
 */
export function classifyRow(p: Pick<ModelProvider, 'auth_mode' | 'provider_type'>): RowClass {
	if (p.auth_mode === 'chatgpt') return 'chatgpt';
	if (p.auth_mode === 'grok' || p.provider_type === 'grok') return 'grok';
	if (p.provider_type === 'chatgpt') return 'chatgpt';
	if (p.provider_type === 'openrouter') return 'openrouter';
	if (p.provider_type === 'openai') return 'openai';
	if (p.provider_type === 'xai') return 'xai';
	return 'custom';
}

export function providerOfRow(p: ModelProvider): ProviderId {
	switch (classifyRow(p)) {
		case 'chatgpt':
		case 'openai':
			return 'openai';
		case 'grok':
		case 'xai':
			return 'grok';
		case 'openrouter':
			return 'openrouter';
		default:
			return `gw:${p.id}`;
	}
}

export function flavorOfRow(p: ModelProvider): CredentialFlavor {
	const cls = classifyRow(p);
	if (cls === 'chatgpt' || cls === 'grok') return 'device';
	if (cls === 'custom') return 'gateway';
	return 'key';
}

/** Endpoint host for summaries: "api.openai.com". */
export function hostOf(url: string | null | undefined): string {
	return (url ?? '').replace(/^https?:\/\//, '').replace(/\/.*$/, '');
}

// ============================================================================
// View model
// ============================================================================

export interface CredentialView {
	/** Collection item id. */
	item: string;
	providerId: ProviderId;
	flavor: CredentialFlavor;
	name: string;
	/** Kind badge: "Subscription", "API key", "Gateway". */
	kind: string;
	/** One line under the name. */
	summary: string;
	status: StatusTone;
	/** Status word for the detail header and screen readers. */
	statusText: string;
	/** Live: its models are in the picker (Claude: the active credential). */
	on: boolean;
	/** Has its own switch (every connected credential except Claude's). */
	switchable: boolean;
	/** Switch position, when switchable. */
	enabled: boolean;
	/** Needs the user (reconnect, sign in, missing key). */
	attention: boolean;
	row?: ModelProvider;
	/** A local draft still being set up. */
	draft?: boolean;
}

export interface ProviderView {
	id: ProviderId;
	item: string;
	name: string;
	/** Brand mark for the tile. */
	glyph: ProviderGlyphKind;
	/** Letter for providers without a brand mark (gateways, fal.ai). */
	letter: string;
	claude: boolean;
	media: boolean;
	gateway: boolean;
	credentials: CredentialView[];
	/** Credentials that are on. */
	onCount: number;
	/** A gateway still being set up. */
	draft?: boolean;
	/** Its state is not known yet (Claude before `/settings/claude-auth` answers). */
	pending?: boolean;
}

export interface DraftCredential {
	/** `draft:<n>` */
	item: string;
	providerId: ProviderId;
	flavor: CredentialFlavor;
	name: string;
	/** The model_providers row a setup already created, hidden from the list until done. */
	rowId?: string | null;
}

export interface ProvidersInput {
	rows: ModelProvider[];
	claude: ClaudeAuthSummary | null;
	anthropicKeyMasked?: string | null;
	fal: { keySet: boolean; masked: string; enabled: boolean };
	/** The usage snapshot's provider entries: their plan is fresher than the sign-in's. */
	usage?: { provider_id: string; plan_type?: string | null }[] | null;
	drafts?: DraftCredential[];
	/** New gateways being set up: provider id → name. */
	draftGateways?: { id: `gw:${string}`; name: string }[];
	/**
	 * The worker has Claude plan-usage readings (`claudeUsageRecorded`): Claude
	 * turns ran here, so Claude was in use even when its sign-in has lapsed.
	 */
	claudeUsed?: boolean;
	/**
	 * The Claude Code summary has not answered yet (no reading, no error):
	 * Claude is neither connected nor "Not connected" until it does.
	 */
	claudePending?: boolean;
}

const BUILT_IN: { id: BuiltInProviderId; name: string; glyph: ProviderGlyphKind; letter: string }[] = [
	{ id: 'openai', name: 'OpenAI', glyph: 'openai', letter: 'O' },
	{ id: 'grok', name: 'xAI Grok', glyph: 'grok', letter: 'X' },
	{ id: 'claude', name: 'Claude', glyph: 'claude', letter: 'C' },
	{ id: 'openrouter', name: 'OpenRouter', glyph: 'openrouter', letter: 'R' },
	{ id: 'fal', name: 'fal.ai', glyph: 'custom', letter: 'f' }
];

/** Agent providers in the order Settings > Providers lists them (fal.ai is media only, so left out). */
export const AGENT_PROVIDER_ORDER: readonly Exclude<BuiltInProviderId, 'fal'>[] = BUILT_IN
	.map((b) => b.id)
	.filter((id): id is Exclude<BuiltInProviderId, 'fal'> => id !== 'fal');

export const MODE_LABEL: Record<ClaudeAuthMode, string> = {
	subscription: 'Subscription',
	api_key: 'API key',
	off: 'Off'
};
export const MODE_OPTIONS: { value: ClaudeAuthMode; label: string }[] = [
	{ value: 'subscription', label: 'Subscription' },
	{ value: 'api_key', label: 'API key' },
	{ value: 'off', label: 'Off' }
];

function plural(n: number, one: string, many = `${one}s`): string {
	return `${n} ${n === 1 ? one : many}`;
}

/**
 * Whether the owner uses Claude on this worker, from what the worker reports:
 * the CLI is signed in, a key is stored, or Claude plan usage was observed
 * here recently (`claudeUsageRecorded`). A mode alone says nothing: the
 * worker records "subscription" by default, and an "API key" mode whose key
 * is gone is a leftover, not a choice. Off is never in use.
 */
export function claudeInUse(claude: ClaudeAuthSummary | null, usageRecorded = false): boolean {
	if (!claude) return false;
	if (claude.cli?.signed_in || claude.api_key?.set) return true;
	return claude.mode !== 'off' && usageRecorded;
}

/** How long a Claude usage reading shows Claude in use: 30 days. */
export const CLAUDE_USAGE_RECENT_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * The usage snapshot holds a Claude plan window observed in the last 30 days:
 * Claude turns ran on this worker recently. Readings are kept forever, so an
 * older one (or one with no `observed_at`) does not count.
 */
export function claudeUsageRecorded(
	usage: { windows?: { observed_at?: number | null }[] | null } | null | undefined,
	now: number = Date.now()
): boolean {
	return (usage?.windows ?? []).some((w) => {
		const seen = w?.observed_at;
		return typeof seen === 'number' && Number.isFinite(seen) && now - seen <= CLAUDE_USAGE_RECENT_MS;
	});
}

/**
 * The Claude credentials. Claude the owner never used is not connected,
 * whatever its mode (see `claudeInUse`). Once Claude is in use, the
 * credential the mode selects always shows, warned when not ready, so a
 * lapsed sign-in asks for attention instead of disappearing.
 */
function claudeCredentials(claude: ClaudeAuthSummary | null, keyMasked?: string | null, used = false): CredentialView[] {
	if (!claude) return [];
	if (!claudeInUse(claude, used)) return [];
	const mode = claude.mode;
	const out: CredentialView[] = [];
	const cli = claude.cli;
	const subActive = mode === 'subscription';
	if (cli.signed_in || subActive) {
		const ready = !!cli.signed_in;
		const plan = claudePlan(cli.plan);
		const account = [cli.email, plan ? `${plan} plan` : null].filter(Boolean).join(', ');
		const status: StatusTone = !ready ? 'warn' : mode === 'off' ? 'off' : subActive ? 'ok' : 'standby';
		const statusText = !ready
			? cli.installed === false
				? 'CLI not installed'
				: 'Not signed in'
			: mode === 'off'
				? 'Off'
				: subActive
					? 'Active for Claude'
					: 'Standby';
		out.push({
			item: CLAUDE_SUB_ITEM,
			providerId: 'claude',
			flavor: 'cli',
			name: plan ? `Claude ${plan} subscription` : 'Claude subscription',
			kind: 'Subscription',
			summary: ready
				? [account || 'Signed in', subActive ? '' : statusText.toLowerCase()].filter(Boolean).join(', ')
				: statusText,
			status,
			statusText,
			on: subActive && ready,
			switchable: false,
			enabled: subActive,
			attention: subActive && !ready
		});
	}
	const keyActive = mode === 'api_key';
	if (claude.api_key.set || keyActive) {
		const ready = claude.api_key.set;
		const masked = keyMasked || (claude.api_key.last4 ? `sk-ant-…${claude.api_key.last4}` : 'Key stored');
		const status: StatusTone = !ready ? 'warn' : mode === 'off' ? 'off' : keyActive ? 'ok' : 'standby';
		const statusText = !ready ? 'No key stored' : mode === 'off' ? 'Off' : keyActive ? 'Active for Claude' : 'Standby';
		out.push({
			item: CLAUDE_KEY_ITEM,
			providerId: 'claude',
			flavor: 'anthropic',
			name: 'Anthropic API key',
			kind: 'API key',
			summary: ready ? `${masked}, ${keyActive ? 'active' : statusText.toLowerCase()}` : statusText,
			status,
			statusText,
			on: keyActive && ready,
			switchable: false,
			enabled: keyActive,
			attention: keyActive && !ready
		});
	}
	return out;
}

export function claudePlan(plan: string | null | undefined): string | null {
	const value = (plan ?? '').trim().toLowerCase();
	if (!value) return null;
	const known: Record<string, string> = { max: 'Max', pro: 'Pro', team: 'Team', enterprise: 'Enterprise' };
	return known[value] ?? plan!.trim();
}

/** Plan words that read better than plain capitalization. */
const PLAN_WORDS: Record<string, string> = { supergrok: 'SuperGrok', prolite: 'Pro Lite' };

/** A subscription plan as the worker reports it ("pro", "supergrok_heavy") for display: "Pro", "SuperGrok Heavy". */
export function planLabel(plan: string | null | undefined): string | null {
	const words = (plan ?? '')
		.trim()
		.split(/[\s_-]+/)
		.filter(Boolean);
	if (!words.length || (words.length === 1 && words[0].toLowerCase() === 'unknown')) return null;
	return words
		.map((w) => PLAN_WORDS[w.toLowerCase()] ?? w.charAt(0).toUpperCase() + w.slice(1))
		.join(' ');
}

/**
 * Title of a ChatGPT or SuperGrok subscription, in the Claude row's pattern:
 * "OpenAI Plus subscription", "SuperGrok Heavy subscription". Without a plan:
 * "OpenAI subscription", "SuperGrok subscription".
 */
export function subscriptionName(cls: RowClass, plan: string | null): string {
	const brand = cls === 'grok' ? 'SuperGrok' : 'OpenAI';
	if (!plan) return `${brand} subscription`;
	return plan.startsWith(brand) ? `${plan} subscription` : `${brand} ${plan} subscription`;
}

/** One model_providers row as a credential. `usagePlan` is the usage snapshot's plan, when loaded. */
export function rowCredential(row: ModelProvider, usagePlan?: string | null): CredentialView {
	const flavor = flavorOfRow(row);
	const cls = classifyRow(row);
	const models = row.models?.length ?? 0;
	const providerId = providerOfRow(row);
	if (flavor === 'device') {
		const signedIn = !!row.codex_access_token_set && !row.needs_reauth;
		const status: StatusTone = !row.enabled ? 'off' : signedIn ? 'ok' : 'warn';
		const statusText = row.needs_reauth
			? 'Reconnect needed'
			: !row.codex_access_token_set
				? 'Not signed in'
				: row.enabled
					? 'On'
					: 'Off';
		const plan = planLabel(usagePlan) ?? planLabel(row.account_plan);
		const email = row.account_email?.trim() || null;
		const account = [email, plan ? `${plan} plan` : null].filter(Boolean).join(', ');
		return {
			item: rowItem(row.id),
			providerId,
			flavor,
			name: subscriptionName(cls, plan),
			kind: 'Subscription',
			summary: !signedIn
				? statusText
				: account
					? `${account}${row.enabled ? '' : ', off'}`
					: row.enabled
						? plural(models, 'model')
						: 'Off',
			status,
			statusText: !signedIn ? statusText : row.enabled ? 'On' : 'Off',
			on: row.enabled && signedIn,
			switchable: true,
			enabled: row.enabled,
			attention: row.enabled && !signedIn,
			row
		};
	}
	const host = hostOf(row.base_url);
	return {
		item: rowItem(row.id),
		providerId,
		flavor,
		name: flavor === 'gateway' ? 'Gateway key' : keyName(row.name),
		kind: flavor === 'gateway' ? 'Gateway' : 'API key',
		summary: row.enabled ? `${host}, ${plural(models, 'model')}${row.auth_token_set ? '' : ', no token'}` : 'Off',
		status: row.enabled ? 'ok' : 'off',
		statusText: row.enabled ? 'On' : 'Off',
		on: row.enabled,
		switchable: true,
		enabled: row.enabled,
		attention: false,
		row
	};
}

/** Older key rows carry the bare brand ("OpenAI"); under the brand's header that reads as "OpenAI API key". */
function keyName(name: string | null | undefined): string {
	const n = (name ?? '').trim();
	if (!n) return 'API key';
	return ['openai', 'grok', 'xai', 'openrouter'].includes(n.toLowerCase()) ? `${n} API key` : n;
}

function falCredential(fal: ProvidersInput['fal']): CredentialView[] {
	if (!fal.keySet) return [];
	return [
		{
			item: FAL_KEY_ITEM,
			providerId: 'fal',
			flavor: 'fal',
			name: 'Media key',
			kind: 'API key',
			summary: fal.enabled ? fal.masked || 'Key stored' : 'Off',
			status: fal.enabled ? 'ok' : 'off',
			statusText: fal.enabled ? 'On' : 'Off',
			on: fal.enabled,
			switchable: true,
			enabled: fal.enabled,
			attention: false
		}
	];
}

function draftCredential(d: DraftCredential): CredentialView {
	return {
		item: d.item,
		providerId: d.providerId,
		flavor: d.flavor,
		name: d.name,
		kind: 'Draft',
		summary: 'Setting up',
		status: 'busy',
		statusText: 'Setting up',
		on: false,
		switchable: false,
		enabled: false,
		attention: false,
		draft: true
	};
}

/** Every provider in display order; connected and not connected alike. */
export function buildProviders(input: ProvidersInput): ProviderView[] {
	const drafts = input.drafts ?? [];
	const hidden = new Set(drafts.map((d) => d.rowId).filter((id): id is string => !!id));
	const rows = input.rows.filter((r) => !hidden.has(r.id));
	const byProvider = new Map<ProviderId, CredentialView[]>();
	const push = (id: ProviderId, c: CredentialView) => byProvider.set(id, [...(byProvider.get(id) ?? []), c]);

	for (const c of claudeCredentials(input.claude, input.anthropicKeyMasked, input.claudeUsed)) push('claude', c);
	// Subscriptions first, then keys, in the order the worker returns them.
	const ordered = [...rows].sort((a, b) => Number(flavorOfRow(a) !== 'device') - Number(flavorOfRow(b) !== 'device'));
	const usagePlans = new Map((input.usage ?? []).map((u) => [u.provider_id, u.plan_type ?? null]));
	for (const row of ordered) push(providerOfRow(row), rowCredential(row, usagePlans.get(row.id)));
	for (const c of falCredential(input.fal)) push('fal', c);
	for (const d of drafts) push(d.providerId, draftCredential(d));

	const views: ProviderView[] = BUILT_IN.map((b) => view(b.id, b.name, b.glyph, b.letter, byProvider.get(b.id) ?? []));
	if (input.claudePending && !input.claude) {
		const claude = views.find((v) => v.claude);
		if (claude && claude.credentials.length === 0) claude.pending = true;
	}
	for (const row of rows) {
		if (classifyRow(row) !== 'custom') continue;
		const id = providerOfRow(row);
		views.push(view(id, row.name || 'Gateway', 'custom', letterOf(row.name), byProvider.get(id) ?? []));
	}
	for (const g of input.draftGateways ?? []) {
		const v = view(g.id, g.name || 'New provider', 'custom', letterOf(g.name, '+'), byProvider.get(g.id) ?? []);
		v.draft = true;
		views.push(v);
	}
	return views;
}

function letterOf(name: string | null | undefined, fallback = 'G'): string {
	const ch = (name ?? '').trim().charAt(0);
	return ch ? ch.toUpperCase() : fallback;
}

function view(
	id: ProviderId,
	name: string,
	glyph: ProviderGlyphKind,
	letter: string,
	credentials: CredentialView[]
): ProviderView {
	return {
		id,
		item: providerItem(id),
		name,
		glyph,
		letter,
		claude: id === 'claude',
		media: id === 'fal',
		gateway: isGatewayId(id),
		credentials,
		onCount: credentials.filter((c) => c.on).length
	};
}

export const isConnected = (p: ProviderView): boolean => p.credentials.length > 0 || !!p.draft;

/** Connected providers (with their credentials) and the "Not connected" group. */
export function splitProviders(views: ProviderView[]): { connected: ProviderView[]; notConnected: ProviderView[] } {
	return {
		// A provider whose state is still loading keeps its place among the
		// connected ones (neutral, "Checking") rather than flashing "Not connected".
		connected: views.filter((p) => isConnected(p) || !!p.pending),
		notConnected: views.filter((p) => !isConnected(p) && !p.pending)
	};
}

/** Keep providers and credentials that match a filter; a provider-name hit keeps all its rows. */
export function filterProviders(views: ProviderView[], query: string): ProviderView[] {
	const q = query.trim().toLowerCase();
	if (!q) return views;
	const out: ProviderView[] = [];
	for (const p of views) {
		if (p.name.toLowerCase().includes(q)) {
			out.push(p);
			continue;
		}
		const creds = p.credentials.filter((c) => `${c.name} ${c.summary} ${c.kind}`.toLowerCase().includes(q));
		if (creds.length) out.push({ ...p, credentials: creds });
	}
	return out;
}

// ============================================================================
// Connector line
// ============================================================================

export interface LineState {
	/** The header's stub into the first row is teal. */
	lit: boolean;
	rows: { on: boolean; nextOn: boolean; last: boolean }[];
}

/**
 * The connector line runs from the provider tile down through its credentials.
 * A segment is teal (`--primary`) where it leads into a credential that is on;
 * for Claude that is the active credential only.
 */
export function lineState(credentials: Pick<CredentialView, 'on'>[]): LineState {
	return {
		lit: credentials[0]?.on ?? false,
		rows: credentials.map((c, i) => ({
			on: c.on,
			nextOn: credentials[i + 1]?.on ?? false,
			last: i === credentials.length - 1
		}))
	};
}

// ============================================================================
// "+" menu and setup
// ============================================================================

export interface AddableKind extends CredentialKindDef {
	/** "API key", or "Another API key" when one exists. */
	menuLabel: string;
}

/** Kinds the "+" offers: the ones not added yet, plus another of a kind that allows several. */
export function addableKinds(p: Pick<ProviderView, 'id' | 'credentials'>): AddableKind[] {
	const present = new Set(p.credentials.map((c) => c.flavor));
	return kindsOf(p.id)
		.filter((k) => k.multiple || !present.has(k.flavor))
		.map((k) => ({ ...k, menuLabel: k.multiple && present.has(k.flavor) ? `Another ${k.label}` : k.label }));
}

/** The "+" button is shown on every provider except Claude (it has the mode control). */
export const hasPlusMenu = (p: Pick<ProviderView, 'claude'>): boolean => !p.claude;

const DRAFT_BASE: Record<CredentialFlavor, (p: ProviderId) => string> = {
	cli: () => 'Claude subscription',
	anthropic: () => 'Anthropic API key',
	device: (p) => (p === 'openai' ? 'ChatGPT subscription' : 'SuperGrok subscription'),
	key: (p) => `${p === 'openai' ? 'OpenAI' : p === 'grok' ? 'Grok' : 'OpenRouter'} API key`,
	gateway: () => 'Gateway key',
	fal: () => 'Media key'
};

/** "OpenAI API key", then "OpenAI API key 2" when one with that name exists. */
export function draftName(providerId: ProviderId, flavor: CredentialFlavor, existing: { name: string }[]): string {
	const base = DRAFT_BASE[flavor](providerId);
	const n = existing.filter((c) => c.name.startsWith(base)).length;
	return n ? `${base} ${n + 1}` : base;
}

/** Fixed endpoints of the API-key providers. */
export const KEY_CONFIG: Record<
	'openai' | 'grok' | 'openrouter',
	{ name: string; provider_type: string; base_url: string; placeholder: string }
> = {
	openai: { name: 'OpenAI', provider_type: 'openai', base_url: 'https://api.openai.com/v1', placeholder: 'sk-…' },
	grok: { name: 'Grok', provider_type: 'xai', base_url: 'https://api.x.ai/v1', placeholder: 'xai-…' },
	openrouter: {
		name: 'OpenRouter',
		provider_type: 'openrouter',
		base_url: 'https://openrouter.ai/api',
		placeholder: 'sk-or-…'
	}
};

// ============================================================================
// Claude
// ============================================================================

/** The Claude provider header status. */
export function claudeHeaderStatus(claude: ClaudeAuthSummary | null): { status: StatusTone; text: string } {
	if (!claude) return { status: 'off', text: 'Loading' };
	if (claude.mode === 'off') return { status: 'off', text: 'Off, not in the picker' };
	const ready = claude.gate
		? claude.gate.claude_credential_ready
		: claude.mode === 'subscription'
			? !!claude.cli.signed_in
			: claude.api_key.set;
	if (!ready) return { status: 'warn', text: 'Not ready' };
	return { status: 'ok', text: claude.mode === 'subscription' ? 'Runs on subscription' : 'Runs on API key' };
}

/**
 * The warning on the Claude provider page, or null. Keys off Claude's own
 * credential (`claude_credential_ready`), not the gate's `ready`: that one
 * says whether any provider can run, so it stays true while Claude alone is
 * not ready. A license block shows the server's reason.
 */
export function claudeGateNotice(claude: ClaudeAuthSummary | null): string | null {
	const gate = claude?.gate;
	if (!claude || !gate || claude.mode === 'off') return null;
	if (gate.license_allows_runs === false) return gate.reason || 'The license does not allow new agent runs.';
	if (gate.claude_credential_ready !== false) return null;
	return claude.mode === 'api_key'
		? 'Claude cannot run: no Anthropic API key is stored on this worker.'
		: 'Claude cannot run: the Claude Code CLI is not signed in on this worker.';
}

/** Credentials that need the user: the index badge counts these. */
export function attentionCount(views: ProviderView[]): number {
	return views.reduce((n, p) => n + p.credentials.filter((c) => c.attention).length, 0);
}

// ============================================================================
// Native image generation
// ============================================================================

/** Rows whose Responses provider can run GenerateNativeImage. */
export function supportsNativeImages(row: ModelProvider | undefined): boolean {
	if (!row?.enabled) return false;
	return (
		['openai', 'chatgpt', 'xai', 'grok'].includes(row.provider_type) ||
		['chatgpt', 'grok'].includes(row.auth_mode ?? '')
	);
}
