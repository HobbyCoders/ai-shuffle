<script lang="ts">
	/** Test double for $lib RootLayout: marks where the worker bootstrap would wrap the page. */
	import type { Snippet } from 'svelte';
	let { mobileApp = false, children }: { mobileApp?: boolean; children: Snippet } = $props();
</script>

<div data-testid="root-layout" data-mobile-app={String(mobileApp)}>{@render children()}</div>
