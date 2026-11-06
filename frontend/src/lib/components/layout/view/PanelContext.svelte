<script lang="ts">
	/** Snippets execute under their render host, not their declaration's context. */
	import { setContext, untrack, type Snippet } from 'svelte';
	let { contexts, children }: { contexts: Map<unknown, unknown>; children: Snippet } = $props();
	for (const [key, value] of untrack(() => contexts)) setContext(key, value);
</script>

{@render children()}
