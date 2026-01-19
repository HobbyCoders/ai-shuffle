<script lang="ts">
	/**
	 * MobileWorkspace - Full-screen stacked cards for mobile devices
	 *
	 * Features:
	 * - Swipeable card stack (touch gestures)
	 * - Velocity-based animation
	 * - Minimal card header (plus, title, close)
	 * - Empty state with create buttons
	 */

	import { ChevronLeft, X, MessageSquare, Plus, Settings, User, Users, FolderKanban, FolderOpen, Puzzle, StickyNote, Zap, BarChart3, Mic } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import type { Snippet } from 'svelte';
	import { MobileWelcome } from '../welcome';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';

	interface Props {
		cards: DeckCard[];
		activeCardIndex: number;
		onCardChange: (index: number) => void;
		onCloseCard: (id: string) => void;
		onCreateCard: (type: string) => void;
		onOpenNavigator?: () => void;
		isApiUser?: boolean;
		children?: Snippet<[DeckCard]>;
	}

	let {
		cards,
		activeCardIndex,
		onCardChange,
		onCloseCard,
		onCreateCard,
		onOpenNavigator,
		isApiUser = false,
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

	const SWIPE_THRESHOLD = 80; // Minimum distance to trigger swipe
	const VELOCITY_THRESHOLD = 0.5; // Require more intentional swipes (px/ms)
	const DIRECTION_LOCK_THRESHOLD = 10;
	const ANIMATION_DURATION = 300; // Match CSS transition duration

	// Icon mapping for card types
	const cardIcons: Record<CardType, typeof MessageSquare> = {
		chat: MessageSquare,
		settings: Settings,
		profile: User,
		subagent: Users,
		project: FolderKanban,
		'user-settings': Settings,
		'file-browser': FolderOpen,
		plugins: Puzzle,
		notes: StickyNote,
		hooks: Zap,
		stats: BarChart3,
	};

	// Current card
	const activeCard = $derived(cards[activeCardIndex]);

	// Swipe offset for animation
	const swipeOffset = $derived(isSwiping && swipeDirection === 'horizontal' ? touchCurrentX - touchStartX : 0);

	// Swipe progress for visual feedback (0 to 1, capped)
	const swipeProgress = $derived(Math.min(Math.abs(swipeOffset) / (SWIPE_THRESHOLD * 2), 1));

	// Direction indicator for swipe feedback
	const swipeIndicatorDirection = $derived(swipeOffset > 0 ? 'left' : 'right');

	// Check if element or its ancestors have horizontal scroll capability
	function hasHorizontalScroll(el: HTMLElement | null): boolean {
		while (el) {
			const style = getComputedStyle(el);
			const overflowX = style.overflowX;

			// Check if element has horizontal scroll enabled and has scrollable content
			if (
				(overflowX === 'scroll' || overflowX === 'auto') &&
				el.scrollWidth > el.clientWidth
			) {
				return true;
			}
			el = el.parentElement;
		}
		return false;
	}

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
		if (interactive !== null) return true;
		// Check if element has horizontal scroll (e.g., code blocks, tables)
		if (hasHorizontalScroll(target)) return true;
		return false;
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

	// Close current card
	function handleClose() {
		if (activeCard) {
			onCloseCard(activeCard.id);
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="mobile-workspace">
	{#if cards.length === 0}
		<!-- Mobile Welcome Screen with Carousel -->
		<MobileWelcome {onCreateCard} {isApiUser} />
	{:else}
		<!-- Mobile Header -->
		<header class="mobile-header">
			<div class="header-actions left">
				{#if onOpenNavigator}
					<button
						class="header-btn add"
						onclick={onOpenNavigator}
						aria-label="Open card navigator"
					>
						<Plus size={20} />
					</button>
				{/if}
				{#if $hotkeyPreferences.globalSttEnabled}
					<button
						class="header-btn mic"
						class:recording={$globalSttState.isRecording}
						class:transcribing={$globalSttState.isTranscribing}
						class:preparing={$globalSttState.isPreparing}
						onclick={triggerToggle}
						aria-label={$globalSttState.isRecording ? 'Stop recording' : 'Start voice input'}
					>
						<Mic size={20} />
					</button>
				{/if}
			</div>

			<div class="header-center">
				{#if activeCard}
					{@const Icon = cardIcons[activeCard.type]}
					<Icon size={16} />
					<span class="header-title">{activeCard.title}</span>
				{/if}
			</div>

			<div class="header-actions">
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
		border-radius: var(--radius, 0.75rem);
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
		/* Active state feedback */
		-webkit-tap-highlight-color: transparent;
	}

	.header-btn:hover {
		background: var(--hover-overlay);
	}

	.header-btn:active {
		background: var(--hover-overlay);
		transform: scale(0.95);
	}

	.header-btn:disabled {
		opacity: 0.3;
		cursor: default;
	}

	.header-btn:disabled:active {
		transform: none;
	}

	/* Primary-colored buttons (add, mic) */
	.header-btn.add:hover,
	.header-btn.mic:hover {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	.header-btn.add:active,
	.header-btn.mic:active {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	/* Destructive-colored button (close) */
	.header-btn.close:hover {
		background: hsl(var(--destructive) / 0.1);
		color: hsl(var(--destructive));
	}

	.header-btn.close:active {
		background: hsl(var(--destructive) / 0.15);
		color: hsl(var(--destructive));
	}

	/* Mic button base state - muted until interacted */
	.header-btn.mic {
		color: hsl(var(--muted-foreground));
	}

	/* Recording state - red pulsing */
	.header-btn.mic.recording {
		background: hsl(var(--destructive) / 0.15);
		color: hsl(var(--destructive));
		animation: pulse-recording 1.5s ease-in-out infinite;
	}

	/* Prevent transform conflict between :active and animation */
	.header-btn.mic.recording:active {
		transform: scale(1.05);
	}

	/* Transcribing state - primary color */
	.header-btn.mic.transcribing {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
		animation: pulse-transcribing 0.8s ease-in-out infinite;
	}

	/* Preparing state - subtle pulse */
	.header-btn.mic.preparing {
		background: hsl(var(--muted) / 0.5);
		color: hsl(var(--muted-foreground));
		animation: pulse-transcribing 1s ease-in-out infinite;
	}

	@keyframes pulse-recording {
		0%, 100% {
			transform: scale(1);
			box-shadow: 0 0 0 0 hsl(var(--destructive) / 0.4);
		}
		50% {
			transform: scale(1.05);
			box-shadow: 0 0 0 4px hsl(var(--destructive) / 0);
		}
	}

	@keyframes pulse-transcribing {
		0%, 100% {
			opacity: 1;
		}
		50% {
			opacity: 0.6;
		}
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
		min-width: 44px; /* Ensure symmetry with left side */
	}

	.header-actions.left {
		justify-content: flex-start;
	}

	.header-actions .badge {
		position: absolute;
		top: 4px;
		right: 4px;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		background: hsl(var(--primary));
		border-radius: var(--radius, 0.75rem);
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
