<script lang="ts">
	/**
	 * CardShuffle - Layout mode selector for card arrangement
	 *
	 * A hover-activated UI that appears at the top of the workspace,
	 * allowing users to switch between different card layout modes:
	 * - Free Flow: Default drag-and-drop positioning
	 * - Side by Side: Cards arranged in vertical columns
	 * - Tile: Cards tiled in a grid pattern
	 * - Stack: Cards stacked in a cascade pattern
	 * - Focus: One main card with sidebar cards
	 *
	 * Hidden by default, only shows when mouse is near the top of the workspace.
	 * In Focus mode, also shows page navigation controls.
	 */

	import {
		Move,
		Columns,
		LayoutGrid,
		Layers,
		Maximize2,
		ChevronDown,
		ChevronLeft,
		ChevronRight
	} from 'lucide-svelte';
	import type { LayoutMode } from '$lib/stores/deck';

	interface Props {
		currentMode: LayoutMode;
		onModeChange: (mode: LayoutMode) => void;
		// Focus mode navigation props
		focusModeIndex?: number;
		focusModeTotal?: number;
		onFocusNavigate?: (direction: 'prev' | 'next') => void;
	}

	let {
		currentMode,
		onModeChange,
		focusModeIndex = 0,
		focusModeTotal = 0,
		onFocusNavigate
	}: Props = $props();

	// Derived state for focus nav visibility
	const showFocusNav = $derived(currentMode === 'focus' && focusModeTotal > 1);

	// Visibility state - trigger shows when mouse is near top of workspace
	let isVisible = $state(false);
	// Hover state for the panel
	let isHovered = $state(false);
	let isExpanded = $state(false);
	let hoverTimeout: ReturnType<typeof setTimeout> | null = null;
	let visibilityTimeout: ReturnType<typeof setTimeout> | null = null;

	const layoutModes: { mode: LayoutMode; label: string; description: string; icon: typeof Move }[] = [
		{
			mode: 'freeflow',
			label: 'Free Flow',
			description: 'Drag and drop cards anywhere',
			icon: Move
		},
		{
			mode: 'sidebyside',
			label: 'Side by Side',
			description: 'Cards in vertical columns',
			icon: Columns
		},
		{
			mode: 'tile',
			label: 'Tile',
			description: 'Equal-sized grid layout',
			icon: LayoutGrid
		},
		{
			mode: 'stack',
			label: 'Stack',
			description: 'Cascading card deck',
			icon: Layers
		},
		{
			mode: 'focus',
			label: 'Focus',
			description: 'One main card, others minimized',
			icon: Maximize2
		}
	];

	// Derive current mode info
	const currentModeInfo = $derived(layoutModes.find(m => m.mode === currentMode) || layoutModes[0]);

	// Handle mouse entering the trigger zone (near top of workspace)
	function handleTriggerZoneEnter() {
		if (visibilityTimeout) clearTimeout(visibilityTimeout);
		isVisible = true;
	}

	// Handle mouse leaving the trigger zone
	function handleTriggerZoneLeave() {
		if (visibilityTimeout) clearTimeout(visibilityTimeout);
		// Don't hide if expanded or hovered over the panel
		if (!isExpanded && !isHovered) {
			visibilityTimeout = setTimeout(() => {
				isVisible = false;
			}, 300);
		}
	}

	function handleMouseEnter() {
		if (hoverTimeout) clearTimeout(hoverTimeout);
		if (visibilityTimeout) clearTimeout(visibilityTimeout);
		isHovered = true;
		// Small delay before expanding to avoid accidental triggers
		hoverTimeout = setTimeout(() => {
			isExpanded = true;
		}, 150);
	}

	function handleMouseLeave() {
		if (hoverTimeout) clearTimeout(hoverTimeout);
		hoverTimeout = setTimeout(() => {
			isHovered = false;
			isExpanded = false;
			// Also trigger visibility check after leaving
			visibilityTimeout = setTimeout(() => {
				isVisible = false;
			}, 300);
		}, 200);
	}

	function handleModeSelect(mode: LayoutMode) {
		onModeChange(mode);
		// Keep panel open briefly to show selection
		setTimeout(() => {
			isExpanded = false;
			isHovered = false;
			isVisible = false;
		}, 300);
	}

	// Handle focus navigation button click - clear timeouts and navigate
	function handleFocusNavClick(e: MouseEvent, direction: 'prev' | 'next') {
		e.stopPropagation();
		// Clear any pending hide timeouts to prevent the panel from disappearing
		if (hoverTimeout) clearTimeout(hoverTimeout);
		if (visibilityTimeout) clearTimeout(visibilityTimeout);
		onFocusNavigate?.(direction);
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			isExpanded = false;
			isHovered = false;
			isVisible = false;
		}
	}
