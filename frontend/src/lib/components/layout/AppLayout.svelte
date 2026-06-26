<script lang="ts">
	/**
	 * AppLayout - Top bar + body layout
	 *
	 * - Global AppTopBar provides primary navigation (pages as siblings).
	 * - The Sessions section is the navigator: a full-canvas board (overview)
	 *   that drills into a focused chat. The old left sidebar is retired.
	 */
	import { onMount, onDestroy } from 'svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import MainPanel from './MainPanel.svelte';
	import AppTopBar from './AppTopBar.svelte';

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
	// Keep this in sync with the `max-width: 1080px` media queries in the headers
	// (AppTopBar / PageHeader / GitHeaderActions).
	const MOBILE_MAX_WIDTH = 1080;

	function checkMobile() {
		isMobile = window.innerWidth <= MOBILE_MAX_WIDTH;
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
</script>

<div class="app-shell">
	<AppTopBar
		{isAdmin}
		{isMobile}
		{onNewSession}
		{onOpenSessions}
		{onOpenFeature}
		{onOpenSpotlight}
	/>

	<div class="app-body">
		<MainPanel
			{onOpenSession}
			{onOpenPlugins}
			{onNewSession}
			{onNewSessionForProject}
			{isMobile}
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
</style>
