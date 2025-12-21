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
	 */

	import {
		Move,
		Columns,
		LayoutGrid,
		Layers,
		Maximize2,
		ChevronDown
	} from 'lucide-svelte';
	import type { LayoutMode } from '$lib/stores/deck';

	interface Props {
		currentMode: LayoutMode;
		onModeChange: (mode: LayoutMode) => void;
	}

	let { currentMode, onModeChange }: Props = $props();

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
			<!-- Collapsed view: Just a subtle indicator -->
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
		padding-top: 4px;
		animation: fadeIn 0.15s ease-out;
	}

	@keyframes fadeIn {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.shuffle-trigger {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		background: hsl(var(--card) / 0.8);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid hsl(var(--border) / 0.5);
		border-radius: 20px;
		color: hsl(var(--muted-foreground));
		font-size: 0.75rem;
		cursor: pointer;
		transition: all 0.2s ease;
		box-shadow: 0 2px 8px hsl(var(--background) / 0.3);
	}

	.shuffle-trigger:hover,
	.shuffle-trigger.expanded {
		background: hsl(var(--card));
		border-color: hsl(var(--border));
		color: hsl(var(--foreground));
		box-shadow: 0 4px 16px hsl(var(--background) / 0.4);
	}

	.trigger-content {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.trigger-label {
		font-weight: 500;
	}

	.trigger-content :global(.chevron) {
		transition: transform 0.2s ease;
	}

	.trigger-content :global(.chevron.rotated) {
		transform: rotate(180deg);
	}

	.shuffle-panel {
		margin-top: 4px;
		background: hsl(var(--popover));
		backdrop-filter: blur(24px);
		-webkit-backdrop-filter: blur(24px);
		border: 1px solid hsl(var(--border));
		border-radius: 12px;
		box-shadow:
			0 10px 40px -10px hsl(var(--background) / 0.5),
			0 4px 12px hsl(var(--background) / 0.2);
		overflow: hidden;
		min-width: 240px;
		animation: slideDown 0.15s ease-out;
	}

	@keyframes slideDown {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.panel-header {
		padding: 10px 14px 6px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
	}

	.header-title {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: hsl(var(--muted-foreground));
	}

	.layout-options {
		padding: 6px;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.layout-option {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.layout-option:hover {
		background: hsl(var(--accent));
	}

	.layout-option.active {
		background: hsl(var(--primary) / 0.1);
	}

	.layout-option.active:hover {
		background: hsl(var(--primary) / 0.15);
	}

	.option-icon {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted) / 0.5);
		border-radius: 6px;
		color: hsl(var(--muted-foreground));
		transition: all 0.15s ease;
	}

	.layout-option:hover .option-icon,
	.layout-option.active .option-icon {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	.option-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.option-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.option-description {
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.option-check {
		flex-shrink: 0;
		color: hsl(var(--primary));
	}
</style>
