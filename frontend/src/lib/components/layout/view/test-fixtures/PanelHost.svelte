<script lang="ts">
	import { viewPanel } from '$lib/stores/view-panel.svelte';
	const token = Symbol('test-host');
	$effect(() => { viewPanel.mountHost(token); return () => viewPanel.unmountHost(token); });
</script>
<!-- No page context: this component is a sibling of, not a child of, the page. -->
<aside data-test-host>{#if viewPanel.panel}{@render viewPanel.panel()}{/if}</aside>
