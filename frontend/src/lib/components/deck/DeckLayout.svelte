<script lang="ts">
	/**
	 * DeckLayout - Main layout component for The Deck
	 *
	 * Provides the core grid structure with:
	 * - Activity rail (64px) on the left
	 * - Workspace area (1fr) in the center
	 * - Context panel (320px) on the right (collapsible)
	 * - Dock (56px) at the bottom (desktop only)
	 *
	 * On mobile (<640px), the rail moves to the bottom and dock is hidden.
	 */

	import { onMount } from 'svelte';
	import { ChevronLeft } from 'lucide-svelte';
	import ActivityRail from './ActivityRail.svelte';
	import ActivityPanel from './ActivityPanel.svelte';
	import type {
		ActivityMode,
		ActivityBadges,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		SessionInfo,
		RunningProcess
	} from './types';
	import type { ActiveSession, HistorySession, ActivityTabType, OverlayType } from './ActivityPanel.svelte';

	interface Props {
		activeMode?: ActivityMode;
		badges?: ActivityBadges;
		contextCollapsed?: boolean;
		// Activity Panel props
		activeTab?: ActivityTabType;
		activeSessions?: ActiveSession[];
		recentSessions?: HistorySession[];
		runningAgents?: DeckAgent[];
		completedAgents?: DeckAgent[];
		generations?: DeckGeneration[];
		currentSession?: SessionInfo | null;
		// Overlay props
		overlayType?: OverlayType;
		overlayData?: Record<string, unknown>;
		// Legacy props (for backwards compat)
		sessions?: DeckSession[];
		agents?: DeckAgent[];
		runningProcesses?: RunningProcess[];
		// Callbacks
		onModeChange?: (mode: ActivityMode) => void;
		onLogoClick?: () => void;
		onSettingsClick?: () => void;
		onContextToggle?: () => void;
		onTabChange?: (tab: ActivityTabType) => void;
		onSessionClick?: (session: ActiveSession) => void;
		onHistorySessionClick?: (session: HistorySession) => void;
		onAgentClick?: (agent: DeckAgent) => void;
		onGenerationClick?: (generation: DeckGeneration) => void;
		onOverlayClose?: () => void;
		onProcessClick?: (process: RunningProcess) => void;
		children?: import('svelte').Snippet;
	}

	let {
		activeMode = 'workspace',
		badges = {},
		contextCollapsed = false,
		// Activity Panel
		activeTab = 'threads',
		activeSessions = [],
		recentSessions = [],
		runningAgents = [],
		completedAgents = [],
		generations = [],
		currentSession = null,
		// Overlay
		overlayType = null,
		overlayData = {},
		// Legacy
		sessions = [],
		agents = [],
		runningProcesses = [],
		// Callbacks
		onModeChange,
		onLogoClick,
		onSettingsClick,
		onContextToggle,
		onTabChange,
		onSessionClick,
		onHistorySessionClick,
		onAgentClick,
		onGenerationClick,
		onOverlayClose,
		onProcessClick,
		children
	}: Props = $props();

	// Track mobile state
	let isMobile = $state(false);
	let localContextCollapsed = $state(contextCollapsed);

	// Sync external collapsed state
	$effect(() => {
		localContextCollapsed = contextCollapsed;
	});

	function checkMobile() {
		isMobile = window.innerWidth < 640;
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});

	function handleContextToggle() {
		localContextCollapsed = !localContextCollapsed;
		onContextToggle?.();
	}
</script>

