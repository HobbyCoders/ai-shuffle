<script lang="ts">
	/** Centered collection detail, using the Agents reading column without page-owned kit overrides. */
	import type { ComponentProps } from 'svelte';
	import DetailPane from '../../settings/shell/DetailPane.svelte';
	let props: ComponentProps<typeof DetailPane> = $props();
</script>

<div class="view-detail"><DetailPane {...props} /></div>

<style>
	.view-detail { min-width: 0; min-height: 100%; }
	.view-detail :global(.st-dwrap) { width: 100%; max-width: 740px; margin-inline: auto; }
	.view-detail :global(.st-dhn h2) { font-size: 24px; }
</style>
