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
	import ContextPanel from './ContextPanel.svelte';
	import Dock from './Dock.svelte';
	import type {
		ActivityMode,
		ActivityBadges,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		SessionInfo,
		MinimizedCard,
		RunningProcess
	} from './types';
	import type { HistorySession } from './ContextPanel.svelte';

	interface Props {
		activeMode?: ActivityMode;
		badges?: ActivityBadges;
		contextCollapsed?: boolean;
		sessions?: DeckSession[];
		recentSessions?: HistorySession[];
		agents?: DeckAgent[];
		generations?: DeckGeneration[];
		currentSession?: SessionInfo | null;
		minimizedCards?: MinimizedCard[];
		runningProcesses?: RunningProcess[];
		onModeChange?: (mode: ActivityMode) => void;
		onLogoClick?: () => void;
		onSettingsClick?: () => void;
		onContextToggle?: () => void;
		onSessionClick?: (session: DeckSession) => void;
		onHistorySessionClick?: (session: HistorySession) => void;
		onAgentClick?: (agent: DeckAgent) => void;
		onGenerationClick?: (generation: DeckGeneration) => void;
		onMinimizedCardClick?: (card: MinimizedCard) => void;
		onProcessClick?: (process: RunningProcess) => void;
		children?: import('svelte').Snippet;
	}

	let {
		activeMode = 'workspace',
		badges = {},
		contextCollapsed = false,
		sessions = [],
		recentSessions = [],
		agents = [],
		generations = [],
		currentSession = null,
		minimizedCards = [],
		runningProcesses = [],
		onModeChange,
		onLogoClick,
		onSettingsClick,
		onContextToggle,
		onSessionClick,
		onHistorySessionClick,
		onAgentClick,
		onGenerationClick,
		onMinimizedCardClick,
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

<div class="deck-layout" class:mobile={isMobile} class:context-collapsed={localContextCollapsed}>
	<!-- Activity Rail - Desktop: left side, Mobile: bottom -->
	{#if !isMobile}
		<aside class="rail-container">
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
	<div class="main-area">
		<!-- Workspace slot -->
		<main class="workspace-container">
			{#if children}
				{@render children()}
			{:else}
				<div class="workspace-placeholder">
					<p>Workspace content goes here</p>
				</div>
			{/if}
		</main>

		<!-- Dock - Desktop only -->
		{#if !isMobile}
			<div class="dock-container">
				<Dock
					{minimizedCards}
					{runningProcesses}
					onCardClick={onMinimizedCardClick}
					{onProcessClick}
				/>
			</div>
		{/if}
	</div>

	<!-- Context Panel - Hidden on mobile -->
	{#if !isMobile}
		<aside class="context-container">
			<ContextPanel
				collapsed={localContextCollapsed}
				{sessions}
				{recentSessions}
				{agents}
				{generations}
				sessionInfo={currentSession}
				onToggleCollapse={handleContextToggle}
				{onSessionClick}
				{onHistorySessionClick}
				{onAgentClick}
				{onGenerationClick}
			/>
		</aside>

		<!-- Expand button - shown when context panel is collapsed -->
		{#if localContextCollapsed}
			<button
				class="context-expand-toggle"
				onclick={handleContextToggle}
				title="Expand panel"
			>
				<ChevronLeft size={16} strokeWidth={2} />
			</button>
		{/if}
	{/if}

	<!-- Mobile Rail - Bottom of screen -->
	{#if isMobile}
		<footer class="mobile-rail-container">
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
		grid-template-columns: 64px 1fr 320px;
		grid-template-rows: 1fr;
		width: 100%;
		height: 100vh;
		background: var(--background);
		overflow: hidden;
	}

	.deck-layout.context-collapsed {
		grid-template-columns: 64px 1fr 0;
	}

	.deck-layout.mobile {
		grid-template-columns: 1fr;
		grid-template-rows: 1fr 64px;
	}

	.rail-container {
		grid-column: 1;
		grid-row: 1;
		z-index: 50;
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

	.dock-container {
		flex-shrink: 0;
	}

	.context-container {
		grid-column: 3;
		grid-row: 1;
		z-index: 40;
		transition: width 0.2s ease;
	}

	.context-collapsed .context-container {
		width: 0;
		overflow: visible;
	}

	.mobile-rail-container {
		grid-column: 1;
		grid-row: 2;
		z-index: 50;
	}

	/* Ensure proper layering */
	:global(.deck-layout > *) {
		min-width: 0;
		min-height: 0;
	}

	/* Expand toggle button - shown when context panel is collapsed */
	.context-expand-toggle {
		position: fixed;
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
		z-index: 60;
	}

	.context-expand-toggle:hover {
		color: var(--foreground);
		background: var(--accent);
	}
</style>
