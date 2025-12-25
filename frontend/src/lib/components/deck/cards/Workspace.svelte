<script lang="ts">
	/**
	 * Workspace - Desktop canvas for free-form draggable cards
	 *
	 * Features:
	 * - Contains all visible (non-minimized) cards
	 * - Empty state with welcome message and create buttons
	 * - Tracks workspace bounds
	 * - Card Shuffle UI for layout mode switching
	 */

	import { onMount } from 'svelte';
	import { MessageSquare, Bot, Terminal } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import { WORKSPACE_PADDING } from './types';
	import type { Snippet } from 'svelte';
	import type { LayoutMode } from '$lib/stores/deck';
	import CardShuffle from './CardShuffle.svelte';

	interface Props {
		cards: DeckCard[];
		onCardFocus: (id: string) => void;
		onCardMove: (id: string, x: number, y: number) => void;
		onCardResize: (id: string, width: number, height: number) => void;
		onCreateCard: (type: CardType) => void;
		layoutMode?: LayoutMode;
		onLayoutModeChange?: (mode: LayoutMode) => void;
		onFocusNavigate?: (direction: 'prev' | 'next') => void;
		focusedCardId?: string | null;
		children?: Snippet;
	}

	let {
		cards,
		onCardFocus,
		onCardMove,
		onCardResize,
		onCreateCard,
		layoutMode = 'freeflow',
		onLayoutModeChange,
		onFocusNavigate,
		focusedCardId = null,
		children
	}: Props = $props();

	// Focus mode navigation state
	const isFocusMode = $derived(layoutMode === 'focus');
	const focusModeCards = $derived(() => {
		if (!isFocusMode) return [];
		return [...visibleCards].sort(
			(a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime()
		);
	});
	const focusModeIndex = $derived(() => {
		const cards = focusModeCards();
		if (cards.length === 0 || !focusedCardId) return 0;
		const idx = cards.findIndex((c) => c.id === focusedCardId);
		return idx >= 0 ? idx : 0;
	});
	const focusModeTotal = $derived(() => focusModeCards().length);

	// Handle layout mode change
	function handleLayoutModeChange(mode: LayoutMode) {
		onLayoutModeChange?.(mode);
	}

	let workspaceEl: HTMLDivElement | undefined = $state();
	let workspaceBounds = $state({ width: 0, height: 0 });

	// Track workspace size
	function updateBounds() {
		if (workspaceEl) {
			workspaceBounds = {
				width: workspaceEl.clientWidth,
				height: workspaceEl.clientHeight,
			};
		}
	}

	onMount(() => {
		updateBounds();

		const resizeObserver = new ResizeObserver(updateBounds);
		if (workspaceEl) {
			resizeObserver.observe(workspaceEl);
		}

		return () => resizeObserver.disconnect();
	});

	// Visible cards (not minimized)
	const visibleCards = $derived(cards.filter((c) => !c.minimized));

	// Sorted by z-index for rendering
	const sortedCards = $derived([...visibleCards].sort((a, b) => a.zIndex - b.zIndex));

	// Check if there's a maximized card
	const maximizedCard = $derived(cards.find((c) => c.maximized));
	const hasMaximizedCard = $derived(!!maximizedCard);

	/**
	 * Clamp position to keep card within workspace bounds
	 * Ensures at least minVisible pixels of the card remain visible
	 */
	export function clampToBounds(x: number, y: number, width: number, height: number): { x: number; y: number } {
		const bounds = workspaceBounds;
		const padding = WORKSPACE_PADDING;

		// Calculate boundaries
		const minX = padding.left - width + padding.minVisible; // Allow card to go left, but keep minVisible showing
		const maxX = bounds.width - padding.right - padding.minVisible; // Keep minVisible on right
		const minY = padding.top; // Don't let header go above workspace
		const maxY = bounds.height - padding.bottom - 40; // Keep at least the header (40px) visible

		return {
			x: Math.max(minX, Math.min(x, maxX)),
			y: Math.max(minY, Math.min(y, maxY))
		};
	}

	// Card type config for create menu (used in empty state)
	const cardTypes: { type: CardType; label: string; icon: typeof MessageSquare }[] = [
		{ type: 'chat', label: 'New Chat', icon: MessageSquare },
		{ type: 'agent', label: 'New Agent', icon: Bot },
		{ type: 'terminal', label: 'New Terminal', icon: Terminal }
	];
</script>

<div
	bind:this={workspaceEl}
	class="workspace"
	class:has-maximized={hasMaximizedCard}
>
	<!-- Card Shuffle UI - Layout mode selector with focus nav integration -->
	{#if sortedCards.length > 0}
		<CardShuffle
			currentMode={layoutMode}
			onModeChange={handleLayoutModeChange}
			focusModeIndex={focusModeIndex()}
			focusModeTotal={focusModeTotal()}
			onFocusNavigate={onFocusNavigate}
		/>
	{/if}

	<!-- Render cards via children snippet -->
	{#if children}
		{@render children()}
	{/if}

	<!-- Empty state when no cards -->
	{#if sortedCards.length === 0}
		<div class="empty-state">
			<div class="empty-content">
				<div class="empty-icon">
					<MessageSquare size={48} strokeWidth={1} />
				</div>
				<h2 class="empty-title">Welcome to The Deck</h2>
				<p class="empty-text">Your workspace is empty. Create a card to get started.</p>
				<div class="create-buttons">
					{#each cardTypes as { type, label, icon: Icon }}
						<button
							class="create-btn"
							onclick={() => onCreateCard(type)}
						>
							<Icon size={18} />
							<span>{label}</span>
						</button>
					{/each}
				</div>
			</div>
		</div>
	{/if}

</div>

<style>
	.workspace {
		position: relative;
		flex: 1;
		overflow: hidden;
		background: hsl(var(--background));
		min-height: 0;
		height: 100%;
		/* Left padding for floating activity pill (56px + 16px + 12px gap) */
		padding-left: 84px;
	}

	/* On mobile, no left padding needed since pill is at bottom */
	@media (max-width: 639px) {
		.workspace {
			padding-left: 0;
			/* Bottom padding for floating pill on mobile */
			padding-bottom: 80px;
		}
	}

	.workspace.has-maximized {
		/* Placeholder for maximized card styling - prevents z-index conflicts */
		overflow: hidden;
	}

	/* Empty State */
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
		color: hsl(var(--muted-foreground));
		pointer-events: auto;
	}

	.empty-icon {
		width: 80px;
		height: 80px;
		margin: 0 auto 1.5rem;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted) / 0.5);
		border-radius: 50%;
		color: hsl(var(--muted-foreground));
	}

	.empty-title {
		font-size: 1.25rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin-bottom: 0.5rem;
	}

	.empty-text {
		font-size: 0.875rem;
		margin-bottom: 1.5rem;
	}

	.create-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		justify-content: center;
	}

	.create-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1rem;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.create-btn:hover {
		background: hsl(var(--accent));
		border-color: hsl(var(--primary) / 0.3);
	}
</style>
