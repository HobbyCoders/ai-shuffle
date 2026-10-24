<script lang="ts">
	/**
	 * GenerationChoiceDetail: the remembered choice for one generation kind
	 * (account preference `generation.choice.<kind>`). The agent uses it when a
	 * request names no source or model; naming one in chat overrides it for
	 * that request. Pick another with the same card the chat shows, or clear it
	 * so the agent asks next time.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { generationChoices } from '$lib/stores/generation-choices.svelte';
	import {
		KIND_LABEL,
		formatValue,
		modelLabel,
		sameChoice,
		sourceLabel,
		type GenerationChoice,
		type GenerationKind,
		type GenerationOption
	} from '$lib/utils/generation-choice';
	import GenerationChoiceCard from '$lib/components/chat/GenerationChoiceCard.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { GENERATION_ICONS, GENERATION_NAMES } from './icons';

	let { kind }: { kind: GenerationKind } = $props();

	const label = $derived(KIND_LABEL[kind]);
	const remembered = $derived(generationChoices.remembered[kind]);
	const last = $derived(generationChoices.last[kind]);
	const lastDiffers = $derived(!!last && !sameChoice(last, remembered));

	let options = $state<GenerationOption[] | null>(null);
	let loadError = $state('');
	let loadingOptions = $state(false);
	let busy = $state(false);
	let actionError = $state('');
	/**
	 * The server's save-time warning for "Remember" (the card shows its own).
	 * Any later save or forget replaces it, and it hides once another pick is
	 * remembered elsewhere.
	 */
	let saveWarning = $state<{ choice: GenerationChoice; text: string } | null>(null);
	const rememberWarning = $derived(saveWarning && sameChoice(remembered, saveWarning.choice) ? saveWarning.text : '');
	let generation = 0;

	async function loadOptions() {
		const mine = ++generation;
		loadingOptions = true;
		loadError = '';
		try {
			const list = await generationChoices.options(kind);
			if (mine === generation) options = list;
		} catch (e) {
			if (mine === generation) loadError = getErrorMessage(e, 'Could not list the sources.');
		} finally {
			if (mine === generation) loadingOptions = false;
		}
	}

	onMount(() => {
		void generationChoices.load(true);
		void loadOptions();
	});

	async function forget() {
		busy = true;
		actionError = '';
		saveWarning = null;
		try {
			await generationChoices.forget(kind);
		} catch (e) {
			actionError = getErrorMessage(e, 'Could not clear the saved choice.');
		} finally {
			busy = false;
		}
	}

	async function rememberLast() {
		if (!last) return;
		busy = true;
		actionError = '';
		saveWarning = null;
		try {
			const saved = await generationChoices.save(kind, last, true, 'settings');
			saveWarning = saved?.warning ? { choice: saved.choice ?? last, text: saved.warning } : null;
		} catch (e) {
			actionError = getErrorMessage(e, 'Could not save this choice.');
		} finally {
			busy = false;
		}
	}

	function describe(options: Record<string, unknown> | undefined): string {
		const entries = Object.entries(options ?? {}).filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object');
		if (!entries.length) return 'Model defaults';
		return entries
			.slice(0, 4)
			.map(([k, v]) => `${k.replace(/_/g, ' ')} ${formatValue(v)}`)
			.join(' · ');
	}

	const facts = $derived<Fact[]>([
		{ label: 'Source', value: remembered ? sourceLabel(remembered.source) : null },
		{ label: 'Model', value: remembered ? remembered.model : null, mono: true },
		{ label: 'Worker', value: remembered?.worker ?? null, mono: true }
	]);
</script>

{#snippet primary()}
	{#if remembered}
		<button type="button" class="st-btn" disabled={busy} onclick={forget}>Ask every time</button>
	{/if}
{/snippet}

{#snippet banner()}
	{#if remembered?.origin === 'migrated'}
		<InlineBanner tone="info">
			Brought over from your earlier Default models setting. It keeps working as before; pick another
			source below to change it.
		</InlineBanner>
	{/if}
	{#if actionError}
		<InlineBanner tone="warn">{actionError}</InlineBanner>
	{/if}
	{#if rememberWarning}
		<InlineBanner tone="warn">Remembered. {rememberWarning}</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name={GENERATION_NAMES[kind]}
	icon={GENERATION_ICONS[kind]}
	status={remembered ? 'ok' : 'off'}
	statusText={remembered ? 'Remembered' : 'Asks each time'}
	badge="Generation"
	subtitle="Used when you don't name a source"
	{primary}
	{banner}
	{facts}
	category="models"
	item="gen-{kind}"
>
	<RowGroup title="Remembered choice">
		{#if remembered}
			<SettingRow label="{sourceLabel(remembered.source)} · {modelLabel(remembered.model)}" hint={describe(remembered.options)}>
				<ValueText>In use</ValueText>
			</SettingRow>
		{:else}
			<SettingRow label="None" hint="The agent shows a choice card">
				<ValueText>Asks each time</ValueText>
			</SettingRow>
		{/if}
		{#if lastDiffers && last}
			<SettingRow label="Last pick: {sourceLabel(last.source)} · {modelLabel(last.model)}" hint={describe(last.options)}>
				<button type="button" class="st-btn sm" disabled={busy} onclick={rememberLast}>Remember</button>
			</SettingRow>
		{/if}
	</RowGroup>

	<RowGroup title="Pick a source">
		{#snippet actions()}
			<button type="button" class="st-btn sm" disabled={loadingOptions} onclick={loadOptions} aria-label="Refresh sources">
				<RefreshCw class={loadingOptions ? 'spin' : ''} />Refresh
			</button>
		{/snippet}
		<div class="gcd-picker">
			{#if loadError}
				<p class="gcd-msg">{loadError}</p>
			{:else if options === null}
				<div class="gcd-skeleton" aria-busy="true" aria-label="Loading sources">
					<span></span><span></span><span></span>
				</div>
			{:else if !options.length}
				<p class="gcd-msg">
					No account can make {label.plural} yet. Connect fal.ai, a ChatGPT plan or a SuperGrok plan in Providers.
				</p>
			{:else}
				{#key options}
					<GenerationChoiceCard {kind} {options} variant="settings" onsaved={() => (saveWarning = null)} />
				{/key}
			{/if}
			<p class="gcd-foot">
				In chat, name a source or model ("use my SuperGrok plan", "Flux Pro, 16:9") to override this for one request.
			</p>
		</div>
	</RowGroup>
</DetailPane>

<style>
	.gcd-picker {
		display: grid;
		gap: 10px;
		padding: 12px;
	}
	.gcd-msg,
	.gcd-foot {
		margin: 0;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--st-mfg, var(--muted-foreground));
	}
	.gcd-foot {
		font-size: 12px;
	}
	.gcd-skeleton {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
		gap: 8px;
	}
	.gcd-skeleton span {
		height: 96px;
		border-radius: 8px;
		background: linear-gradient(90deg, var(--muted), color-mix(in oklch, var(--muted) 40%, transparent), var(--muted));
		background-size: 200% 100%;
		animation: gcd-shimmer 1.2s ease-in-out infinite;
	}
	@keyframes gcd-shimmer {
		to {
			background-position: -200% 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.gcd-skeleton span {
			animation: none;
		}
	}
</style>
