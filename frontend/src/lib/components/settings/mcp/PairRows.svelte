<script lang="ts">
	/**
	 * PairRows: a titled group of key/value rows (MCP headers, environment
	 * variables). Values are masked until shown, since they are often tokens.
	 */
	import { Eye, EyeOff, Plus, X } from 'lucide-svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import type { Pair } from './mcp-model.svelte';

	interface Props {
		title: string;
		pairs: Pair[];
		addLabel: string;
		keyPlaceholder: string;
		valuePlaceholder?: string;
		/** Shown when the list is empty. */
		empty: string;
		disabled?: boolean;
	}

	let {
		title,
		pairs = $bindable(),
		addLabel,
		keyPlaceholder,
		valuePlaceholder = 'Value',
		empty,
		disabled = false
	}: Props = $props();

	let shown = $state(false);

	function add() {
		pairs = [...pairs, { key: '', value: '' }];
	}

	function remove(index: number) {
		pairs = pairs.filter((_, i) => i !== index);
	}
</script>

<RowGroup title={title} count={pairs.length || undefined}>
	{#snippet actions()}
		{#if pairs.length}
			<button
				type="button"
				class="st-btn ghost sm icon"
				aria-label={shown ? `Hide ${title.toLowerCase()} values` : `Show ${title.toLowerCase()} values`}
				aria-pressed={shown}
				onclick={() => (shown = !shown)}
			>
				{#if shown}<EyeOff />{:else}<Eye />{/if}
			</button>
		{/if}
		<button type="button" class="st-btn ghost sm" {disabled} onclick={add}><Plus />{addLabel}</button>
	{/snippet}
	{#if pairs.length === 0}
		<div class="st-row pr-empty">{empty}</div>
	{:else}
		{#each pairs as pair, i (i)}
			<div class="st-row pr">
				<input
					class="pr-in st-mono"
					aria-label="{title} name {i + 1}"
					placeholder={keyPlaceholder}
					autocomplete="off"
					spellcheck="false"
					{disabled}
					bind:value={pair.key}
				/>
				<input
					class="pr-in st-mono"
					type={shown ? 'text' : 'password'}
					aria-label="{title} value {i + 1}"
					placeholder={valuePlaceholder}
					autocomplete="off"
					spellcheck="false"
					{disabled}
					bind:value={pair.value}
				/>
				<button
					type="button"
					class="st-btn ghost sm icon"
					aria-label="Remove {pair.key || `row ${i + 1}`}"
					{disabled}
					onclick={() => remove(i)}
				>
					<X />
				</button>
			</div>
		{/each}
	{/if}
</RowGroup>

<style>
	.pr {
		display: grid;
		grid-template-columns: minmax(0, 2fr) minmax(0, 3fr) auto;
		gap: 8px;
		align-items: center;
		padding: 8px 12px 8px 16px;
	}
	.pr-empty {
		padding: 14px 16px;
		font-size: 13px;
		color: var(--st-subtle);
	}
	.pr-in {
		height: 32px;
		min-width: 0;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
		font-size: 12px;
	}
	.pr-in::placeholder {
		color: var(--st-subtle);
	}
	.pr-in:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.pr-in:disabled {
		opacity: 0.55;
	}
	@media (max-width: 480px) {
		.pr {
			grid-template-columns: minmax(0, 1fr) auto;
		}
		.pr .pr-in:nth-child(2) {
			grid-row: 2;
		}
	}
</style>
