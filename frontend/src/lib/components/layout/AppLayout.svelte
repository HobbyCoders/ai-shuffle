<script lang="ts">
	/**
	 * AppLayout - Top bar + body layout
	 *
	 * - Global AppTopBar provides primary navigation (pages as siblings).
	 * - The session list (SidePanel) is contextual to the Sessions section
	 *   (shown only for chat views); feature pages render full-width.
	 * - Responsive: sidebar overlays on mobile/tablet.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { layout, activeView, sidebarCollapsed, sidebarOverlay, sidebarWidth, MIN_SIDEBAR_WIDTH, MAX_SIDEBAR_WIDTH } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import SidePanel from './SidePanel.svelte';
	import MainPanel from './MainPanel.svelte';
	import AppTopBar from './AppTopBar.svelte';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';

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
	let shortcutReg: ShortcutRegistration | null = null;
	let isResizing = $state(false);

	// The session sidebar belongs to the Sessions section only. Feature pages
	// (Files, Notes, Build group, Automations, …) render full-width.
	const inSessions = $derived(($activeView?.type ?? 'chat') === 'chat');
	const showSidebar = $derived(inSessions);

	function checkMobile() {
		const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;
		const isReasonableMobileSize = window.innerWidth < 1024;
		const wasMobile = isMobile;
		isMobile = window.innerWidth < 640 || (isTouchDevice && isReasonableMobileSize);

		// Set overlay mode for mobile/tablet
		layout.setSidebarOverlay(isMobile);

		// Auto-collapse sidebar on mobile
		if (isMobile && !wasMobile) {
			layout.setSidebarCollapsed(true);
		}
	}

	// Resize handling — use AbortController to prevent listener accumulation
	let resizeAbort: AbortController | null = null;

	function startResize(e: PointerEvent) {
		if ($sidebarOverlay || $sidebarCollapsed) return;
		e.preventDefault();

		resizeAbort?.abort();
		resizeAbort = new AbortController();

		isResizing = true;

		const onPointerMove = (ev: PointerEvent) => {
			const newWidth = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, ev.clientX));
			layout.setSidebarWidth(newWidth);
		};

		const onPointerUp = () => {
			isResizing = false;
			resizeAbort?.abort();
			resizeAbort = null;
		};

		document.addEventListener('pointermove', onPointerMove, { signal: resizeAbort.signal });
		document.addEventListener('pointerup', onPointerUp, { signal: resizeAbort.signal });
	}

	// Mobile hamburger / Sessions affordance: ensure we're in the Sessions
	// section and reveal the session drawer.
	function handleToggleSidebar() {
		if (!inSessions) {
			onOpenSessions();
			layout.setSidebarCollapsed(false);
			return;
		}
		layout.toggleSidebar();
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);

		shortcutReg = registerShortcut({
			id: 'toggle-sidebar',
			description: 'Toggle sidebar',
			key: 'b',
			cmdOrCtrl: true,
			category: 'navigation',
			action: () => handleToggleSidebar()
		});
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
		}
		shortcutReg?.unregister();
	});

	function handleMainPanelClick() {
		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
		}
	}
</script>

<div class="app-shell">
	<AppTopBar
		{isAdmin}
		{isMobile}
		{onNewSession}
		{onOpenSessions}
		{onOpenFeature}
		{onOpenSpotlight}
		onToggleSidebar={handleToggleSidebar}
	/>

	<div
		class="app-layout"
		class:mobile={isMobile}
		class:sidebar-overlay={$sidebarOverlay}
		class:sidebar-collapsed={$sidebarCollapsed}
		class:no-sidebar={!showSidebar}
		class:resizing={isResizing}
		style:--sidebar-width="{$sidebarWidth}px"
	>
		<!-- Sidebar (Sessions section only) -->
		{#if showSidebar}
			<SidePanel
				slim
				{isAdmin}
				{onNewSession}
				{onNewSessionForProject}
				{onOpenSession}
				{onOpenFeature}
			/>

			{#if !$sidebarOverlay && !$sidebarCollapsed}
				<div
					class="resize-handle"
					onpointerdown={startResize}
					role="separator"
					aria-orientation="vertical"
					aria-label="Resize sidebar"
					tabindex="-1"
				>
					<div class="resize-handle-bar"></div>
				</div>
			{/if}

			{#if $sidebarOverlay && !$sidebarCollapsed}
				<button
					class="sidebar-backdrop"
					onclick={handleMainPanelClick}
					tabindex="-1"
					aria-label="Close sidebar"
				></button>
			{/if}
		{/if}

		<!-- Main content -->
		<div class="main-content" role="main">
			<MainPanel
				onOpenSession={onOpenSession}
				onOpenPlugins={onOpenPlugins}
				{isMobile}
			/>
		</div>
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

	.app-layout {
		display: flex;
		flex: 1;
		min-height: 0;
		width: 100%;
		background: var(--background);
		overflow: hidden;
		perspective: 1200px;
	}

	.app-layout.resizing {
		user-select: none;
		-webkit-user-select: none;
	}

	.app-layout.resizing :global(iframe) {
		pointer-events: none;
	}

	.main-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
		height: 100%;
		background: var(--card);
		position: relative;
		z-index: 1;
		border-top-left-radius: 12px;
		box-shadow: -2px 0 16px -4px rgba(0,0,0,0.06);
		transition: border-radius 350ms cubic-bezier(0.16,1,0.3,1),
			box-shadow 350ms cubic-bezier(0.16,1,0.3,1);
	}

	/* Square the corner / drop the shadow when there's no docked sidebar */
	.app-layout.sidebar-collapsed .main-content,
	.app-layout.sidebar-overlay .main-content,
	.app-layout.no-sidebar .main-content {
		border-top-left-radius: 0;
		box-shadow: none;
	}

	/* Desktop: paper-peel 3D collapse animation */
	.app-layout:not(.sidebar-overlay) :global(.side-panel.collapsed) {
		transform: rotateY(-12deg) scale(0.97);
		opacity: 0;
	}

	/* Resize handle */
	.resize-handle {
		position: relative;
		width: 6px;
		flex-shrink: 0;
		cursor: col-resize;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: center;
		margin-left: -3px;
		margin-right: -3px;
	}

	.resize-handle-bar {
		width: 3px;
		height: 40px;
		border-radius: 3px;
		background: var(--border);
		opacity: 0;
		transition: opacity 200ms ease, background-color 200ms ease, height 200ms ease;
	}

	.resize-handle:hover .resize-handle-bar {
		opacity: 1;
		background: var(--primary);
		height: 48px;
	}

	.app-layout.resizing .resize-handle-bar {
		opacity: 1;
		background: var(--primary);
		height: 64px;
	}

	/* Mobile: sidebar overlays on top (below the 52px top bar) */
	.app-layout.sidebar-overlay :global(.side-panel) {
		position: fixed;
		left: 0;
		top: 52px;
		bottom: 0;
		z-index: 1000;
		box-shadow: var(--shadow-l);
		width: 100vw;
		width: 100dvw;
	}

	.app-layout.sidebar-overlay :global(.side-panel.collapsed) {
		transform: translateX(-100%);
	}

	.sidebar-backdrop {
		position: fixed;
		inset: 0;
		top: 52px;
		z-index: 999;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 50%, transparent);
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		border: none;
		padding: 0;
		cursor: default;
	}

	@media (max-width: 639px) {
		.app-layout.sidebar-overlay :global(.side-panel) {
			top: 48px;
			padding-left: env(safe-area-inset-left, 0px);
			padding-right: env(safe-area-inset-right, 0px);
		}
		.sidebar-backdrop { top: 48px; }
	}
</style>
