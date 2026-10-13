<script lang="ts">
	/**
	 * Main Page — the worker-served web UI, which is desktop-only. The page
	 * itself (data bootstrap, shortcuts, modals) is the shared AppPage; this
	 * route only picks the desktop shell. The mobile app (mobile/) renders the
	 * same AppPage with its own shell.
	 */
	import AppPage from '$lib/components/layout/AppPage.svelte';
	import AppLayout from '$lib/components/layout/AppLayout.svelte';
</script>

<AppPage>
	{#snippet shell(props)}
		<AppLayout {...props} />
	{/snippet}
</AppPage>
