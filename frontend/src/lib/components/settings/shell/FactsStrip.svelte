<script lang="ts" module>
	export interface Fact {
		label: string;
		/** Facts with no value are left out. */
		value: string | number | null | undefined;
		mono?: boolean;
		/** Muted, may wrap (a sentence rather than a value). */
		muted?: boolean;
		/** Makes the fact a button (e.g. open the tab it summarises). */
		onclick?: () => void;
		/** Accessible name for a clickable fact; defaults to "label: value". */
		action?: string;
	}
</script>

<script lang="ts">
	/** FactsStrip: up to 4 label/value facts under a detail header (2x2 on the phone). */
	import { ChevronRight } from 'lucide-svelte';

	interface Props {
		facts: Fact[];
	}

	let { facts }: Props = $props();
	const shown = $derived(facts.filter((f) => f.value !== null && f.value !== undefined && f.value !== '').slice(0, 4));

	$effect(() => {
		if (import.meta.env.DEV && facts.length > 4) console.warn('[settings] FactsStrip shows at most 4 facts');
	});
</script>

{#if shown.length}
	<dl class="st-facts" style:--n={shown.length}>
		{#each shown as fact (fact.label)}
			{#if fact.onclick}
				<div class="st-fact link">
					<dt>
						<button type="button" class="hit" aria-label={fact.action ?? `${fact.label}: ${fact.value}`} onclick={fact.onclick}></button>
						{fact.label}<span class="go" aria-hidden="true"><ChevronRight /></span>
					</dt>
					<dd class:st-mono={fact.mono} class:mute={fact.muted} title={String(fact.value)}>{fact.value}</dd>
				</div>
			{:else}
				<div class="st-fact">
					<dt>{fact.label}</dt>
					<dd class:st-mono={fact.mono} class:mute={fact.muted} title={String(fact.value)}>{fact.value}</dd>
				</div>
			{/if}
		{/each}
	</dl>
{/if}

<style>
	.st-facts {
		display: grid;
		grid-template-columns: repeat(var(--n, 4), minmax(0, 1fr));
		border-top: 1px solid var(--st-border);
		border-bottom: 1px solid var(--st-border);
		margin: 0;
	}
	.st-fact {
		position: relative;
		padding: 10px 12px 11px;
		border-left: 1px solid var(--st-border);
		min-width: 0;
	}
	.st-fact:first-child {
		border-left: 0;
		padding-left: 0;
	}
	dt {
		font-size: 12px;
		color: var(--st-mfg);
		display: flex;
		align-items: center;
		gap: 3px;
	}
	dd {
		margin: 2px 0 0;
		font-size: 13.5px;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	dd.st-mono {
		font-weight: 400;
		font-size: 12px;
	}
	dd.mute {
		font-weight: 400;
		color: var(--st-mfg);
		white-space: normal;
		font-size: 12.5px;
		line-height: 1.3;
	}
	/* Clickable fact: the whole cell is the hit target. */
	.hit {
		position: absolute;
		inset: 0;
		border: 0;
		padding: 0;
		background: none;
		border-radius: var(--st-radius-sm);
	}
	.hit:focus-visible {
		outline-offset: -2px;
	}
	.go {
		display: inline-flex;
		opacity: 0;
		transition: opacity 0.12s;
	}
	.go :global(svg.lucide) {
		width: 12px;
		height: 12px;
	}
	.st-fact.link:hover dd {
		color: var(--st-primary);
	}
	.st-fact.link:hover .go,
	.st-fact.link:has(.hit:focus-visible) .go {
		opacity: 1;
	}
</style>
