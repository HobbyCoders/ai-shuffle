<script lang="ts">
	/** SearchBox: the filter field above a model list (same look as the collection filter). */
	import { Search, X } from 'lucide-svelte';

	interface Props {
		value: string;
		placeholder: string;
		label?: string;
	}

	let { value = $bindable(''), placeholder, label }: Props = $props();
</script>

<label class="st-msearch">
	<Search />
	<span class="st-vh">{label ?? placeholder}</span>
	<input type="search" bind:value {placeholder} autocomplete="off" />
	{#if value}
		<button type="button" class="clear" aria-label="Clear search" onclick={() => (value = '')}><X /></button>
	{/if}
</label>

<style>
	.st-msearch {
		display: flex;
		align-items: center;
		gap: 8px;
		height: 32px;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		padding: 0 4px 0 10px;
		color: var(--st-mfg);
		min-width: 0;
	}
	.st-msearch :global(svg.lucide) {
		width: 15px;
		height: 15px;
	}
	input {
		flex: 1;
		border: 0;
		background: none;
		outline: none;
		font-size: 13px;
		min-width: 0;
		color: var(--st-fg);
	}
	input::-webkit-search-cancel-button {
		-webkit-appearance: none;
	}
	input::placeholder {
		color: var(--st-subtle);
	}
	.st-msearch:focus-within {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.clear {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		border: 0;
		background: none;
		border-radius: var(--st-radius-sm);
		color: var(--st-mfg);
		padding: 0;
	}
	.clear:hover {
		color: var(--st-fg);
		background: var(--st-hover);
	}
</style>
