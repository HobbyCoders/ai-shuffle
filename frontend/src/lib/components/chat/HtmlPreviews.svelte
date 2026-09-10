<script lang="ts">
	import { extractHtmlPreviews } from '$lib/utils/htmlPreview';
	import HtmlPreview from './HtmlPreview.svelte';
	let { content, streaming = false }: { content: string; streaming?: boolean } = $props();
	const previews = $derived(extractHtmlPreviews(content, !streaming));
</script>

{#if previews.length}
	<div class="html-previews" aria-label="HTML previews">
		{#each previews as preview (preview.href)}
			<HtmlPreview href={preview.href} filename={preview.filename} />
		{/each}
	</div>
{/if}

<style>
	.html-previews { display: grid; gap: 1rem; margin-top: 1rem; min-width: 0; }
</style>
