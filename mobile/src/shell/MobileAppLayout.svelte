<script lang="ts">
	/**
	 * MobileAppLayout - the mobile app shell.
	 *
	 * AppTopBar's compact bar plus its slide-in drawer (which hosts the same
	 * Sidebar the desktop shell docks), over the shared MainPanel. MainPanel's
	 * header snippet renders AppTopBar with MainPanel's download / git actions:
	 * one header at every width, including an unfolded phone.
	 */
	import { onMount } from 'svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import type { AppShellProps } from '$lib/components/layout/app-shell';
	import MainPanel from '$lib/components/layout/MainPanel.svelte';
	import CredentialStorageBanner from '$lib/components/CredentialStorageBanner.svelte';
	import AppTopBar from './AppTopBar.svelte';

	let {
		isAdmin,
		onNewSession,
		onSettledOpenSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight
	}: AppShellProps = $props();

	// The drawer is always an overlay on the mobile shell (never persisted).
	onMount(() => {
		layout.setSidebarOverlay(true);
	});
</script>

<div class="app-shell mobile">
	<CredentialStorageBanner />
	<div class="app-body">
		<MainPanel {onOpenSession}>
			{#snippet header(ctx)}
				<AppTopBar
					{isAdmin}
					{onNewSession}
					{onSettledOpenSession}
					{onOpenSession}
					{onOpenFeature}
					{onOpenSpotlight}
					hideGlobalStt={ctx.isSessionView || ctx.embedded}
				>
					{#snippet actions()}
						{@render ctx.downloadAction()}
						{@render ctx.headerActions()}
					{/snippet}
				</AppTopBar>
			{/snippet}
		</MainPanel>
	</div>
</div>

<style>
	.app-shell {
		display: flex;
		flex-direction: column;
		width: 100%;
		height: 100vh;
		height: 100dvh;
		/* visualViewport.height — tracks the virtual keyboard, unlike dvh */
		height: var(--app-viewport-height, 100dvh);
		background: var(--background);
		overflow: hidden;
	}

	.app-body {
		position: relative;
		flex: 1;
		min-height: 0;
		min-width: 0;
		display: flex;
		overflow: hidden;
	}
</style>
