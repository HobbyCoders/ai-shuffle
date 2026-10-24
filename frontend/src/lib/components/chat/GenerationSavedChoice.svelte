<script lang="ts">
	/**
	 * GenerationSavedChoice: one line under a generation tool row that ran the
	 * account's remembered choice without asking, with the two ways to change
	 * it: ask again next time, or open Settings → Default models.
	 */
	import { Bookmark } from 'lucide-svelte';
	import { openSettings } from '$lib/settings/navigation.svelte';
	import { generationChoices } from '$lib/stores/generation-choices.svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { KIND_LABEL, modelLabel, priceView, sourceLabel, type RememberedResult } from '$lib/utils/generation-choice';

	let { saved }: { saved: RememberedResult } = $props();

	let phase = $state<'idle' | 'busy' | 'forgotten' | 'error'>('idle');
	let error = $state('');
	const label = $derived(KIND_LABEL[saved.kind]);
	const price = $derived(priceView(saved.price, saved.kind));

	async function askNextTime() {
		phase = 'busy';
		try {
			await generationChoices.forget(saved.kind);
			phase = 'forgotten';
		} catch (e) {
			error = getErrorMessage(e, 'Could not change the saved choice.');
			phase = 'error';
		}
	}
</script>

<p class="gsc" role="note">
	<Bookmark class="gsc-icon" aria-hidden="true" />
	<span class="gsc-text">
		{#if phase === 'forgotten'}
			I'll ask which source to use for the next {label.noun}.
		{:else}
			Used your saved choice for {label.plural}:
			<strong>{sourceLabel(saved.choice.source)} · {modelLabel(saved.choice.model)}</strong>
			{#if price.priced}<span class="gsc-price">{price.figure}</span>{/if}
		{/if}
		{#if phase === 'error'}<span class="gsc-error">{error}</span>{/if}
	</span>
	{#if phase !== 'forgotten'}
		<span class="gsc-actions">
			<button type="button" onclick={askNextTime} disabled={phase === 'busy'}>Ask me next time</button>
			<button type="button" onclick={() => openSettings({ category: 'models', item: `gen-${saved.kind}` })}>Change</button>
		</span>
	{/if}
</p>

<style>
	/* A note under its tool row: indented to the row's name, the row's muted
	   type, and the question tray's quiet text actions. */
	.gsc {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 2px 10px;
		margin: 0 0 6px;
		padding: 0 8px 0 31px;
		font-size: 0.6875rem;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.gsc :global(.gsc-icon) {
		flex: none;
		width: 12px;
		height: 12px;
		color: var(--subtle-foreground, var(--muted-foreground));
	}
	.gsc-text {
		flex: 1 1 220px;
		min-width: 0;
	}
	.gsc strong {
		color: var(--foreground);
		font-weight: 500;
	}
	.gsc-price {
		margin-left: 6px;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		color: var(--foreground);
	}
	.gsc-error {
		display: block;
		color: var(--foreground);
	}
	.gsc-actions {
		display: inline-flex;
		gap: 12px;
	}
	button {
		min-height: 28px;
		padding: 0;
		border: 0;
		background: none;
		color: var(--muted-foreground);
		font: inherit;
		font-weight: 500;
		cursor: pointer;
		transition: color 0.15s ease;
	}
	button:hover:not(:disabled) {
		color: var(--foreground);
	}
	button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	button:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 2px;
	}
	@media (max-width: 480px) {
		.gsc {
			padding-left: 8px;
		}
		button {
			min-height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		button {
			transition: none;
		}
	}
</style>
