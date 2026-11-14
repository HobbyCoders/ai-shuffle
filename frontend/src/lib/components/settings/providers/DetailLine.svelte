<script lang="ts">
	/**
	 * A detail's subtitle ("Used by media tools · $2.50 · Checked 4m ago"), the
	 * parts joined as detailLine joins them. A `danger` part (a spent balance)
	 * reads in the destructive colour, as it does in the chat's usage popover.
	 */
	type Shown = string | number | { text: string; danger?: boolean };
	type Part = Shown | null | undefined | false;

	let { parts }: { parts: Part[] } = $props();
	const shown = $derived(parts.filter((p): p is Shown => p !== null && p !== undefined && p !== false && p !== ''));
</script>

<span
	>{#each shown as part, i}{#if i}{' · '}{/if}{#if typeof part === 'object'}<span class:danger={part.danger}>{part.text}</span
			>{:else}{part}{/if}{/each}</span
>

<style>
	.danger {
		color: var(--st-destructive);
	}
</style>
