<script lang="ts">
	/**
	 * Workspace - Desktop-like container for cards
	 *
	 * This is the main workspace area where cards can be:
	 * - Dragged around freely
	 * - Resized
	 * - Stacked (z-index management)
	 * - Minimized to dock
	 */

	import { onMount } from 'svelte';
	import {
		workspace,
		openCards,
		minimizedCards,
		maximizedCard,
		activeMode,
		WORKSPACE_MODES,
		type WorkspaceCard as WorkspaceCardType
	} from '$lib/stores/workspace';
	import WorkspaceCard from './WorkspaceCard.svelte';
	import CardDock from './CardDock.svelte';

	interface Props {
		class?: string;
	}

	let { class: className = '' }: Props = $props();

	let workspaceEl: HTMLDivElement;
	let workspaceBounds = $state({ width: 0, height: 0 });

	// Track workspace size for constraining card positions
	function updateBounds() {
		if (workspaceEl) {
			workspaceBounds = {
				width: workspaceEl.clientWidth,
				height: workspaceEl.clientHeight
			};
		}
	}

	onMount(() => {
		updateBounds();

		const resizeObserver = new ResizeObserver(updateBounds);
		resizeObserver.observe(workspaceEl);

		return () => resizeObserver.disconnect();
	});

	// Get cards sorted by z-index for rendering
	const sortedCards = $derived(
		[...$openCards].sort((a, b) => a.zIndex - b.zIndex)
	);

	// Check if there's a maximized card
	const hasMaximizedCard = $derived($maximizedCard !== undefined);

	// Get current mode config for empty state
	const currentModeConfig = $derived(WORKSPACE_MODES.find(m => m.id === $activeMode) || WORKSPACE_MODES[0]);
</script>

<div
	bind:this={workspaceEl}
	class="workspace {className}"
	class:has-maximized={hasMaximizedCard}
>
	<!-- Render cards -->
	{#each sortedCards as card (card.id)}
		<WorkspaceCard
			{card}
			bounds={workspaceBounds}
			isMaximized={card.state === 'maximized'}
		/>
	{/each}

	<!-- Card dock for minimized cards -->
	{#if $minimizedCards.length > 0}
		<CardDock cards={$minimizedCards} />
	{/if}

	<!-- Empty state when no cards -->
	{#if $openCards.length === 0 && $minimizedCards.length === 0}
		<div class="empty-state">
			<div class="empty-content">
				<svg
					class="empty-icon"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
				>
					<path d={currentModeConfig.icon} />
				</svg>
				<p class="empty-text">No {currentModeConfig.name.toLowerCase()} open</p>
				<p class="empty-hint">
					{#if $activeMode === 'chat'}
						Open a conversation from the sidebar or click <kbd>+</kbd>
					{:else if $activeMode === 'files'}
						Open a file browser from the sidebar or click <kbd>+</kbd>
					{:else}
						Click <kbd>+</kbd> to get started
					{/if}
				</p>
			</div>
		</div>
	{/if}
</div>

<style>
	.workspace {
		position: relative;
		flex: 1;
		overflow: hidden;
		background: var(--workspace-bg, #0a0a0a);
	}

	.empty-state {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
	}

	.empty-content {
		text-align: center;
		color: rgba(255, 255, 255, 0.3);
	}

	.empty-icon {
		width: 64px;
		height: 64px;
		margin: 0 auto 1rem;
		opacity: 0.5;
	}

	.empty-text {
		font-size: 1.125rem;
		font-weight: 500;
		margin-bottom: 0.5rem;
	}

	.empty-hint {
		font-size: 0.875rem;
		opacity: 0.7;
	}

	.empty-hint kbd {
		display: inline-block;
		padding: 0.125rem 0.375rem;
		font-family: monospace;
		font-size: 0.75rem;
		background: rgba(255, 255, 255, 0.1);
		border-radius: 4px;
		border: 1px solid rgba(255, 255, 255, 0.1);
	}
</style>
