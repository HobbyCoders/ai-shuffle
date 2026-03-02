<script lang="ts">
	/**
	 * AppLayout - Codex-style two-column layout
	 *
	 * Replaces DeckLayout. Provides:
	 * - Collapsible left sidebar (SidePanel)
	 * - Full-width main content area (MainPanel)
	 * - Responsive: sidebar overlays on mobile/tablet
	 * - Resizable sidebar with drag handle
	 */
	import { onMount, onDestroy } from 'svelte';
	import { layout, sidebarCollapsed, sidebarOverlay, sidebarWidth, MIN_SIDEBAR_WIDTH, MAX_SIDEBAR_WIDTH } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import SidePanel from './SidePanel.svelte';
	import MainPanel from './MainPanel.svelte';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenPlugins?: () => void;
	}

	let {
		isAdmin = false,
		onNewSession,
		onOpenSession,
		onOpenFeature,
		onOpenPlugins
	}: Props = $props();

	let isMobile = $state(false);
	let shortcutReg: ShortcutRegistration | null = null;
	let isResizing = $state(false);

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
	// on rapid/multi-touch interactions
	let resizeAbort: AbortController | null = null;

	function startResize(e: PointerEvent) {
		if ($sidebarOverlay || $sidebarCollapsed) return;
		e.preventDefault();

		// Clean up any previous resize listeners (prevents accumulation)
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

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);

		// Register Cmd+B to toggle sidebar
		shortcutReg = registerShortcut({
			id: 'toggle-sidebar',
			description: 'Toggle sidebar',
			key: 'b',
			cmdOrCtrl: true,
			category: 'navigation',
			action: () => layout.toggleSidebar()
		});
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
		}
		shortcutReg?.unregister();
	});

	// Close overlay sidebar when clicking main panel on mobile
	function handleMainPanelClick() {
		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
		}
	}
</script>

<div
	class="app-layout"
	class:mobile={isMobile}
	class:sidebar-overlay={$sidebarOverlay}
	class:sidebar-collapsed={$sidebarCollapsed}
	class:resizing={isResizing}
	style:--sidebar-width="{$sidebarWidth}px"
>
	<!-- Sidebar -->
	<SidePanel
		{isAdmin}
		{onNewSession}
		{onOpenSession}
		{onOpenFeature}
	/>

	<!-- Resize handle (desktop only, when sidebar is open) -->
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

	<!-- Overlay backdrop for mobile sidebar -->
	{#if $sidebarOverlay && !$sidebarCollapsed}
		<button
			class="sidebar-backdrop"
			onclick={handleMainPanelClick}
			tabindex="-1"
			aria-label="Close sidebar"
		></button>
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

<style>
	.app-layout {
		display: flex;
		width: 100%;
		height: 100vh;
		height: 100dvh;
		background: var(--background);
		overflow: hidden;
		perspective: 1200px;
	}

	/* Disable text selection and pointer events on iframes while resizing */
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
		border-bottom-left-radius: 12px;
		box-shadow: -2px 0 16px -4px rgba(0,0,0,0.06);
		transition: border-radius 350ms cubic-bezier(0.16,1,0.3,1),
			box-shadow 350ms cubic-bezier(0.16,1,0.3,1);
	}

	/* Remove rounded left edge when sidebar is collapsed or in mobile overlay */
	.app-layout.sidebar-collapsed .main-content,
	.app-layout.sidebar-overlay .main-content {
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
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
		/* Extend the clickable area without taking visual space */
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

	/* Mobile: sidebar overlays on top */
	.app-layout.sidebar-overlay :global(.side-panel) {
		position: fixed;
		left: 0;
		top: 0;
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

	/* Mobile: safe area insets for notched devices */
	@media (max-width: 639px) {
		.app-layout.sidebar-overlay :global(.side-panel) {
			padding-left: env(safe-area-inset-left, 0px);
			padding-right: env(safe-area-inset-right, 0px);
			padding-top: env(safe-area-inset-top, 0px);
		}
	}
</style>