<div class="deck-layout deck-layout-responsive" class:mobile={isMobile} class:context-collapsed={localContextCollapsed}>
	<!-- Activity Rail - Desktop: left side, Mobile: bottom -->
	<!-- CSS classes provide fallback if JS is delayed -->
	{#if !isMobile}
		<aside class="rail-container rail-desktop mobile-hidden" data-layout="rail-desktop">
			<ActivityRail
				{activeMode}
				{badges}
				{isMobile}
				{onModeChange}
				{onLogoClick}
				{onSettingsClick}
			/>
		</aside>
	{/if}

	<!-- Main content area -->
	<div class="main-area main-content-responsive">
		<!-- Workspace slot - contains cards and the context panel overlay -->
		<main class="workspace-container">
			{#if children}
				{@render children()}
			{:else}
				<div class="workspace-placeholder">
					<p>Workspace content goes here</p>
				</div>
			{/if}

			<!-- Activity Panel - Inside workspace, overlays cards -->
			<!-- CSS classes hide on mobile as fallback -->
			{#if !isMobile}
				<aside class="context-container context-panel-desktop context-panel-responsive mobile-hidden" class:collapsed={localContextCollapsed} data-layout="context-panel">
					<ActivityPanel
						collapsed={localContextCollapsed}
						{activeTab}
						{activeSessions}
						{recentSessions}
						{runningAgents}
						{completedAgents}
						{generations}
						sessionInfo={currentSession}
						{overlayType}
						{overlayData}
						onToggleCollapse={handleContextToggle}
						{onTabChange}
						{onSessionClick}
						{onHistorySessionClick}
						{onAgentClick}
						{onGenerationClick}
						{onOverlayClose}
					/>
				</aside>

				<!-- Expand button - shown when context panel is collapsed -->
				{#if localContextCollapsed}
					<button
						class="context-expand-toggle mobile-hidden"
						onclick={handleContextToggle}
						title="Expand panel"
					>
						<ChevronLeft size={16} strokeWidth={2} />
					</button>
				{/if}
			{/if}
		</main>
	</div>

	<!-- Mobile Rail - Bottom of screen -->
	<!-- CSS classes show on mobile as fallback -->
	{#if isMobile}
		<footer class="mobile-rail-container rail-mobile desktop-hidden" data-layout="rail-mobile">
			<ActivityRail
				{activeMode}
				{badges}
				{isMobile}
				{onModeChange}
				{onLogoClick}
				{onSettingsClick}
			/>
		</footer>
	{/if}
</div>

<style>
	.deck-layout {
		display: grid;
		grid-template-columns: 64px 1fr;
		grid-template-rows: 1fr;
		width: 100%;
		height: 100vh;
		height: 100dvh; /* Dynamic viewport height for mobile browsers */
		background: var(--background);
		overflow: hidden;
	}

	.deck-layout.mobile {
		grid-template-columns: 1fr;
		grid-template-rows: 1fr calc(64px + env(safe-area-inset-bottom, 0px));
	}

	/* CSS fallback: If JS hasn't loaded yet, use media queries to set mobile layout */
	@media (max-width: 639px) {
		.deck-layout:not(.mobile) {
			grid-template-columns: 1fr;
			grid-template-rows: 1fr calc(64px + env(safe-area-inset-bottom, 0px));
		}
	}

	.rail-container {
		grid-column: 1;
		grid-row: 1;
		z-index: 50;
	}

	/* CSS fallback: Hide desktop rail on mobile even if JS hasn't set .mobile class */
	@media (max-width: 639px) {
		.rail-container {
			display: none;
		}
	}

	.main-area {
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-width: 0;
		min-height: 0;
		height: 100%;
	}

	.deck-layout:not(.mobile) .main-area {
		grid-column: 2;
		grid-row: 1;
	}

	.mobile .main-area {
		grid-column: 1;
		grid-row: 1;
		overflow: hidden;
	}

	/* CSS fallback: Main area takes full width on mobile */
	@media (max-width: 639px) {
		.main-area {
			grid-column: 1;
			grid-row: 1;
		}
	}

	.workspace-container {
		flex: 1;
		overflow: hidden;
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}

	.workspace-placeholder {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}

	/* Context panel - positioned inside workspace as an overlay */
	/* z-index: 10000 ensures it's always above maximized cards which use dynamic z-index values */
	.context-container {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		width: 320px;
		z-index: 10000;
		transition: transform 0.2s ease, opacity 0.2s ease;
		pointer-events: auto;
	}

	.context-container.collapsed {
		transform: translateX(100%);
		opacity: 0;
		pointer-events: none;
	}

	/* CSS fallback: Hide context panel on mobile */
	@media (max-width: 639px) {
		.context-container {
			display: none;
		}
	}

	/* Tablet: Narrower context panel */
	@media (min-width: 640px) and (max-width: 1023px) {
		.context-container {
			width: 280px;
		}
	}

	.mobile-rail-container {
		grid-column: 1;
		grid-row: 2;
		z-index: 100;
		position: relative;
		background: var(--card);
		height: calc(64px + env(safe-area-inset-bottom, 0px));
		min-height: calc(64px + env(safe-area-inset-bottom, 0px));
		padding-bottom: env(safe-area-inset-bottom, 0px);
		flex-shrink: 0;
	}

	/* CSS fallback: Hide mobile rail on desktop */
	@media (min-width: 640px) {
		.mobile-rail-container {
			display: none;
		}
	}

	/* Ensure proper layering */
	:global(.deck-layout > *) {
		min-width: 0;
		min-height: 0;
	}

	/* Expand toggle button - shown when context panel is collapsed */
	.context-expand-toggle {
		position: absolute;
		right: 0;
		top: 50%;
		transform: translateY(-50%);
		width: 24px;
		height: 48px;
		background: var(--card);
		border: 1px solid var(--border);
		border-right: none;
		border-radius: 6px 0 0 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
		z-index: 10001;
	}

	.context-expand-toggle:hover {
		color: var(--foreground);
		background: var(--accent);
	}

	/* CSS fallback: Hide expand toggle on mobile */
	@media (max-width: 639px) {
		.context-expand-toggle {
			display: none;
		}
	}

	/* Accessibility: Reduced motion support */
	@media (prefers-reduced-motion: reduce) {
		.context-container {
			transition: none;
		}

		.context-expand-toggle {
			transition: none;
		}
	}

	/* Tablet: Adjust grid columns */
	@media (min-width: 640px) and (max-width: 1023px) {
		.deck-layout {
			grid-template-columns: 56px 1fr;
		}
	}
</style>
