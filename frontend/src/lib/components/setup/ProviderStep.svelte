<script lang="ts">
	/**
	 * Wizard step: connect at least one AI provider. Every agent provider is an
	 * equal choice (no default, no badge); picking one opens its connect flow
	 * inline: Claude's own (ClaudeConnect) or the Settings > Providers setups
	 * (ProviderConnect). Several can be connected.
	 *
	 * Blocking: Continue is enabled once at least one agent provider is
	 * connected: the worker's gate (`/auth/status` -> `agent_gate`) reports it in
	 * `ready_providers` (an older worker: the providers store) AND it has at
	 * least one picked model (see `setupReadiness`). A ready provider without a
	 * model is named so the owner can open it and pick one.
	 *
	 * "Check again" re-reads the status and first asks every API key or
	 * gateway row that is not usable yet for its models again, so a key fixed
	 * at the provider or a provider back from an outage is picked up.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Check, ChevronDown, Loader2, RefreshCw } from 'lucide-svelte';
	import type { AgentGateStatus } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { fetchProviderModels } from '$lib/api/model-providers';
	import ProviderGlyph from '$lib/components/settings/ProviderGlyph.svelte';
	import { buildProviders, type ProviderId } from '$lib/components/settings/providers/providers-model';
	import ClaudeConnect from './ClaudeConnect.svelte';
	import ProviderConnect from './ProviderConnect.svelte';
	import {
		SETUP_PROVIDERS, pendingRow, providerReadiness, readyCount, rowsToRecheck, setupReadiness,
		type SetupProviderChoice
	} from './provider-readiness';

	interface Props {
		isAdmin: boolean;
		onContinue: () => void;
		onBack?: () => void;
		/** A provider finished connecting in this step. */
		onConnected?: (providerId: string) => void;
		/** How often to re-check while nothing is ready, in ms. */
		pollMs?: number;
	}
	let { isAdmin, onContinue, onBack, onConnected, pollMs = 6000 }: Props = $props();

	let open = $state<SetupProviderChoice | null>(null);
	let gate = $state<AgentGateStatus | null>(null);
	let loaded = $state(false);
	let checking = $state(false);
	let timer: ReturnType<typeof setInterval> | undefined;
	let disposed = false;

	const s = $derived(providers.current);
	const views = $derived(buildProviders({
		rows: s.providers,
		claude: s.claude,
		anthropicKeyMasked: s.anthropicKeyMasked,
		fal: { keySet: s.falKeySet, masked: s.falKeyMasked, enabled: s.falEnabled },
	}));
	const readiness = $derived(setupReadiness(gate, views, s.providers));

	const NAMES: Record<string, string> = { claude: 'Claude', openai: 'OpenAI', grok: 'xAI Grok', openrouter: 'OpenRouter' };
	function nameOf(id: string): string {
		return NAMES[id] ?? views.find((v) => v.id === id)?.name ?? 'Your gateway';
	}
	const readyNames = $derived(readiness.ready.map(nameOf));
	const needsModelNames = $derived(readiness.needsModel.map(nameOf));

	/**
	 * Read the status again. `refetch` (Check again) first asks the rows that
	 * are not usable yet for their models: the worker records whether the key
	 * was accepted, which is what makes it ready.
	 */
	async function recheck(refetch = false) {
		if (checking) return;
		checking = true;
		try {
			if (refetch) {
				const raw = providerReadiness(gate, views).ready;
				const stale = rowsToRecheck(raw, s.providers);
				await Promise.all(stale.map((r) => fetchProviderModels(r.id).catch(() => null)));
			}
			const [status] = await Promise.all([
				auth.checkAuth().catch(() => auth.current.status),
				providers.refreshAll().catch(() => {}),
			]);
			if (!disposed) gate = status?.agent_gate ?? null;
		} finally {
			checking = false;
			loaded = true;
		}
	}

	function toggle(choice: SetupProviderChoice) {
		open = open === choice ? null : choice;
	}

	async function connected(id: string) {
		onConnected?.(id);
		await recheck();
	}

	onMount(() => {
		gate = auth.current.status?.agent_gate ?? null;
		void recheck();
		// A sign-in finished elsewhere (another tab, a host terminal) shows up on its own.
		timer = setInterval(() => { if (!readiness.anyReady) void recheck(); }, pollMs);
	});
	onDestroy(() => { disposed = true; clearInterval(timer); });
</script>

