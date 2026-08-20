<script lang="ts">
	/**
	 * AppLayout - app shell (Studio Noir)
	 *
	 * Desktop (>1080px): a 256px sidebar on the black `--sidebar` plane holds the
	 * session navigator and the page rail; the main panel carries its own top bar
	 * (StudioTopBar, rendered by MainPanel so it can host the existing git
	 * actions). The sidebar collapses from that top bar and its collapsed state
	 * persists via the layout store.
	 *
	 * Mobile (≤1080px): unchanged pattern — AppTopBar's compact bar plus the
	 * slide-in drawer, which now hosts the very same Sidebar component.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { layout, sidebarCollapsed } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import MainPanel from './MainPanel.svelte';
	import AppTopBar from './AppTopBar.svelte';
	import Sidebar from './Sidebar.svelte';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSessions: () => void;
		onOpenPlugins?: () => void;
		onOpenSpotlight?: () => void;
	}

	let {
		isAdmin = false,
		onNewSession,
		onNewSessionForProject,
		onOpenSession,
		onOpenFeature,
		onOpenSessions,
		onOpenPlugins,
		onOpenSpotlight
	}: Props = $props();

	let isMobile = $state(false);

	// Mobile is width-based (not pointer-gated) so the mobile UI is reachable by
	// simply narrowing a desktop window — important for testing on desktop. The
	// 1080px cutoff also covers unfolded foldables whose inner screens report
	// ~1028px (Galaxy Z Fold 6) / ~1040px (Z Fold 7) CSS width in their wide
	// orientation; a pointer:coarse gate at 1024 would have missed those.
	// Shared with the slide-out panels via isPanelFullscreenWidth, and mirrored
	// by the `max-width: 1080px` media queries in the headers.
	function checkMobile() {
		isMobile = isPanelFullscreenWidth(window.innerWidth);
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
		}
	});

	const showSidebar = $derived(!isMobile && !$sidebarCollapsed);
</script>

<div class="app-shell" class:mobile={isMobile}>
	{#if isMobile}
		<AppTopBar
			{isAdmin}
			{onNewSession}
			{onOpenSession}
			{onOpenSessions}
			{onOpenFeature}
			{onOpenSpotlight}
		/>
	{/if}

	<div class="app-body">
		{#if showSidebar}
			<div class="sidebar-slot">
				<Sidebar
					{isAdmin}
					{onNewSession}
					{onOpenSession}
					{onOpenFeature}
					{onOpenSessions}
					{onOpenSpotlight}
				/>
			</div>
		{/if}

		<MainPanel
			{onOpenSession}
			{onOpenPlugins}
			{onNewSession}
			{onNewSessionForProject}
			{onOpenSessions}
			{isMobile}
			sidebarCollapsed={$sidebarCollapsed}
			onToggleSidebar={() => layout.toggleSidebar()}
		/>
	</div>
</div>

<style>
	.app-shell {
		display: flex;
		flex-direction: column;
		width: 100%;
		height: 100vh;
		height: 100dvh;
		background: var(--background);
		overflow: hidden;
	}

	.app-body {
		flex: 1;
		min-height: 0;
		min-width: 0;
		display: flex;
		overflow: hidden;
	}

	.sidebar-slot {
		width: 256px;
		flex: none;
		min-height: 0;
		border-right: 1px solid var(--sidebar-border);
	}
</style>
