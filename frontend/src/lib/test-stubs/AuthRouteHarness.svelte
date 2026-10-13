<script lang="ts">
	import { page } from '$app/stores';
	import Layout from '../../routes/+layout.svelte';
	import Home from '../../routes/+page.svelte';
	import Pair from '../../routes/pair/+page.svelte';

	const path = $derived<string>($page.url.pathname);
</script>

<Layout>
	{#if path === '/setup'}
		<p>Setup fixture</p>
	{:else if path === '/pair'}
		<Pair />
	{:else}
		<Home />
	{/if}
</Layout>
