<script lang="ts">
	/** ListGroupLabel: a quiet group heading inside a collection list ("Workers", "Not connected"). */
	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		/** Optional trailing content (a count, a small action). */
		children?: Snippet;
	}

	let { label, children }: Props = $props();
</script>

<div class="st-lg" role="presentation">
	<span>{label}</span>
	{#if children}<span class="act">{@render children()}</span>{/if}
</div>

<style>
	.st-lg {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 12px;
		color: var(--st-subtle);
		padding: 16px 12px 6px;
	}
	.act {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
</style>