<div class="space-y-4" data-testid="setup-provider">
	<p class="text-xs text-muted-foreground leading-relaxed">
		Subscriptions (ChatGPT, SuperGrok, Claude) and API keys both work. Your provider bills you directly.
	</p>

	<ul class="choices" aria-label="AI providers">
		{#each SETUP_PROVIDERS as p (p.id)}
			{@const count = readyCount(p.id, readiness.ready)}
			<li class="choice" class:open={open === p.id} class:connected={count > 0}>
				<button type="button" class="choice-head" aria-expanded={open === p.id}
					aria-controls="setup-provider-{p.id}" onclick={() => toggle(p.id)} data-testid="setup-provider-{p.id}">
					<span class="mark" aria-hidden="true"><ProviderGlyph kind={p.glyph} size={20} /></span>
					<span class="text">
						<span class="name">{p.name}</span>
						<span class="blurb">{p.blurb}</span>
					</span>
					{#if count > 0}
						<span class="pill" data-testid="setup-provider-{p.id}-connected">
							<Check size={12} strokeWidth={2.5} />{count > 1 ? `${count} connected` : 'Connected'}
						</span>
					{/if}
					<ChevronDown size={16} class="chev" aria-hidden="true" />
				</button>
				{#if open === p.id}
					<div class="panel" id="setup-provider-{p.id}">
						{#if p.id === 'claude'}
							<ClaudeConnect {isAdmin} onReady={() => void connected('claude')} />
						{:else}
							<ProviderConnect provider={p.id}
								existing={p.id === 'gateway' ? [] : (views.find((v) => v.id === p.id)?.credentials ?? [])}
								pending={pendingRow(p.id, s.providers)}
								connected={count > 0}
								onConnected={(id: ProviderId) => void connected(id)}
								onChanged={() => void recheck()} />
						{/if}
					</div>
				{/if}
			</li>
		{/each}
	</ul>

	{#if readiness.anyReady}
		<div role="status" class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-start gap-2" data-testid="setup-provider-ready">
			<Check size={16} strokeWidth={2} class="shrink-0 mt-0.5" />
			<span>{readyNames.length ? `Connected: ${readyNames.join(', ')}.` : 'A provider is connected.'} You can add more now or later.</span>
		</div>
	{:else if loaded}
		<div role="status" class="bg-muted/40 border border-border px-3 py-2.5 rounded-lg text-sm text-muted-foreground flex items-center justify-between gap-3" data-testid="setup-provider-missing">
			<span>
				{#if needsModelNames.length}
					{needsModelNames.join(', ')} {needsModelNames.length > 1 ? 'have' : 'has'} no model picked yet. Open {needsModelNames.length > 1 ? 'one' : 'it'} above and pick a model to continue.
				{:else}
					Connect at least one provider to continue.
				{/if}
			</span>
			<button type="button" class="recheck" onclick={() => void recheck(true)} disabled={checking}>
				{#if checking}<Loader2 size={14} class="animate-spin" />{:else}<RefreshCw size={14} />{/if}Check again
			</button>
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<button type="button" onclick={onContinue} class="btn btn-primary flex-1 py-3" disabled={!readiness.anyReady}>Continue</button>
	</div>
</div>

<style>
	.choices { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
	.choice {
		position: relative;
		border: 1px solid var(--border);
		border-radius: 0.625rem;
		background: color-mix(in srgb, var(--muted) 25%, transparent);
		transition: border-color 0.2s ease-out, background-color 0.2s ease-out, box-shadow 0.2s ease-out;
	}
	/* The open choice grows a primary rail down its left edge, into its panel. */
	.choice::before {
		content: '';
		position: absolute;
		left: -1px; top: 0.625rem; bottom: 0.625rem;
		width: 3px;
		border-radius: 0 3px 3px 0;
		background: var(--primary);
		transform: scaleY(0);
		transform-origin: top;
		transition: transform 0.25s cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	.choice.open::before { transform: scaleY(1); }
	.choice.open { border-color: color-mix(in srgb, var(--primary) 55%, var(--border)); background: var(--card); box-shadow: 0 6px 24px color-mix(in srgb, var(--foreground) 7%, transparent); }
	.choice.connected:not(.open) { border-color: color-mix(in srgb, var(--success) 40%, var(--border)); }
	.choice-head {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		width: 100%;
		min-height: 56px;
		padding: 0.625rem 0.875rem;
		background: transparent;
		border: 0;
		border-radius: 0.625rem;
		text-align: left;
		cursor: pointer;
		color: var(--foreground);
	}
	.choice-head:hover { background: color-mix(in srgb, var(--foreground) 3%, transparent); }
	.choice-head:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.mark {
		display: grid; place-items: center;
		width: 2.25rem; height: 2.25rem; flex: none;
		border-radius: 0.5rem;
		background: var(--background);
		border: 1px solid var(--border);
		color: var(--foreground);
	}
	.text { min-width: 0; flex: 1; }
	.name { display: block; font-size: 0.9375rem; font-weight: 600; line-height: 1.3; }
	.blurb { display: block; font-size: 0.75rem; color: var(--muted-foreground); line-height: 1.4; }
	.pill {
		display: inline-flex; align-items: center; gap: 0.25rem; flex: none;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		font-size: 0.6875rem; font-weight: 600;
		color: var(--success);
		background: color-mix(in srgb, var(--success) 12%, transparent);
		border: 1px solid color-mix(in srgb, var(--success) 30%, transparent);
	}
	.choice-head :global(.chev) { flex: none; color: var(--muted-foreground); transition: transform 0.2s ease-out; }
	.choice.open .choice-head :global(.chev) { transform: rotate(180deg); }
	.panel {
		padding: 0.25rem 0.875rem 0.875rem;
		animation: panel-in 0.22s ease-out;
	}
	@keyframes panel-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
	.recheck {
		display: inline-flex; align-items: center; gap: 0.375rem; flex: none;
		font-size: 0.8125rem; color: var(--primary);
		background: none; border: 0; padding: 0.25rem; cursor: pointer;
	}
	.recheck:disabled { opacity: 0.6; cursor: default; }
	@media (max-width: 420px) {
		.choice-head { min-height: 52px; }
		.panel { padding: 0.25rem 0.625rem 0.75rem; }
	}
	@media (prefers-reduced-motion: reduce) {
		.choice, .choice::before, .choice-head :global(.chev) { transition: none; }
		.panel { animation: none; }
	}
</style>
