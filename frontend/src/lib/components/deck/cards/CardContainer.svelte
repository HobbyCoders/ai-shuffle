<script lang="ts">
	/**
	 * CardContainer - Main deck area that manages card layout
	 *
	 * Supports multiple layout modes:
	 * - stack: Cards overlap with peek visibility (like iOS cards)
	 * - split: 2-3 cards side by side
	 * - focus: One card expanded full
	 * - grid: Overview of all cards as thumbnails
	 *
	 * Features drag to reorder cards.
	 */
	import type { Snippet } from 'svelte';
	import type { DeckCard, LayoutMode } from './types';

	interface Props {
		cards: DeckCard[];
		layout?: LayoutMode;
		activeCardId?: string | null;
		children: Snippet<[{ card: DeckCard; index: number; isActive: boolean }]>;
		oncardactivate?: (id: string) => void;
		oncardclose?: (id: string) => void;
		oncardminimize?: (id: string) => void;
		onlayoutchange?: (layout: LayoutMode) => void;
		onreorder?: (cards: DeckCard[]) => void;
	}

	let {
		cards,
		layout = 'stack',
		activeCardId = null,
		children,
		oncardactivate,
		oncardclose,
		oncardminimize,
		onlayoutchange,
		onreorder
	}: Props = $props();

	// Internal card order (can differ from props during drag)
	let internalOrder = $state<string[]>([]);
	$effect(() => {
		internalOrder = cards.map((c) => c.id);
	});

	// Drag and drop state
	let draggedCardId = $state<string | null>(null);
	let dropTargetId = $state<string | null>(null);

	// Get ordered cards based on internal order
	const orderedCards = $derived(() => {
		const cardMap = new Map(cards.map((c) => [c.id, c]));
		return internalOrder.map((id) => cardMap.get(id)).filter((c): c is DeckCard => c !== undefined);
	});

	// Layout-specific card styles
	function getCardStyle(index: number, total: number, cardId: string): string {
		const isActive = cardId === activeCardId;
		const isDragged = cardId === draggedCardId;
		const isDropTarget = cardId === dropTargetId;

		let transform = '';
		let zIndex = index;
		let opacity = '1';

		if (isDragged) {
			return `z-index: 100; opacity: 0.8; transform: scale(1.02);`;
		}

		if (isDropTarget) {
			transform = 'translateY(8px)';
		}

		switch (layout) {
			case 'stack': {
				// Stack layout: cards offset vertically with peek visibility
				const offset = isActive ? 0 : (index - getActiveIndex()) * 8;
				const scale = isActive ? 1 : 0.98 - Math.abs(index - getActiveIndex()) * 0.01;
				zIndex = isActive ? 50 : total - Math.abs(index - getActiveIndex());
				opacity = isActive ? '1' : String(1 - Math.abs(index - getActiveIndex()) * 0.15);
				transform = `translateY(${offset}px) scale(${scale})`;
				break;
			}
			case 'split': {
				// Split layout: equal width side by side
				zIndex = isActive ? 10 : 1;
				break;
			}
			case 'focus': {
				// Focus layout: only active card visible
				if (!isActive) {
					opacity = '0';
					transform = 'scale(0.9)';
				}
				zIndex = isActive ? 10 : 0;
				break;
			}
			case 'grid': {
				// Grid layout: thumbnail view
				zIndex = isActive ? 10 : 1;
				break;
			}
		}

		return `z-index: ${zIndex}; opacity: ${opacity}; transform: ${transform};`;
	}

	function getActiveIndex(): number {
		return orderedCards().findIndex((c) => c.id === activeCardId);
	}

	// Container classes based on layout
	function getContainerClass(): string {
		switch (layout) {
			case 'stack':
				return 'deck-container--stack';
			case 'split':
				return 'deck-container--split';
			case 'focus':
				return 'deck-container--focus';
			case 'grid':
				return 'deck-container--grid';
			default:
				return '';
		}
	}

	// Drag handlers
	function handleDragStart(e: DragEvent, cardId: string) {
		draggedCardId = cardId;
		if (e.dataTransfer) {
			e.dataTransfer.effectAllowed = 'move';
		}
	}

	function handleDragOver(e: DragEvent, cardId: string) {
		e.preventDefault();
		if (draggedCardId && draggedCardId !== cardId) {
			dropTargetId = cardId;
		}
	}

	function handleDragLeave() {
		dropTargetId = null;
	}

	function handleDrop(e: DragEvent, targetId: string) {
		e.preventDefault();
		if (draggedCardId && draggedCardId !== targetId) {
			// Reorder cards
			const newOrder = [...internalOrder];
			const draggedIndex = newOrder.indexOf(draggedCardId);
			const targetIndex = newOrder.indexOf(targetId);

			if (draggedIndex !== -1 && targetIndex !== -1) {
				newOrder.splice(draggedIndex, 1);
				newOrder.splice(targetIndex, 0, draggedCardId);
				internalOrder = newOrder;

				// Emit reorder event with full card objects
				const reorderedCards = newOrder
					.map((id) => cards.find((c) => c.id === id))
					.filter((c): c is DeckCard => c !== undefined);
				onreorder?.(reorderedCards);
			}
		}
		draggedCardId = null;
		dropTargetId = null;
	}

	function handleDragEnd() {
		draggedCardId = null;
		dropTargetId = null;
	}

	// Layout switcher
	function cycleLayout() {
		const layouts: LayoutMode[] = ['stack', 'split', 'focus', 'grid'];
		const currentIndex = layouts.indexOf(layout);
		const nextIndex = (currentIndex + 1) % layouts.length;
		onlayoutchange?.(layouts[nextIndex]);
	}

	// Keyboard navigation
	function handleKeydown(e: KeyboardEvent) {
		if (!activeCardId) return;

		const currentIndex = getActiveIndex();
		let newIndex = currentIndex;

		switch (e.key) {
			case 'ArrowLeft':
			case 'ArrowUp':
				newIndex = Math.max(0, currentIndex - 1);
				break;
			case 'ArrowRight':
			case 'ArrowDown':
				newIndex = Math.min(orderedCards().length - 1, currentIndex + 1);
				break;
			case 'Home':
				newIndex = 0;
				break;
			case 'End':
				newIndex = orderedCards().length - 1;
				break;
			default:
				return;
		}

		if (newIndex !== currentIndex) {
			e.preventDefault();
			const newCard = orderedCards()[newIndex];
			if (newCard) {
				oncardactivate?.(newCard.id);
			}
		}
	}

	// Layout icon paths
	function getLayoutIcon(l: LayoutMode): string {
		switch (l) {
			case 'stack':
				return 'M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10';
			case 'split':
				return 'M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2';
			case 'focus':
				return 'M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4';
			case 'grid':
				return 'M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z';
			default:
				return '';
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div
	class="deck-container relative w-full h-full {getContainerClass()}"
	role="region"
	aria-label="Card deck"
>
	<!-- Layout switcher button -->
	<div class="absolute top-2 right-2 z-50 flex items-center gap-1 bg-card/80 backdrop-blur-sm rounded-lg p-1 border border-border/50">
		{#each ['stack', 'split', 'focus', 'grid'] as l}
			<button
				type="button"
				class="p-1.5 rounded-md transition-colors {layout === l ? 'bg-primary/20 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
				onclick={() => onlayoutchange?.(l as LayoutMode)}
				aria-label="{l} layout"
				aria-pressed={layout === l}
				title="{l.charAt(0).toUpperCase() + l.slice(1)} layout"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getLayoutIcon(l as LayoutMode)} />
				</svg>
			</button>
		{/each}
	</div>

	<!-- Cards container -->
	<div class="deck-cards {getContainerClass()}">
		{#each orderedCards() as card, index (card.id)}
			<div
				class="deck-card-wrapper transition-all duration-300 ease-out"
				style={getCardStyle(index, orderedCards().length, card.id)}
				ondragover={(e) => handleDragOver(e, card.id)}
				ondragleave={handleDragLeave}
				ondrop={(e) => handleDrop(e, card.id)}
				role="listitem"
			>
				{@render children({ card, index, isActive: card.id === activeCardId })}
			</div>
		{/each}
	</div>

	<!-- Empty state -->
	{#if cards.length === 0}
		<div class="absolute inset-0 flex items-center justify-center">
			<div class="text-center p-8 max-w-md">
				<div class="w-16 h-16 rounded-2xl bg-muted/50 flex items-center justify-center mx-auto mb-4">
					<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
					</svg>
				</div>
				<h3 class="text-lg font-medium text-foreground mb-2">No cards open</h3>
				<p class="text-sm text-muted-foreground">
					Start a new chat, launch an agent, or create content to see cards here.
				</p>
			</div>
		</div>
	{/if}

	<!-- Card count indicator -->
	{#if cards.length > 0}
		<div class="absolute bottom-2 left-2 z-50 flex items-center gap-2 bg-card/80 backdrop-blur-sm rounded-lg px-3 py-1.5 border border-border/50">
			<span class="text-xs text-muted-foreground">
				{cards.length} card{cards.length !== 1 ? 's' : ''}
			</span>
			{#if activeCardId}
				<span class="text-xs text-foreground font-medium">
					{getActiveIndex() + 1}/{cards.length}
				</span>
			{/if}
		</div>
	{/if}
</div>

<style>
	.deck-container {
		padding: 3rem 1rem 2.5rem;
	}

	.deck-cards {
		position: relative;
		width: 100%;
		height: 100%;
	}

	/* Stack layout */
	.deck-container--stack .deck-cards {
		display: flex;
		flex-direction: column;
		align-items: center;
	}

	.deck-container--stack .deck-card-wrapper {
		position: absolute;
		width: calc(100% - 2rem);
		max-width: 800px;
		height: calc(100% - 40px);
		left: 50%;
		transform-origin: center top;
		margin-left: calc(-50% + 1rem);
	}

	/* Split layout */
	.deck-container--split .deck-cards {
		display: flex;
		flex-direction: row;
		gap: 8px;
		height: 100%;
	}

	.deck-container--split .deck-card-wrapper {
		flex: 1;
		min-width: 0;
		max-width: calc(50% - 4px);
	}

	.deck-container--split .deck-card-wrapper:nth-child(n+3) {
		max-width: calc(33.333% - 6px);
	}

	/* Focus layout */
	.deck-container--focus .deck-cards {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
	}

	.deck-container--focus .deck-card-wrapper {
		position: absolute;
		width: calc(100% - 2rem);
		max-width: 900px;
		height: calc(100% - 2rem);
		transition: opacity 0.3s, transform 0.3s;
	}

	/* Grid layout */
	.deck-container--grid .deck-cards {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
		gap: 12px;
		height: auto;
		max-height: 100%;
		overflow-y: auto;
		padding: 4px;
	}

	.deck-container--grid .deck-card-wrapper {
		height: 200px;
		cursor: pointer;
	}

	/* Responsive adjustments */
	@media (max-width: 640px) {
		.deck-container {
			padding: 3rem 0.5rem 2rem;
		}

		.deck-container--split .deck-cards {
			flex-direction: column;
		}

		.deck-container--split .deck-card-wrapper {
			max-width: 100%;
			height: auto;
			min-height: 300px;
		}

		.deck-container--grid .deck-cards {
			grid-template-columns: 1fr;
		}
	}
</style>
