<script lang="ts">
	/**
	 * DeckLayout - Main layout component for The Deck
	 *
	 * Provides a clean full-screen workspace with:
	 * - Floating dealer button for card creation
	 * - Context panel (320px) on the right (collapsible)
	 * - All navigation via cards (no sidebar)
	 *
	 * On mobile (<640px), dealer button moves to bottom center.
	 */

	import { onMount } from 'svelte';
	import { ChevronLeft, Plus } from 'lucide-svelte';
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
		isAdmin?: boolean;
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
		isAdmin = false,
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
	<!-- Main content area - full width now -->
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

			<!-- Floating Dealer Button -->
			<button
				class="dealer-button"
				class:mobile={isMobile}
				onclick={() => onLogoClick?.()}
				title="Deal New Card"
			>
				<div class="dealer-button-disc">
					<Plus size={20} strokeWidth={2.5} class="dealer-icon" />
				</div>
				{#if !isMobile}
					<span class="tooltip">Deal New Card</span>
				{/if}
			</button>

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
</div>

<style>
	.deck-layout {
		display: grid;
		grid-template-columns: 1fr;
		grid-template-rows: 1fr;
		width: 100%;
		height: 100vh;
		height: 100dvh; /* Dynamic viewport height for mobile browsers */
		background: var(--background);
		overflow: hidden;
	}

	.main-area {
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-width: 0;
		min-height: 0;
		height: 100%;
		grid-column: 1;
		grid-row: 1;
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

	/* ===================================
	   FLOATING DEALER BUTTON
	   =================================== */

	.dealer-button {
		position: absolute;
		bottom: 24px;
		left: 24px;
		z-index: 9999;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 56px;
		height: 56px;
		cursor: pointer;
		border: none;
		background: transparent;
		-webkit-tap-highlight-color: transparent;
	}

	.dealer-button-disc {
		position: relative;
		width: 52px;
		height: 52px;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Classic dealer button - white/cream plastic look */
		background:
			radial-gradient(ellipse 80% 50% at 50% 20%,
				rgba(255, 255, 255, 0.9) 0%,
				transparent 60%
			),
			linear-gradient(180deg,
				var(--dealer-white) 0%,
				var(--dealer-cream) 50%,
				var(--dealer-shadow) 100%
			);

		border-radius: 50%;

		/* Embossed edge effect */
		box-shadow:
			inset 0 2px 4px rgba(255, 255, 255, 0.8),
			inset 0 -3px 6px rgba(0, 0, 0, 0.15),
			0 4px 16px rgba(0, 0, 0, 0.4),
			0 2px 6px rgba(0, 0, 0, 0.3);

		transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
	}

	/* Gold ring border */
	.dealer-button-disc::before {
		content: '';
		position: absolute;
		inset: 4px;
		border: 2px solid var(--gold);
		border-radius: 50%;
		opacity: 0.6;
		transition: opacity 0.2s ease;
	}

	.dealer-button:hover .dealer-button-disc {
		transform: scale(1.08) translateY(-2px);
		box-shadow:
			inset 0 2px 4px rgba(255, 255, 255, 0.9),
			inset 0 -3px 6px rgba(0, 0, 0, 0.1),
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 4px 10px rgba(0, 0, 0, 0.3),
			0 0 24px var(--gold-glow);
	}

	.dealer-button:hover .dealer-button-disc::before {
		opacity: 0.9;
	}

	.dealer-button:active .dealer-button-disc {
		transform: scale(0.95);
		transition-duration: 0.1s;
	}

	.dealer-button :global(.dealer-icon) {
		color: var(--dealer-text);
		filter: drop-shadow(0 1px 0 rgba(255, 255, 255, 0.5));
		transition: transform 0.25s ease;
	}

	.dealer-button:hover :global(.dealer-icon) {
		transform: rotate(90deg);
	}

	/* Tooltip */
	.dealer-button .tooltip {
		position: absolute;
		left: calc(100% + 12px);
		top: 50%;
		transform: translateY(-50%) translateX(-4px);
		padding: 8px 14px;
		background: color-mix(in srgb, var(--felt) 92%, transparent);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.2s ease;
		pointer-events: none;
		box-shadow:
			inset 3px 0 0 var(--gold),
			0 4px 20px rgba(0, 0, 0, 0.3);
	}

	.dealer-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
		transform: translateY(-50%) translateX(0);
	}

	/* Mobile dealer button - bottom center */
	.dealer-button.mobile {
		bottom: calc(24px + env(safe-area-inset-bottom, 0px));
		left: 50%;
		transform: translateX(-50%);
		width: 52px;
		height: 52px;
	}

	.dealer-button.mobile .dealer-button-disc {
		width: 48px;
		height: 48px;
	}

	/* ===================================
	   CONTEXT PANEL
	   =================================== */

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

		.dealer-button-disc {
			transition: none;
		}
	}
</style>
