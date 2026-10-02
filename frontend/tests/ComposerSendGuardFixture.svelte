<script lang="ts">
	import AppPage from '../src/lib/components/layout/AppPage.svelte';
	import ChatView from '../src/lib/components/layout/ChatView.svelte';
</script>

<!-- The real app bootstrap (AppPage) around the real chat surface; only the
     sidebar chrome is left out. -->
<AppPage>
	{#snippet shell()}
		<main><ChatView /></main>
	{/snippet}
</AppPage>

<style>
	main {
		height: 100vh;
		display: flex;
		flex-direction: column;
	}
</style>
