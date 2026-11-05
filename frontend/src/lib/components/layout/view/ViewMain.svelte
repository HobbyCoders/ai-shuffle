<script lang="ts">
	import type { Snippet } from 'svelte';
	let { width = 'reading', children }: { width?: 'reading' | 'data'; children: Snippet } = $props();
</script>
<div class="view-main st-scroll" data-width={width}>
	<div class="view-main-inner">{@render children()}</div>
</div>
<style>
	.view-main { flex: 1; min-height: 0; min-width: 0; overflow: auto; }
	.view-main-inner { display: flex; flex-direction: column; width: 100%; min-height: 100%; padding: var(--st-page-top) var(--st-gutter); margin: 0 auto; }
	.view-main-inner > :global(.st-empty-detail) { max-width: none; padding-inline: 0; }
	.view-main[data-width='reading'] .view-main-inner { max-width: calc(var(--st-reading-width) + 2 * var(--st-gutter)); }
	.view-main[data-width='data'] .view-main-inner { max-width: calc(var(--st-data-width) + 2 * var(--st-gutter)); }
	@media (max-width: 600px) { .view-main-inner, .view-main[data-width='data'] .view-main-inner { padding: 0 var(--st-gutter) 16px; } }
</style>