</script>

<svelte:window on:keydown={handleKeyDown} />

<div
	class="card-shuffle-container"
	role="group"
	aria-label="Card layout mode selector"
>
	<!-- Invisible trigger zone at top of workspace - always present to detect mouse -->
	<div
		class="trigger-zone"
		onmouseenter={handleTriggerZoneEnter}
		onmouseleave={handleTriggerZoneLeave}
	></div>

	<!-- Hover zone with the actual UI - only visible when triggered -->
	{#if isVisible || isHovered || isExpanded}
		<div
			class="hover-zone"
			class:active={isHovered}
			onmouseenter={handleMouseEnter}
			onmouseleave={handleMouseLeave}
		>
			<!-- Top bar with layout selector and optional focus nav -->
			<div class="top-bar">
				<!-- Focus Navigation (left side) -->
				{#if showFocusNav}
					<div class="focus-nav">
						<button
							class="focus-nav-btn"
							onclick={(e) => handleFocusNavClick(e, 'prev')}
							title="Previous card (←)"
						>
							<ChevronLeft size={18} strokeWidth={2} />
						</button>

						<div class="focus-nav-indicator">
							<span class="focus-nav-current">{focusModeIndex + 1}</span>
							<span class="focus-nav-separator">/</span>
							<span class="focus-nav-total">{focusModeTotal}</span>
						</div>

						<button
							class="focus-nav-btn"
							onclick={(e) => handleFocusNavClick(e, 'next')}
							title="Next card (→)"
						>
							<ChevronRight size={18} strokeWidth={2} />
						</button>
					</div>
				{/if}

				<!-- Layout Mode Selector (center/right) -->
				<div class="shuffle-trigger" class:expanded={isExpanded}>
					<div class="trigger-content">
						<svelte:component this={currentModeInfo.icon} size={14} strokeWidth={2} />
						<span class="trigger-label">{currentModeInfo.label}</span>
						<ChevronDown
							size={12}
							strokeWidth={2}
							class="chevron {isExpanded ? 'rotated' : ''}"
						/>
					</div>
				</div>
			</div>

			<!-- Expanded view: Layout options -->
			{#if isExpanded}
				<div class="shuffle-panel" role="menu">
					<div class="panel-header">
						<span class="header-title">Card Layout</span>
					</div>
					<div class="layout-options">
						{#each layoutModes as { mode, label, description, icon: Icon }}
							<button
								class="layout-option"
								class:active={currentMode === mode}
								onclick={() => handleModeSelect(mode)}
								role="menuitem"
								aria-current={currentMode === mode ? 'true' : undefined}
							>
								<div class="option-icon">
									<Icon size={18} strokeWidth={1.5} />
								</div>
								<div class="option-content">
									<span class="option-label">{label}</span>
									<span class="option-description">{description}</span>
								</div>
								{#if currentMode === mode}
									<div class="option-check">
										<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
											<path d="M20 6L9 17l-5-5" />
										</svg>
									</div>
								{/if}
							</button>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.card-shuffle-container {
		position: absolute;
		top: 0;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10001; /* Above cards and context panel */
		pointer-events: none;
	}

	/* Invisible trigger zone - wide area at top to detect mouse */
	.trigger-zone {
		position: absolute;
		top: 0;
		left: 50%;
		transform: translateX(-50%);
		width: 300px;
		height: 40px;
		pointer-events: auto;
	}

	.hover-zone {
		pointer-events: auto;
		display: flex;
		flex-direction: column;
		align-items: center;
		padding-top: 8px;
		animation: fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
	}

	.top-bar {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	/* Focus Mode Navigation */
	.focus-nav {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 8px;
		background: var(--glass-bg);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid var(--glass-border);
		border-radius: 24px;
		box-shadow: var(--shadow-m);
	}

	.focus-nav-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.focus-nav-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.focus-nav-btn:active {
		transform: scale(0.92);
	}

	.focus-nav-indicator {
		display: flex;
		align-items: center;
		gap: 3px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		min-width: 40px;
		justify-content: center;
		padding: 0 4px;
	}

	.focus-nav-current {
		color: var(--primary);
		font-weight: 600;
	}

	.focus-nav-separator {
		color: var(--muted-foreground);
	}

	.focus-nav-total {
		color: var(--muted-foreground);
	}

	@keyframes fadeIn {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.shuffle-trigger {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 16px;
		background: var(--glass-bg);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid var(--glass-border);
		border-radius: 24px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		box-shadow: var(--shadow-m);
	}

	.shuffle-trigger:hover {
		background: var(--card);
		border-color: var(--border);
		box-shadow: var(--shadow-l);
		transform: translateY(-1px);
	}

	.shuffle-trigger.expanded {
		background: var(--card);
		border-color: var(--primary);
		box-shadow: var(--shadow-l), 0 0 0 1px var(--primary);
	}

	.trigger-content {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.trigger-label {
		font-weight: 500;
		color: var(--foreground);
	}

	.trigger-content :global(.chevron) {
		color: var(--muted-foreground);
		transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), color 0.2s ease;
	}

	.shuffle-trigger:hover .trigger-content :global(.chevron),
	.shuffle-trigger.expanded .trigger-content :global(.chevron) {
		color: var(--primary);
	}

	.trigger-content :global(.chevron.rotated) {
		transform: rotate(180deg);
	}

	.shuffle-panel {
		margin-top: 8px;
		background: var(--popover);
		backdrop-filter: blur(24px);
		-webkit-backdrop-filter: blur(24px);
		border: 1px solid var(--border);
		border-radius: 16px;
		box-shadow: var(--shadow-l);
		overflow: hidden;
		min-width: 280px;
		animation: slideDown 0.2s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes slideDown {
		from {
			opacity: 0;
			transform: translateY(-12px) scale(0.96);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.panel-header {
		padding: 14px 16px 10px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 30%, transparent);
	}

	.header-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--muted-foreground);
	}

	.layout-options {
		padding: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.layout-option {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 10px 12px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 10px;
		color: var(--foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.layout-option:hover {
		background: var(--accent);
		border-color: var(--border);
		transform: translateX(2px);
	}

	.layout-option.active {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.layout-option.active:hover {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		border-color: color-mix(in oklch, var(--primary) 40%, transparent);
	}

	.option-icon {
		flex-shrink: 0;
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--muted);
		border-radius: 8px;
		color: var(--muted-foreground);
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		box-shadow: var(--shadow-s);
	}

	.layout-option:hover .option-icon {
		background: var(--secondary);
		color: var(--foreground);
		transform: scale(1.05);
		box-shadow: var(--shadow-m);
	}

	.layout-option.active .option-icon {
		background: color-mix(in oklch, var(--primary) 25%, transparent);
		color: var(--primary);
		box-shadow: 0 0 0 1px var(--primary), var(--shadow-s);
	}

	.layout-option.active:hover .option-icon {
		transform: scale(1.05);
		box-shadow: 0 0 0 1px var(--primary), var(--shadow-m);
	}

	.option-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.option-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.layout-option.active .option-label {
		color: var(--primary);
	}

	.option-description {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.option-check {
		flex-shrink: 0;
		width: 20px;
		height: 20px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--primary);
		border-radius: 50%;
		color: var(--primary-foreground);
		box-shadow: var(--shadow-s);
		animation: checkPop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes checkPop {
		from {
			transform: scale(0);
			opacity: 0;
		}
		to {
			transform: scale(1);
			opacity: 1;
		}
	}
</style>
