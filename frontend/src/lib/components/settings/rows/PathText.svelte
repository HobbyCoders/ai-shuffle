<script lang="ts">
	/** Middle ellipsis: preserve the final segment without splitting a directory name. */
	let { path }: { path: string } = $props();
	const split = $derived(Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\')));
</script>
<span class="path-text" title={path} aria-label={path}>
	{#if split > 0}<span class="prefix" aria-hidden="true">{path.slice(0, split)}</span><span class="tail" aria-hidden="true">{path.slice(split)}</span>{:else}<span class="prefix" aria-hidden="true">{path}</span>{/if}
</span>
<style>
	.path-text { display: inline-flex; min-width: 0; max-width: 100%; white-space: nowrap; vertical-align: bottom; font-family: var(--font-mono); font-size: 12px; }
	.prefix { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.tail { flex: none; max-width: 85%; overflow: hidden; text-overflow: ellipsis; }
</style>
