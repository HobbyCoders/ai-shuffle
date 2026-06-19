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

	function checkMobile() {
		const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;
		const isReasonableMobileSize = window.innerWidth < 1024;
		isMobile = window.innerWidth < 640 || (isTouchDevice && isReasonableMobileSize);
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
		onToggleSidebar={onOpenSessions}
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
