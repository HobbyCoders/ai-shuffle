<script lang="ts">
	/**
	 * MobileWorkspace - Full-screen stacked cards for mobile devices
	 *
	 * Features:
	 * - Swipeable card stack (touch gestures)
	 * - Velocity-based animation
	 * - Dot indicators (tappable)
	 * - Minimal card header (back arrow, title, close)
	 * - Empty state with create buttons
	 */

	import { ChevronLeft, X, MessageSquare, Bot, Palette, Terminal, Plus, Settings, Layers } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import type { Snippet } from 'svelte';

	interface Props {
		cards: DeckCard[];
		activeCardIndex: number;
		minimizedCount?: number;
		onCardChange: (index: number) => void;
		onCloseCard: (id: string) => void;
		onCreateCard: (type: CardType) => void;
		onSettingsClick?: () => void;
		onMinimizedClick?: () => void;
		children?: Snippet<[DeckCard]>;
	}

	let {
		cards,
		activeCardIndex,
		minimizedCount = 0,
		onCardChange,
		onCloseCard,
		onCreateCard,
		onSettingsClick,
		onMinimizedClick,
		children
	}: Props = $props();

	// Swipe state
	let touchStartX = $state(0);
	let touchStartY = $state(0);
	let touchCurrentX = $state(0);
	let touchStartTime = $state(0);
	let isSwiping = $state(false);
	let swipeDirection = $state<'horizontal' | 'vertical' | null>(null);

	const SWIPE_THRESHOLD = 50;
	const VELOCITY_THRESHOLD = 0.3;
	const DIRECTION_LOCK_THRESHOLD = 10;

	// Icon mapping
	const cardIcons: Record<CardType, typeof MessageSquare> = {
		chat: MessageSquare,
		agent: Bot,
		canvas: Palette,
		terminal: Terminal,
	};

	// Card type config for create buttons
	const cardTypes: { type: CardType; label: string; icon: typeof MessageSquare }[] = [
		{ type: 'chat', label: 'Chat', icon: MessageSquare },
		{ type: 'agent', label: 'Agent', icon: Bot },
		{ type: 'canvas', label: 'Canvas', icon: Palette },
		{ type: 'terminal', label: 'Terminal', icon: Terminal },
	];

	// Current card
	const activeCard = $derived(cards[activeCardIndex]);

	// Swipe offset for animation
	const swipeOffset = $derived(isSwiping && swipeDirection === 'horizontal' ? touchCurrentX - touchStartX : 0);

	// Touch handlers
	function handleTouchStart(e: TouchEvent) {
		if (cards.length <= 1) return;

		touchStartX = e.touches[0].clientX;
		touchStartY = e.touches[0].clientY;
		touchCurrentX = touchStartX;
		touchStartTime = Date.now();
		swipeDirection = null;
	}

	function handleTouchMove(e: TouchEvent) {
		if (!touchStartTime) return;

		const currentX = e.touches[0].clientX;
		const currentY = e.touches[0].clientY;
		const diffX = currentX - touchStartX;
		const diffY = currentY - touchStartY;

		// Lock direction after threshold
		if (swipeDirection === null) {
			if (Math.abs(diffX) > DIRECTION_LOCK_THRESHOLD || Math.abs(diffY) > DIRECTION_LOCK_THRESHOLD) {
				swipeDirection = Math.abs(diffX) > Math.abs(diffY) ? 'horizontal' : 'vertical';
				if (swipeDirection === 'horizontal') {
					isSwiping = true;
				}
			}
		}

		if (swipeDirection === 'horizontal') {
			touchCurrentX = currentX;
			// Prevent scroll
			e.preventDefault();
		}
	}

	function handleTouchEnd() {
		if (!isSwiping || swipeDirection !== 'horizontal') {
			resetSwipeState();
			return;
		}

		const diffX = touchCurrentX - touchStartX;
		const elapsed = Date.now() - touchStartTime;
		const velocity = Math.abs(diffX) / elapsed;

		let newIndex = activeCardIndex;

		// Check if swipe exceeds threshold or velocity
		if (Math.abs(diffX) > SWIPE_THRESHOLD || velocity > VELOCITY_THRESHOLD) {
			if (diffX > 0 && activeCardIndex > 0) {
				// Swipe right - go to previous
				newIndex = activeCardIndex - 1;
			} else if (diffX < 0 && activeCardIndex < cards.length - 1) {
				// Swipe left - go to next
				newIndex = activeCardIndex + 1;
			}
		}

		if (newIndex !== activeCardIndex) {
			onCardChange(newIndex);
		}

		resetSwipeState();
	}

	function resetSwipeState() {
		isSwiping = false;
		touchStartX = 0;
		touchStartY = 0;
		touchCurrentX = 0;
		touchStartTime = 0;
		swipeDirection = null;
	}

	// Navigate to specific card via dot indicator
	function navigateToCard(index: number) {
		if (index !== activeCardIndex && index >= 0 && index < cards.length) {
			onCardChange(index);
		}
	}

	// Close current card
	function handleClose() {
		if (activeCard) {
			onCloseCard(activeCard.id);
		}
	}

	// Go back (to previous card or close if first)
	function handleBack() {
		if (activeCardIndex > 0) {
			onCardChange(activeCardIndex - 1);
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="mobile-workspace">
	<!-- Mobile Toolbar - always visible at top -->
	<div class="mobile-toolbar">
		<button
			class="toolbar-btn"
			onclick={() => onMinimizedClick?.()}
			title="Minimized cards"
			disabled={minimizedCount === 0}
		>
			<Layers size={20} />
			{#if minimizedCount > 0}
				<span class="badge">{minimizedCount}</span>
			{/if}
		</button>
		<button
			class="toolbar-btn"
			onclick={() => onSettingsClick?.()}
			title="Settings"
		>
			<Settings size={20} />
		</button>
	</div>

	{#if cards.length === 0}
		<!-- Empty State -->
		<div class="empty-state">
			<div class="empty-content">
				<div class="empty-icon">
					<Plus size={32} />
				</div>
				<h2 class="empty-title">No cards open</h2>
				<p class="empty-text">Create a card to get started</p>
				<div class="create-grid">
					{#each cardTypes as { type, label, icon: Icon }}
						<button
							class="create-card"
							onclick={() => onCreateCard(type)}
						>
							<Icon size={24} />
							<span>{label}</span>
						</button>
					{/each}
				</div>
			</div>
		</div>
	{:else}
		<!-- Mobile Header -->
		<header class="mobile-header">
			<button
				class="header-btn"
				onclick={handleBack}
				disabled={activeCardIndex === 0}
				aria-label="Go back"
			>
				<ChevronLeft size={24} />
			</button>

			<div class="header-center">
				{#if activeCard}
					{@const Icon = cardIcons[activeCard.type]}
					<Icon size={16} />
					<span class="header-title">{activeCard.title}</span>
				{/if}
			</div>

			<button
				class="header-btn close"
				onclick={handleClose}
				aria-label="Close card"
			>
				<X size={20} />
			</button>
		</header>

		<!-- Card Container with Swipe -->
		<div
			class="card-container"
			ontouchstart={handleTouchStart}
			ontouchmove={handleTouchMove}
			ontouchend={handleTouchEnd}
		>
			<div
				class="card-wrapper"
				class:swiping={isSwiping}
				style:transform="translateX({swipeOffset}px)"
			>
				{#if activeCard && children}
					{@render children(activeCard)}
				{/if}
			</div>
		</div>

		<!-- Dot Indicators -->
		{#if cards.length > 1}
			<div class="dot-indicators">
				{#each cards as card, index}
					<button
						class="dot"
						class:active={index === activeCardIndex}
						onclick={() => navigateToCard(index)}
						aria-label="Go to card {index + 1}"
					>
						<span class="dot-inner"></span>
					</button>
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.mobile-workspace {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: hsl(var(--background));
	}

	/* Mobile Toolbar */
	.mobile-toolbar {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 8px;
		padding: 8px 12px;
		background: hsl(var(--card));
		border-bottom: 1px solid hsl(var(--border));
		flex-shrink: 0;
	}

	.toolbar-btn {
		position: relative;
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.toolbar-btn:hover {
		background: hsl(var(--accent));
		color: hsl(var(--foreground));
	}

	.toolbar-btn:disabled {
		opacity: 0.4;
		cursor: default;
	}

	.toolbar-btn .badge {
		position: absolute;
		top: 4px;
		right: 4px;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		background: hsl(var(--primary));
		border-radius: 8px;
		font-size: 0.625rem;
		font-weight: 600;
		color: hsl(var(--primary-foreground));
		display: flex;
		align-items: center;
		justify-content: center;
	}

	/* Header */
	.mobile-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 56px;
		padding: 0 8px;
		background: hsl(var(--card));
		border-bottom: 1px solid hsl(var(--border));
		flex-shrink: 0;
	}

	.header-btn {
		width: 44px;
		height: 44px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.header-btn:hover {
		background: hsl(var(--accent));
	}

	.header-btn:disabled {
		opacity: 0.3;
		cursor: default;
	}

	.header-btn.close:hover {
		background: hsl(var(--destructive) / 0.1);
		color: hsl(var(--destructive));
	}

	.header-center {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		justify-content: center;
		color: hsl(var(--muted-foreground));
	}

	.header-title {
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
		max-width: 200px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Card Container */
	.card-container {
		flex: 1;
		overflow: hidden;
		touch-action: pan-y;
	}

	.card-wrapper {
		height: 100%;
		transition: transform 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94);
	}

	.card-wrapper.swiping {
		transition: none;
	}

	/* Dot Indicators */
	.dot-indicators {
		display: flex;
		justify-content: center;
		gap: 8px;
		padding: 12px;
		background: hsl(var(--card));
		border-top: 1px solid hsl(var(--border));
	}

	.dot {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		padding: 0;
		cursor: pointer;
	}

	.dot-inner {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: hsl(var(--muted-foreground) / 0.3);
		transition: all 0.2s ease;
	}

	.dot.active .dot-inner {
		width: 10px;
		height: 10px;
		background: hsl(var(--primary));
	}

	.dot:hover .dot-inner {
		background: hsl(var(--muted-foreground) / 0.5);
	}

	.dot.active:hover .dot-inner {
		background: hsl(var(--primary));
	}

	/* Empty State */
	.empty-state {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 24px;
	}

	.empty-content {
		text-align: center;
		max-width: 320px;
	}

	.empty-icon {
		width: 64px;
		height: 64px;
		margin: 0 auto 16px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted));
		border-radius: 50%;
		color: hsl(var(--muted-foreground));
	}

	.empty-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin-bottom: 8px;
	}

	.empty-text {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		margin-bottom: 24px;
	}

	.create-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 12px;
	}

	.create-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 20px 16px;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 12px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.create-card:hover {
		background: hsl(var(--accent));
		border-color: hsl(var(--primary) / 0.3);
	}

	.create-card:active {
		transform: scale(0.98);
	}
</style>
