<script lang="ts">
	import { page } from '$app/stores';
	import Layout from '../../routes/+layout.svelte';
	import Home from '../../routes/+page.svelte';
	import Login from '../../routes/login/+page.svelte';
	import Pair from '../../routes/pair/+page.svelte';
</script>

<Layout>
	{#if $page.url.pathname === '/login'}
		<Login />
	{:else if $page.url.pathname === '/setup'}
		<p>Setup fixture</p>
	{:else if $page.url.pathname === '/pair'}
		<Pair />
	{:else}
		<Home />
	{/if}
</Layout>
