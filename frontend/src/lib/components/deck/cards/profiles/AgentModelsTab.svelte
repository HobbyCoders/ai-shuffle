<script lang="ts" module>
	import { decodeProviderModel, getModelLabel, getProviderDisplay, getProviders } from '$lib/constants/models';

	/** "Sonnet 5.5" for any stored model value (alias or prov:<id>:<model>). */
	export function modelLabel(value: string | null | undefined): string {
		return value ? getModelLabel(value) : '';
	}

	/** Which provider a stored model runs through ("Claude" for native aliases). */
	export function modelProvider(value: string | null | undefined): string {
		const decoded = decodeProviderModel(value);
		if (!decoded) return 'Claude';
		const provider = getProviders().find((p) => p.id === decoded.providerId);
		return provider ? getProviderDisplay(provider).name : decoded.providerId;
	}
</script>

<script lang="ts">
	/**
	 * The Models tab of an agent (issue #1827).
	 *
	 * With the subagent default available: a card with the one global default
	 * (Change, or Choose while none is set), the
	 * models the lead agent may choose from, then this agent's choice: use the
	 * default (no pinned model) or pin a model per provider. Without the
	 * endpoint (an older worker answers 404) only the per-provider pickers show,
	 * as before. Voice has its own card either way.
	 */
	import { X } from 'lucide-svelte';
	import type { SubagentDefault } from '$lib/api/subagentDefault';
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import InlineBanner from '$lib/components/settings/rows/InlineBanner.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import { NATIVE_PROVIDER_KEY, VOICE_PROVIDER_KEY, subagentPickerValue } from '$lib/constants/models';
	import { isPinned } from './agentList';

	interface Props {
		providerModels: Record<string, string>;
		onModelsChange: (next: Record<string, string>) => void;
		/** undefined while loading; null when this worker has no subagent default. */
		subagentDefault: SubagentDefault | null | undefined;
		/** A load or save failed (not a 404). */
		defaultError?: string;
		onDefaultModel: (model: string) => void | Promise<void>;
		onAllowed: (allowed: string[]) => void | Promise<void>;
	}

	let { providerModels, onModelsChange, subagentDefault, defaultError = '', onDefaultModel, onAllowed }: Props = $props();

	const providers = $derived(getProviders());
	const hasDefault = $derived(!!subagentDefault);
	const pinnedNow = $derived(isPinned(providerModels));
	/** "Pin" picked but nothing chosen yet; lasts while this agent is open. */
	let pinChoice = $state(false);
	/** Pins set aside by "Use the subagent default", so Pin brings them back. */
	let stash = $state<Record<string, string> | null>(null);
	const mode = $derived<'default' | 'pinned'>(pinnedNow || pinChoice ? 'pinned' : 'default');

	let changing = $state(false);

	function setProviderModel(key: string, model: string) {
		onModelsChange({ ...providerModels, [key]: model });
	}

	function chooseMode(next: 'default' | 'pinned') {
		if (next === mode) return;
		if (next === 'default') {
			const pins = Object.fromEntries(Object.entries(providerModels).filter(([k]) => k !== VOICE_PROVIDER_KEY));
			stash = isPinned(pins) ? pins : stash;
			pinChoice = false;
			const voice = providerModels[VOICE_PROVIDER_KEY];
			onModelsChange(voice ? { [VOICE_PROVIDER_KEY]: voice } : {});
		} else {
			pinChoice = true;
			if (stash) onModelsChange({ ...stash, ...providerModels });
		}
	}

	function modeKey(event: KeyboardEvent) {
		if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
		event.preventDefault();
		const next = mode === 'default' ? 'pinned' : 'default';
		chooseMode(next);
		const group = (event.currentTarget as HTMLElement).closest('[role="radiogroup"]');
		queueMicrotask(() => group?.querySelector<HTMLElement>(`[data-mode="${next}"]`)?.focus());
	}

	const allowed = $derived(subagentDefault?.allowed ?? []);

	function addAllowed(value: string) {
		if (!value || allowed.includes(value)) return;
		void onAllowed([...allowed, value]);
	}

	function removeAllowed(value: string) {
		void onAllowed(allowed.filter((m) => m !== value));
	}

	async function pickDefault(value: string) {
		if (!value) return;
		await onDefaultModel(value);
		changing = false;
	}
