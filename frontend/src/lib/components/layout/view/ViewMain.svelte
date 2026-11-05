<script lang="ts">
	import type { Snippet } from 'svelte';
	let { width = 'reading', children }: { width?: 'reading' | 'data'; children: Snippet } = $props();
</script>
<div class="view-main st-scroll" data-width={width}>
	<div class="view-main-inner">{@render children()}</div>
</div>
<style>
	.view-main { flex: 1; min-height: 0; min-width: 0; overflow: auto; }
	.view-main-inner { width: 100%; min-height: 100%; padding: 48px; margin: 0 auto; }
	.view-main[data-width='reading'] .view-main-inner { max-width: 856px; }
	.view-main[data-width='data'] .view-main-inner { padding: 32px; }
	@media (max-width: 600px) { .view-main-inner, .view-main[data-width='data'] .view-main-inner { padding: 16px; } }
</style>
