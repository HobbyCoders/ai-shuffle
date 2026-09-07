<script lang="ts">
	/**
	 * AppLayout - app shell (Studio Noir)
	 *
	 * Two shells, chosen by PLATFORM (see `$lib/stores/platform.svelte`), never
	 * by window width:
	 *
	 * Desktop (every web browser): a 256px sidebar on the black `--sidebar`
	 * plane holds the session navigator and the page rail; the main panel
	 * carries its own top bar (StudioTopBar, rendered by MainPanel so it can
	 * host the existing git actions). The sidebar collapses from that top bar
	 * and its collapsed state persists via the layout store. In a narrow window
	 * (≤1080px) the same shell stays, but the sidebar becomes an overlay that
	 * starts collapsed, so the main panel keeps the full width.
	 *
	 * Mobile (the Android app's WebView, or `?ui=mobile`): AppTopBar's compact
	 * bar plus the slide-in drawer, which hosts the very same Sidebar component.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { layout, sidebarCollapsed } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { isNarrowViewportWidth } from '$lib/utils/viewport';
	import MainPanel from './MainPanel.svelte';
	import AppTopBar from './AppTopBar.svelte';
	import Sidebar from './Sidebar.svelte';
	import CredentialStorageBanner from '../CredentialStorageBanner.svelte';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onSettledOpenSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenPlugins?: () => void;
		onOpenSpotlight?: () => void;
	}

	let {
		isAdmin = false,
		onNewSession,
		onSettledOpenSession,
		onNewSessionForProject,
		onOpenSession,
		onOpenFeature,
		onOpenPlugins,
		onOpenSpotlight
	}: Props = $props();

	const isMobile = $derived(platform.isMobile);
	/** Desktop shell only: window is too narrow for a docked sidebar. */
	let narrow = $state(false);

	// --- resizable sidebar ---------------------------------------------------
	const WIDTH_MIN = 220;
	const WIDTH_MAX = 400;
	const WIDTH_DEFAULT = 256;
	let sidebarWidth = $state(layout.current.sidebarWidth);
	let resizing = $state(false);
	$effect(() => { if (!resizing) sidebarWidth = $layout.sidebarWidth; });
	function persistWidth() { layout.setSidebarWidth(sidebarWidth); }

	function startResize(e: PointerEvent) {
		e.preventDefault();
		resizing = true;
		const startX = e.clientX;
		const startWidth = sidebarWidth;

		const move = (ev: PointerEvent) => {
			sidebarWidth = Math.min(WIDTH_MAX, Math.max(WIDTH_MIN, startWidth + ev.clientX - startX));
		};
		const up = () => {
			resizing = false;
			persistWidth();
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', up);
		};
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', up);
	}

	function resetWidth() {
		sidebarWidth = WIDTH_DEFAULT;
		persistWidth();
	}

	// The sidebar is an overlay (collapsed by default, not persisted) on the
	// mobile shell always, and on the desktop shell only while the window is
	// narrow. To try the mobile shell in a desktop browser, load `?ui=mobile`.
	let overlayApplied: boolean | null = null;
	function syncOverlay() {
		narrow = isNarrowViewportWidth(window.innerWidth);
		const overlay = isMobile || narrow;
		if (overlay === overlayApplied) return;
		overlayApplied = overlay;
		layout.setSidebarOverlay(overlay);
	}

	onMount(() => {
		syncOverlay();
		window.addEventListener('resize', syncOverlay);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', syncOverlay);
		}
	});

	const showSidebar = $derived(!isMobile && !$sidebarCollapsed);
	/** Desktop shell, narrow window: the open sidebar floats over the main panel. */
	const sidebarFloating = $derived(showSidebar && narrow);

	function closeFloatingSidebar() {
		if (sidebarFloating) layout.setSidebarCollapsed(true);
	}
	// Navigating from a floating sidebar dismisses it, like the mobile drawer.
	const sidebarNav = $derived({
		onNewSession: () => { onNewSession(); closeFloatingSidebar(); },
		onSettledOpenSession: () => { onSettledOpenSession(); closeFloatingSidebar(); },
		onOpenSession: (id: string) => { onOpenSession(id); closeFloatingSidebar(); },
		onOpenFeature: (type: ViewType) => { onOpenFeature(type); closeFloatingSidebar(); },
		onOpenSpotlight: onOpenSpotlight ? () => { onOpenSpotlight?.(); closeFloatingSidebar(); } : undefined
	});
</script>

<div class="app-shell" class:mobile={isMobile} class:narrow={!isMobile && narrow}>
	<CredentialStorageBanner />
	{#if isMobile}
		<AppTopBar
			{isAdmin}
			{onNewSession}
			{onSettledOpenSession}
			{onOpenSession}
			{onOpenFeature}
			{onOpenSpotlight}
		/>
	{/if}

	<div class="app-body">
		{#if sidebarFloating}
			<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
			<div class="sidebar-scrim" onclick={closeFloatingSidebar}></div>
		{/if}
		{#if showSidebar}
			<div class="sidebar-slot" class:floating={sidebarFloating} style:width="{sidebarWidth}px">
				<Sidebar
					{isAdmin}
					onNewSession={sidebarNav.onNewSession}
					onSettledOpenSession={sidebarNav.onSettledOpenSession}
					onOpenSession={sidebarNav.onOpenSession}
					onOpenFeature={sidebarNav.onOpenFeature}
					onOpenSpotlight={sidebarNav.onOpenSpotlight}
				/>
				{#if !sidebarFloating}
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<div
						class="resize-handle"
						class:active={resizing}
						role="separator"
						aria-orientation="vertical"
						aria-label="Resize sidebar"
						title="Drag to resize · double-click to reset"
						onpointerdown={startResize}
						ondblclick={resetWidth}
					></div>
				{/if}
			</div>
		{/if}

		<MainPanel
			{onOpenSession}
			{onOpenPlugins}
			{onNewSession}
			{onNewSessionForProject}
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

	.sidebar-slot {
		position: relative;
		flex: none;
		min-height: 0;
		border-right: 1px solid var(--sidebar-border);
	}

	/* Desktop shell in a narrow window: the sidebar floats over the main panel
	   instead of squeezing it, and never grows past the viewport. */
	.sidebar-slot.floating {
		position: absolute;
		inset: 0 auto 0 0;
		z-index: 40;
		max-width: min(100%, 100vw);
		background: var(--sidebar);
		box-shadow: 8px 0 32px color-mix(in oklch, black 45%, transparent);
	}

	.sidebar-scrim {
		position: absolute;
		inset: 0;
		z-index: 39;
		background: color-mix(in oklch, black 45%, transparent);
	}

	/* Drag handle on the sidebar's right edge. Slightly overhangs the border so
	   the hit target is comfortable without stealing clicks from the cards. */
	.resize-handle {
		position: absolute;
		top: 0;
		right: -3px;
		width: 6px;
		height: 100%;
		cursor: col-resize;
		z-index: 20;
		touch-action: none;
	}
	.resize-handle:hover,
	.resize-handle.active {
		background: color-mix(in oklch, var(--primary) 35%, transparent);
	}
</style>