</script>

{#snippet pickers()}
	<SettingRow label="Claude" hint="Callers on native Claude.">
		<ModelPicker
			value={subagentPickerValue(NATIVE_PROVIDER_KEY, providerModels[NATIVE_PROVIDER_KEY])}
			providerScope={NATIVE_PROVIDER_KEY}
			allowEmpty
			emptyLabel="Not offered"
			allowCustom={false}
			onSelect={(value) => setProviderModel(NATIVE_PROVIDER_KEY, value)}
		/>
	</SettingRow>
	{#each providers as provider (provider.id)}
		{@const display = getProviderDisplay(provider)}
		<SettingRow label={display.name} hint={display.subtext || provider.name}>
			<ModelPicker
				value={subagentPickerValue(provider.id, providerModels[provider.id])}
				allowEmpty
				emptyLabel="Not offered"
				allowCustom={false}
				onSelect={(value) => setProviderModel(provider.id, value)}
			/>
		</SettingRow>
	{/each}
{/snippet}

{#if defaultError}
	<InlineBanner>{defaultError}</InlineBanner>
{/if}

{#if subagentDefault === undefined}
	<p class="st-note" role="status">Loading models…</p>
{:else if subagentDefault}
	{@const dm = subagentDefault.model}
	<RowGroup title="All agents">
		<SettingRow
			label="Subagent default"
			hint={dm ? 'Used by every agent.' : 'Unpinned agents need one.'}
			expanded={changing ? changeEditor : undefined}
		>
			{#if dm}
				<span class="valtx">{modelLabel(dm)}</span>
				<span class="chip">{modelProvider(dm)}</span>
			{:else}
				<span class="valtx none">No subagent default yet</span>
			{/if}
			<button
				type="button"
				class="st-btn sm"
				class:primary={!dm && !changing}
				aria-expanded={changing}
				onclick={() => (changing = !changing)}
			>
				{changing ? 'Done' : dm ? 'Change' : 'Choose'}
			</button>
		</SettingRow>
	</RowGroup>
	{#snippet changeEditor()}
		<div class="change">
			<ModelPicker value={dm ?? ''} allowCustom={false} startOnAll onSelect={(value) => void pickDefault(value)} />
		</div>
	{/snippet}

	<RowGroup title="Models agents may choose from" count={allowed.length}>
		{#each allowed as m (m)}
			<SettingRow label={modelLabel(m)} hint={modelProvider(m)}>
				{#if m === dm}<span class="chip prim">Default</span>{/if}
				<button type="button" class="st-btn ghost icon sm" aria-label={`Remove ${modelLabel(m)}`} title="Remove" onclick={() => removeAllowed(m)}><X /></button>
			</SettingRow>
		{/each}
		<SettingRow label="Add a model" hint={allowed.length ? 'From your model list.' : 'None yet.'}>
			<ModelPicker value="" allowEmpty emptyLabel="Pick a model" allowCustom={false} startOnAll onSelect={addAllowed} />
		</SettingRow>
	</RowGroup>
	<p class="st-note">The lead agent picks from these.</p>

	<RowGroup title="This agent">
		<div class="tiers" role="radiogroup" aria-label="Model for this agent">
			{#each ['default', 'pinned'] as const as id (id)}
				{@const on = mode === id}
				<button
					type="button"
					class="tier"
					role="radio"
					data-mode={id}
					aria-checked={on}
					tabindex={on ? 0 : -1}
					onclick={() => chooseMode(id)}
					onkeydown={modeKey}
				>
					<span class="rd" aria-hidden="true"></span>
					<span class="tx">
						{#if id === 'default'}
							<span class="t">Use the subagent default</span>
							<span class="h">No setup needed.</span>
							<span class="res">{dm ? `${modelLabel(dm)} · for all agents` : 'Choose a default first.'}</span>
						{:else}
							<span class="t">Pin a model for this agent</span>
							<span class="h">Advanced. Per provider.</span>
							<span class="res">Stays fixed until you change it.</span>
						{/if}
					</span>
				</button>
			{/each}
		</div>
		{#if mode === 'pinned'}
			<div class="pins">{@render pickers()}</div>
		{/if}
	</RowGroup>
	{#if mode === 'pinned'}
		<p class="st-note">Leave a provider empty to hide it there.</p>
	{/if}
{:else}
	<RowGroup title="Models">
		{@render pickers()}
	</RowGroup>
	<p class="st-note">Leave a provider empty to hide it there.</p>
{/if}

<RowGroup title="Voice calls">
	<!-- Voice is not a provider: a voice call is funded by an OpenAI Realtime
	     row, but the workers it dispatches can run on any leg. This picks that
	     leg and is what makes the agent dispatchable from a call at all. -->
	<SettingRow label="Voice" hint="When voice hands off.">
		<ModelPicker
			value={providerModels[VOICE_PROVIDER_KEY] || ''}
			allowEmpty
			emptyLabel="Not in voice calls"
			allowCustom={false}
			onSelect={(value) => setProviderModel(VOICE_PROVIDER_KEY, value)}
		/>
	</SettingRow>
</RowGroup>

<style>
	.valtx {
		font-size: 13px;
		font-weight: 500;
		white-space: nowrap;
	}
	.valtx.none {
		color: var(--st-warning);
	}
	.chip {
		flex: none;
		display: inline-flex;
		align-items: center;
		gap: 3px;
		padding: 1px 7px;
		border-radius: 999px;
		font-size: 10.5px;
		font-weight: 600;
		background: var(--st-muted);
		color: var(--st-mfg);
		line-height: 1.5;
	}
	.chip.prim {
		background: var(--st-primary-soft);
		color: var(--st-primary);
	}
	.change {
		display: flex;
		justify-content: flex-end;
	}
	.tiers > .tier + .tier {
		border-top: 1px solid var(--st-border);
	}
	.tier {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 12px;
		align-items: start;
		width: 100%;
		padding: 12px 16px;
		border: 0;
		background: none;
		text-align: left;
		color: inherit;
		transition: background 0.12s;
	}
	.tier:hover {
		background: var(--st-hover);
	}
	.tier:focus-visible {
		outline-offset: -2px;
	}
	.tier[aria-checked='true'] {
		background: color-mix(in oklch, var(--primary) 5%, transparent);
	}
	.rd {
		margin-top: 2px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		border: 1.5px solid var(--st-border-strong);
		flex: none;
	}
	.tier[aria-checked='true'] .rd {
		border: 5px solid var(--st-primary);
	}
	.tx {
		display: grid;
		gap: 1px;
		min-width: 0;
	}
	.t {
		font-size: 14px;
		font-weight: 450;
	}
	.h {
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.res {
		font-size: 11.5px;
		color: var(--st-subtle);
		margin-top: 3px;
	}
	.pins {
		border-top: 1px solid var(--st-border);
		background: color-mix(in oklch, var(--st-muted) 35%, transparent);
	}
	.pins > :global(.st-row + .st-row) {
		border-top: 1px solid var(--st-border);
	}
	.pins > :global(.st-row) {
		padding-left: 44px;
	}
	.st-note {
		margin: 8px 2px 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	@container (max-width: 560px) {
		.pins > :global(.st-row) {
			padding-left: 14px;
		}
		.tier {
			padding: 12px 14px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.tier {
			transition: none;
		}
	}
</style>
