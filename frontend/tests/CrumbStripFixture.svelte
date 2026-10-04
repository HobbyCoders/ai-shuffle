<script lang="ts">
	import StudioTopBar from '../src/lib/components/layout/StudioTopBar.svelte';

	// The strip as the desktop window draws it: a sidebar beside the main panel,
	// whose top bar carries a subagent transcript opened from the assistant
	// (Sessions > Assistant > title) and the chat's action cluster on the right.
	const params = new URLSearchParams(location.search);
	const title = params.get('title') || 'Agents page header review';
	const actionsWidth = Number(params.get('actions') || 330);
</script>

<div class="window">
	<aside class="sidebar" aria-hidden="true"></aside>
	<main class="main-panel">
		<StudioTopBar projectLabel="Sessions" {title} sessionId="child" onToggleSidebar={() => {}}>
			{#snippet actions()}
				<div class="fake-actions" style:width="{actionsWidth}px"></div>
			{/snippet}
		</StudioTopBar>
	</main>
</div>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.window { display: flex; width: 100vw; }
	.sidebar { flex: none; width: 260px; }
	.main-panel { flex: 1; min-width: 0; }
	.fake-actions { height: 28px; }
</style>
