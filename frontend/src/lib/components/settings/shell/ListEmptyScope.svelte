<script lang="ts">
	/** List empty states are a quiet line; creation belongs to the header/detail. */
	import { setContext, type Snippet } from 'svelte';
	let { children }: { children?: Snippet } = $props();
	setContext('view-panel-empty', true);
</script>
{@render children?.()}
