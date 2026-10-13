/**
 * Which agent providers can run an agent right now, for the setup wizard's
 * "AI provider" step and its first run.
 *
 * Source of truth is the worker's gate (`/auth/status` -> `agent_gate`):
 * `any_provider_ready` and `ready_providers`. An older worker does not send
 * those, so the providers store's connected credentials stand in: a provider
 * counts when one of its credentials is on (Claude: the active credential is
 * ready). fal.ai is media only and never counts.
 */
import type { AgentGateStatus } from '$lib/api/client';
import type { ModelProvider } from '$lib/api/model-providers';
import {
	AGENT_PROVIDER_ORDER, flavorOfRow, providerOfRow, type ProviderView
} from '$lib/components/settings/providers/providers-model';

/** The choices the step offers. `gateway` stands for every custom gateway. */
export type SetupProviderChoice = 'claude' | 'openai' | 'openrouter' | 'grok' | 'gateway';

export interface SetupProviderDef {
	id: SetupProviderChoice;
	name: string;
	/** One short line under the name. */
	blurb: string;
	glyph: 'claude' | 'openai' | 'openrouter' | 'grok' | 'custom';
}

const BUILT_IN_CHOICES: Record<(typeof AGENT_PROVIDER_ORDER)[number], SetupProviderDef> = {
	openai: { id: 'openai', name: 'OpenAI', blurb: 'ChatGPT subscription or OpenAI API key', glyph: 'openai' },
	grok: { id: 'grok', name: 'xAI Grok', blurb: 'SuperGrok subscription or xAI API key', glyph: 'grok' },
	claude: { id: 'claude', name: 'Claude', blurb: 'Claude subscription or Anthropic API key', glyph: 'claude' },
	openrouter: { id: 'openrouter', name: 'OpenRouter', blurb: 'One API key, many models', glyph: 'openrouter' }
};

/** Same order as Settings > Providers, then "Your own gateway" last. */
export const SETUP_PROVIDERS: readonly SetupProviderDef[] = [
	...AGENT_PROVIDER_ORDER.map((id) => BUILT_IN_CHOICES[id]),
	{ id: 'gateway', name: 'Your own gateway', blurb: 'Any OpenAI or Anthropic compatible endpoint', glyph: 'custom' }
];

export interface ProviderReadiness {
	/** At least one agent provider can run. */
	anyReady: boolean;
	/** Ready provider ids: `claude`, `openai`, `grok`, `openrouter`, `gw:<id>`. */
	ready: string[];
	/** Where the answer came from. */
	source: 'gate' | 'store';
}

/** Ready agent providers from the Settings view model (the fallback). */
export function readyFromViews(views: readonly ProviderView[]): string[] {
	return views
		.filter((p) => !p.media && !p.draft && p.credentials.some((c) => c.on && !c.draft))
		.map((p) => p.id as string);
}

export function providerReadiness(
	gate: AgentGateStatus | null | undefined,
	views: readonly ProviderView[]
): ProviderReadiness {
	if (gate && typeof gate.any_provider_ready === 'boolean') {
		const ready = Array.isArray(gate.ready_providers) ? gate.ready_providers.filter((id) => typeof id === 'string') : [];
		return { anyReady: gate.any_provider_ready, ready, source: 'gate' };
	}
	const ready = readyFromViews(views);
	return { anyReady: ready.length > 0, ready, source: 'store' };
}

/** The step's choice a ready provider id belongs to. */
export function choiceOf(id: string): SetupProviderChoice | null {
	if (id.startsWith('gw:')) return 'gateway';
	return (['claude', 'openai', 'openrouter', 'grok'] as const).find((c) => c === id) ?? null;
}

/** How many ready providers sit under a choice (gateways can be several). */
export function readyCount(choice: SetupProviderChoice, ready: readonly string[]): number {
	return ready.filter((id) => choiceOf(id) === choice).length;
}

/**
 * The wizard's stricter answer: a provider counts as connected only when the
 * worker reports it ready AND it has a model to run. Claude's models are
 * built in; every other provider needs an enabled row with at least one
 * picked model. A ready provider without one is listed in `needsModel`, so
 * the wizard never says "Connected" for something the first run cannot use.
 */
export interface SetupReadiness extends ProviderReadiness {
	/** Ready on the worker, but no model picked yet. */
	needsModel: string[];
}

export function hasPickedModel(id: string, rows: readonly ModelProvider[]): boolean {
	if (id === 'claude') return true;
	return rows.some((r) => r.enabled && providerOfRow(r) === id && (r.models?.length ?? 0) > 0);
}

export function setupReadiness(
	gate: AgentGateStatus | null | undefined,
	views: readonly ProviderView[],
	rows: readonly ModelProvider[]
): SetupReadiness {
	const base = providerReadiness(gate, views);
	// A gate that says "ready" without naming providers (should not happen) is trusted as is.
	if (base.anyReady && base.ready.length === 0) return { ...base, needsModel: [] };
	const ready = base.ready.filter((id) => hasPickedModel(id, rows));
	const needsModel = base.ready.filter((id) => !hasPickedModel(id, rows));
	return { anyReady: ready.length > 0, ready, needsModel, source: base.source };
}

/**
 * A row under a setup choice that was saved but cannot run yet: an API key or
 * gateway with no picked model (its key may also have been rejected). The
 * wizard resumes it instead of starting a second row. Newest first.
 */
export function pendingRow(choice: SetupProviderChoice, rows: readonly ModelProvider[]): ModelProvider | null {
	const stuck = rows.filter((r) =>
		r.enabled && flavorOfRow(r) !== 'device' && choiceOf(providerOfRow(r)) === choice && (r.models?.length ?? 0) === 0
	);
	stuck.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
	return stuck[0] ?? null;
}

/** Rows "Check again" asks for their models again: API keys and gateways not usable yet. */
export function rowsToRecheck(ready: readonly string[], rows: readonly ModelProvider[]): ModelProvider[] {
	return rows.filter((r) =>
		r.enabled && flavorOfRow(r) !== 'device' && (!ready.includes(providerOfRow(r)) || (r.models?.length ?? 0) === 0)
	);
}
