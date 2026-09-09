<script lang="ts">
	/**
	 * The mobile app's one page: the shared AppPage (data bootstrap,
	 * shortcuts, modals) wrapped in the mobile shell — compact app bar,
	 * slide-in drawer, PageHeader over the shared MainPanel.
	 */
	import AppPage from '$lib/components/layout/AppPage.svelte';
	import MobileAppLayout from '../shell/MobileAppLayout.svelte';
	// Last, so the mobile rules follow the shared component styles in the bundle.
	import '../mobile.css';
</script>

<AppPage>
	{#snippet shell(props)}
		<MobileAppLayout {...props} />
	{/snippet}
</AppPage>
