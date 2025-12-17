<script lang="ts">
	/**
	 * DeckLayout - The main layout wrapper for "The Deck" UI
	 *
	 * Provides:
	 * - Activity Rail (left, 64px) - Vertical icon navigation
	 * - Main Deck Area (center, fluid) - Where cards stack/split
	 * - Context Panel (right, 320px, collapsible) - Contextual sidebar
	 * - Dock (bottom, 56px) - Running processes and quick actions
	 */

	import type { Snippet } from 'svelte';
	import type { ActivityMode, ActivityBadges, DeckSession, DeckAgent, DeckGeneration, SessionInfo } from './types';
	import ActivityRail from './ActivityRail.svelte';
	import ContextPanel from './ContextPanel.svelte';
	import Dock from './Dock.svelte';

	interface Props {
		/** Currently active mode */
		activeMode?: ActivityMode;
		/** Badge configuration for activity rail */
		badges?: ActivityBadges;
		/** Whether the context panel is collapsed */
		contextCollapsed?: boolean;
		/** Chat sessions for context panel */
		sessions?: DeckSession[];
		/** Running agents */
		agents?: DeckAgent[];
		/** Media generations */
		generations?: DeckGeneration[];
		/** Current session info */
		currentSession?: SessionInfo | null;
		/** Main content slot */
		children?: Snippet;
		/** Called when mode changes */
		onModeChange?: (mode: ActivityMode) => void;
		/** Called when settings is clicked */
		onSettingsClick?: () => void;
		/** Called when context panel collapse state changes */
		onContextCollapsedChange?: (collapsed: boolean) => void;
		/** Called when a session is clicked */
		onSessionClick?: (session: DeckSession) => void;
		/** Called when an agent is clicked */
		onAgentClick?: (agent: DeckAgent) => void;
		/** Called when a generation is clicked */
		onGenerationClick?: (generation: DeckGeneration) => void;
		/** Called when new chat button is clicked */
		onNewChat?: () => void;
		/** Called when new agent button is clicked */
		onNewAgent?: () => void;
		/** Called when new create button is clicked */
		onNewCreate?: () => void;
	}

	let {
		activeMode = 'chat',
		badges = {},
		contextCollapsed = false,
		sessions = [],
		agents = [],
		generations = [],
		currentSession = null,
		children,
		onModeChange,
		onSettingsClick,
		onContextCollapsedChange,
		onSessionClick,
		onAgentClick,
		onGenerationClick,
		onNewChat,
		onNewAgent,
		onNewCreate
	}: Props = $props();

	// Track local collapsed state if not controlled
	let localContextCollapsed = $state(contextCollapsed);

	// Sync with prop
	$effect(() => {
		localContextCollapsed = contextCollapsed;
	});

	function handleContextCollapsedChange(collapsed: boolean) {
		localContextCollapsed = collapsed;
		onContextCollapsedChange?.(collapsed);
	}

	// Mobile detection
	let isMobile = $state(false);

	// Check for mobile on mount and resize
	$effect(() => {
		function checkMobile() {
			isMobile = window.innerWidth < 640;
		}
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});
</script>

<div
	class="deck-layout h-screen overflow-hidden"
	class:context-collapsed={localContextCollapsed}
	class:mobile={isMobile}
	role="application"
	aria-label="AI Hub - The Deck"
>
	<!-- Activity Rail -->
	<div class="deck-rail" class:mobile-hidden={isMobile}>
		<ActivityRail
			{activeMode}
			{badges}
			{onModeChange}
			{onSettingsClick}
		/>
	</div>

	<!-- Main Content Area -->
	<main class="deck-main overflow-hidden" aria-label="Main content">
		{#if children}
			{@render children()}
		{:else}
			<!-- Default empty state -->
			<div class="h-full flex items-center justify-center">
				<div class="text-center">
					<div
						class="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-4"
					>
						<svg
							class="w-8 h-8 text-primary"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M13 10V3L4 14h7v7l9-11h-7z"
							/>
						</svg>
					</div>
					<h2 class="text-lg font-semibold text-foreground mb-1">Welcome to The Deck</h2>
					<p class="text-sm text-muted-foreground">Select an activity to get started</p>
				</div>
			</div>
		{/if}
	</main>

	<!-- Context Panel -->
	<div class="deck-context" class:mobile-overlay={isMobile}>
		<ContextPanel
			collapsed={localContextCollapsed}
			{sessions}
			{agents}
			{generations}
			{currentSession}
			onCollapsedChange={handleContextCollapsedChange}
			{onSessionClick}
			{onAgentClick}
			{onGenerationClick}
		/>
	</div>

	<!-- Dock -->
	<div class="deck-dock">
		<Dock
			{agents}
			{generations}
			{onAgentClick}
			{onGenerationClick}
			{onNewChat}
			{onNewAgent}
			{onNewCreate}
		/>
	</div>

	<!-- Mobile Activity Rail (bottom) -->
	{#if isMobile}
		<div class="mobile-rail">
			<ActivityRail
				{activeMode}
				{badges}
				{onModeChange}
				{onSettingsClick}
			/>
		</div>
	{/if}
</div>

<style>
	.deck-layout {
		display: grid;
		grid-template-columns: 64px 1fr 320px;
		grid-template-rows: 1fr 56px;
		grid-template-areas:
			'rail main context'
			'rail dock dock';
		background-color: var(--background);
	}

	/* Collapsed context panel state */
	.deck-layout.context-collapsed {
		grid-template-columns: 64px 1fr 0;
	}

	.deck-rail {
		grid-area: rail;
		z-index: 30;
	}

	.deck-main {
		grid-area: main;
		min-width: 0;
		min-height: 0;
		background-color: var(--background);
	}

	.deck-context {
		grid-area: context;
		z-index: 20;
		overflow: hidden;
	}

	.deck-dock {
		grid-area: dock;
		z-index: 30;
	}

	/* Mobile layout */
	.deck-layout.mobile {
		grid-template-columns: 1fr;
		grid-template-rows: 1fr 56px 64px;
		grid-template-areas:
			'main'
			'dock'
			'rail';
	}

	.deck-layout.mobile.context-collapsed {
		grid-template-columns: 1fr;
	}

	.mobile-hidden {
		display: none;
	}

	.mobile-rail {
		grid-area: rail;
		z-index: 30;
	}

	/* Mobile context panel overlay */
	.mobile-overlay {
		position: fixed;
		right: 0;
		top: 0;
		bottom: 120px; /* Above dock + mobile rail */
		z-index: 40;
	}

	/* Smooth transitions */
	.deck-layout {
		transition: grid-template-columns 300ms cubic-bezier(0.4, 0, 0.2, 1);
	}

	/* Medium screens: collapse context by default */
	@media (max-width: 1024px) and (min-width: 641px) {
		.deck-layout:not(.context-collapsed) {
			grid-template-columns: 64px 1fr 280px;
		}
	}

	/* Ensure proper stacking */
	.deck-rail,
	.deck-dock,
	.mobile-rail {
		position: relative;
	}
</style>
