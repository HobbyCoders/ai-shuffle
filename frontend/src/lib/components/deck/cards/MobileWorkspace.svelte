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

	import { ChevronLeft, X, MessageSquare, Terminal, Plus, Settings, User, Users, FolderKanban, Image, Box, AudioLines, FolderOpen, Puzzle } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import type { Snippet } from 'svelte';
	import { MobileWelcome } from '../welcome';

	interface Props {
		cards: DeckCard[];
		activeCardIndex: number;
		onCardChange: (index: number) => void;
		onCloseCard: (id: string) => void;
		onCreateCard: (type: CardType) => void;
		onOpenNavigator?: () => void;
		children?: Snippet<[DeckCard]>;
	}

	let {
		cards,
		activeCardIndex,
		onCardChange,
		onCloseCard,
		onCreateCard,
		onOpenNavigator,
		children
	}: Props = $props();

	// Swipe state
	let touchStartX = $state(0);
	let touchStartY = $state(0);
	let touchCurrentX = $state(0);
	let touchStartTime = $state(0);
	let isSwiping = $state(false);
	let swipeDirection = $state<'horizontal' | 'vertical' | null>(null);
	let isAnimating = $state(false);

	const SWIPE_THRESHOLD = 50;
	const VELOCITY_THRESHOLD = 0.3;
	const DIRECTION_LOCK_THRESHOLD = 10;
	const ANIMATION_DURATION = 300; // Match CSS transition duration

	// Icon mapping - all 12 card types
	const cardIcons: Record<CardType, typeof MessageSquare> = {
		chat: MessageSquare,
		terminal: Terminal,
		settings: Settings,
		profile: User,
		subagent: Users,
		project: FolderKanban,
		'user-settings': Settings,
		'image-studio': Image,
		'model-studio': Box,
		'audio-studio': AudioLines,
		'file-browser': FolderOpen,
		plugins: Puzzle,
	};

	// Current card
	const activeCard = $derived(cards[activeCardIndex]);

	// Swipe offset for animation
	const swipeOffset = $derived(isSwiping && swipeDirection === 'horizontal' ? touchCurrentX - touchStartX : 0);

	// Swipe progress for visual feedback (0 to 1, capped)
	const swipeProgress = $derived(Math.min(Math.abs(swipeOffset) / (SWIPE_THRESHOLD * 2), 1));

	// Direction indicator for swipe feedback
	const swipeIndicatorDirection = $derived(swipeOffset > 0 ? 'left' : 'right');

	// Check if touch target is an interactive element (input, textarea, etc.)
	function isInteractiveElement(target: EventTarget | null): boolean {
		if (!(target instanceof HTMLElement)) return false;
		const tagName = target.tagName.toLowerCase();
		const interactiveTags = ['input', 'textarea', 'select', 'button', 'a'];
		if (interactiveTags.includes(tagName)) return true;
		// Check for contenteditable
		if (target.isContentEditable) return true;
		// Check if inside an interactive element
		const interactive = target.closest('input, textarea, select, button, a, [contenteditable="true"]');
		return interactive !== null;
	}

	// Touch handlers
	function handleTouchStart(e: TouchEvent) {
		if (cards.length <= 1) return;

		// Block new swipes during animation
		if (isAnimating) return;

		// Don't initiate swipe if touching an interactive element
		if (isInteractiveElement(e.target)) return;

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
			// Blur any focused element before switching to prevent focus jumping issues
			if (document.activeElement instanceof HTMLElement) {
				document.activeElement.blur();
			}

			// Lock animation to prevent rapid successive swipes
			isAnimating = true;
			onCardChange(newIndex);
			setTimeout(() => {
				isAnimating = false;
			}, ANIMATION_DURATION);
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
		if (isAnimating) return;
		if (index !== activeCardIndex && index >= 0 && index < cards.length) {
			// Blur any focused element before switching to prevent focus jumping issues
			if (document.activeElement instanceof HTMLElement) {
				document.activeElement.blur();
			}

			// Lock animation to prevent rapid tapping
			isAnimating = true;
			onCardChange(index);
			setTimeout(() => {
				isAnimating = false;
			}, ANIMATION_DURATION);
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
	{#if cards.length === 0}
		<!-- Mobile Welcome Screen with Carousel -->
		<MobileWelcome {onCreateCard} />
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

			<div class="header-actions">
				{#if onOpenNavigator}
					<button
						class="header-btn add"
						onclick={onOpenNavigator}
						aria-label="Open card navigator"
					>
						<Plus size={20} />
					</button>
				{/if}
				<button
					class="header-btn close"
					onclick={handleClose}
					aria-label="Close card"
				>
					<X size={20} />
				</button>
			</div>
		</header>

		<!-- Card Container with Swipe -->
		<div
			class="card-container"
			ontouchstart={handleTouchStart}
			ontouchmove={handleTouchMove}
			ontouchend={handleTouchEnd}
		>
			<!-- Swipe direction indicators -->
			{#if isSwiping && swipeDirection === 'horizontal'}
				<div
					class="swipe-indicator left"
					class:visible={swipeOffset > SWIPE_THRESHOLD / 2 && activeCardIndex > 0}
					style:opacity={swipeIndicatorDirection === 'left' ? swipeProgress : 0}
				>
					<ChevronLeft size={24} />
				</div>
				<div
					class="swipe-indicator right"
					class:visible={swipeOffset < -SWIPE_THRESHOLD / 2 && activeCardIndex < cards.length - 1}
					style:opacity={swipeIndicatorDirection === 'right' ? swipeProgress : 0}
				>
					<ChevronLeft size={24} style="transform: rotate(180deg)" />
				</div>
			{/if}

			<div
				class="card-wrapper"
				class:swiping={isSwiping}
				style:transform="translateX({swipeOffset}px)"
				style:opacity={1 - swipeProgress * 0.15}
			>
				{#if activeCard && children}
					<!-- Use optional chaining to prevent undefined.id error during card removal -->
					{#key activeCard?.id ?? 'none'}
						{@render children(activeCard)}
					{/key}
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
		/* Safe area support for notched devices */
		padding-top: env(safe-area-inset-top, 0);
		padding-left: env(safe-area-inset-left, 0);
		padding-right: env(safe-area-inset-right, 0);
	}

	/* Header - improved contrast and readability */
	.mobile-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 56px;
		min-height: 56px;
		padding: 0 12px;
		background: hsl(var(--card));
		border-bottom: 1px solid hsl(var(--border) / 0.8);
		flex-shrink: 0;
		/* Subtle shadow for depth */
		box-shadow: 0 1px 3px hsl(var(--foreground) / 0.05);
	}

	.header-btn {
		/* Apple HIG: minimum 44x44px touch target */
		width: 44px;
		height: 44px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 10px;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
		/* Active state feedback */
		-webkit-tap-highlight-color: transparent;
	}

	.header-btn:hover {
		background: hsl(var(--accent));
	}

	.header-btn:active {
		background: hsl(var(--accent));
		transform: scale(0.95);
	}

	.header-btn:disabled {
		opacity: 0.3;
		cursor: default;
	}

	.header-btn:disabled:active {
		transform: none;
	}

	.header-btn.add:hover {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	.header-btn.add:active {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	.header-btn.close:hover {
		background: hsl(var(--destructive) / 0.1);
		color: hsl(var(--destructive));
	}

	.header-btn.close:active {
		background: hsl(var(--destructive) / 0.15);
		color: hsl(var(--destructive));
	}

	/* Small button - still maintains 44px touch target */
	.header-btn.small {
		width: 44px;
		height: 44px;
		position: relative;
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.header-actions .badge {
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

	.header-center {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		justify-content: center;
		/* Better contrast for icon */
		color: hsl(var(--foreground) / 0.7);
	}

	.header-title {
		font-size: 0.9375rem;
		font-weight: 600;
		/* High contrast foreground */
		color: hsl(var(--foreground));
		max-width: 200px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		/* Slight letter spacing for readability */
		letter-spacing: -0.01em;
	}

	/* Card Container */
	.card-container {
		flex: 1;
		min-height: 0; /* Critical: allows flex child to shrink below content size */
		overflow: hidden;
		position: relative;
		/* Allow normal touch behavior for inputs - swipe is controlled by JS */
		touch-action: auto;
	}

	/* Swipe direction indicators */
	.swipe-indicator {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		width: 40px;
		height: 40px;
		border-radius: 50%;
		background: hsl(var(--primary) / 0.9);
		color: hsl(var(--primary-foreground));
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 10;
		pointer-events: none;
		opacity: 0;
		transition: opacity 0.15s ease;
		box-shadow: 0 2px 8px hsl(var(--foreground) / 0.15);
	}

	.swipe-indicator.left {
		left: 16px;
	}

	.swipe-indicator.right {
		right: 16px;
	}

	.swipe-indicator.visible {
		opacity: 1;
	}

	.card-wrapper {
		height: 100%;
		min-height: 0; /* Allow shrinking in flex context */
		display: flex;
		flex-direction: column;
		transition: transform 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94),
					opacity 0.15s ease;
	}

	.card-wrapper.swiping {
		transition: none;
	}

	/* Dot Indicators - improved touch targets */
	.dot-indicators {
		display: flex;
		justify-content: center;
		gap: 4px;
		padding: 8px 16px;
		padding-bottom: max(12px, env(safe-area-inset-bottom, 12px));
		background: hsl(var(--card));
		border-top: 1px solid hsl(var(--border) / 0.6);
	}

	.dot {
		/* Apple HIG: 44x44px touch target */
		width: 44px;
		height: 44px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		padding: 0;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: transform 0.15s ease;
	}

	.dot:active {
		transform: scale(0.9);
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
		/* Subtle glow for active state */
		box-shadow: 0 0 6px hsl(var(--primary) / 0.4);
	}

	.dot:hover .dot-inner {
		background: hsl(var(--muted-foreground) / 0.5);
	}

	.dot.active:hover .dot-inner {
		background: hsl(var(--primary));
	}

	/* Empty state now uses MobileWelcome component */

	/* Large phone / small tablet portrait */
	@media (min-width: 414px) {
		.header-title {
			max-width: 260px;
		}

		.empty-content {
			margin-top: -3vh;
		}
	}
</style>
