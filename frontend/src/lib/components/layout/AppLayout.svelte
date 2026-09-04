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

	let isMobile = $state(false);

	// --- resizable sidebar ---------------------------------------------------
	const WIDTH_KEY = 'sidebar-width';
	const WIDTH_MIN = 220;
	const WIDTH_MAX = 400;
	const WIDTH_DEFAULT = 256;

	function loadWidth(): number {
		if (typeof localStorage === 'undefined') return WIDTH_DEFAULT;
		const stored = Number(localStorage.getItem(WIDTH_KEY));
		return Number.isFinite(stored) && stored >= WIDTH_MIN && stored <= WIDTH_MAX
			? stored
			: WIDTH_DEFAULT;
	}

	let sidebarWidth = $state(loadWidth());
	let resizing = $state(false);

	function persistWidth() {
		try {
			localStorage.setItem(WIDTH_KEY, String(sidebarWidth));
		} catch {
			// Storage blocked — the width still holds for this session.
		}
	}

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
			{onSettledOpenSession}
			{onOpenSession}
			{onOpenFeature}
			{onOpenSpotlight}
		/>
	{/if}

	<div class="app-body">
		{#if showSidebar}
			<div class="sidebar-slot" style:width="{sidebarWidth}px">
				<Sidebar
					{isAdmin}
					{onNewSession}
					{onSettledOpenSession}
					{onOpenSession}
					{onOpenFeature}
					{onOpenSpotlight}
				/>
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
