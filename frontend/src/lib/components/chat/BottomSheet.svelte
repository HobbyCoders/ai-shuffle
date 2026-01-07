<script lang="ts">
	/**
	 * BottomSheet - Mobile-optimized selection UI
	 *
	 * Features:
	 * - Slides up from bottom of screen
	 * - Dark backdrop with blur
	 * - Rounded top corners
	 * - Drag handle (pill shape)
	 * - Close button in header
	 * - Maximum height of 80vh
	 * - Scrollable content area
	 * - Touch-friendly tap targets (min 44px)
	 * - Prevents body scroll when open
	 * - Close on backdrop tap or Escape key
	 * - Safe area padding for iOS notch
	 */

	import type { Snippet } from 'svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
		title: string;
		children: Snippet;
	}

	let {
		open,
		onClose,
		title,
		children
	}: Props = $props();

	// Track if user is swiping down
	let swipeStartY = $state(0);
	let swipeCurrentY = $state(0);
	let isSwiping = $state(false);
	let sheetElement: HTMLDivElement | undefined = $state();

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			onClose();
		}
	}

	// Prevent body scroll when open
	$effect(() => {
		if (open) {
			const originalStyle = window.getComputedStyle(document.body).overflow;
			document.body.style.overflow = 'hidden';

			return () => {
				document.body.style.overflow = originalStyle;
			};
		}
	});

	// Touch/swipe handling for drag-to-close
	function handleSwipeStart(e: TouchEvent | PointerEvent) {
		const y = 'touches' in e ? e.touches[0].clientY : e.clientY;
		swipeStartY = y;
		swipeCurrentY = y;
		isSwiping = true;
	}

	function handleSwipeMove(e: TouchEvent | PointerEvent) {
		if (!isSwiping) return;

		const y = 'touches' in e ? e.touches[0].clientY : e.clientY;
		swipeCurrentY = y;

		// Only allow downward swipes
		const deltaY = swipeCurrentY - swipeStartY;
		if (deltaY > 0 && sheetElement) {
			sheetElement.style.transform = `translateY(${deltaY}px)`;
		}
	}

	function handleSwipeEnd() {
		if (!isSwiping) return;

		const deltaY = swipeCurrentY - swipeStartY;
		const threshold = 100; // pixels to swipe down before closing

		if (deltaY > threshold) {
			onClose();
		}

		// Reset
		if (sheetElement) {
			sheetElement.style.transform = '';
		}
		isSwiping = false;
		swipeStartY = 0;
		swipeCurrentY = 0;
	}

	function handleBackdropClick() {
		onClose();
	}

	function handleSheetClick(e: MouseEvent) {
		e.stopPropagation();
	}
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-50 flex items-end justify-center"
		role="dialog"
		aria-modal="true"
		aria-labelledby="bottom-sheet-title"
	>
		<!-- Dark overlay -->
		<button
			class="absolute inset-0 bg-black/60 backdrop-blur-sm animate-fade-in"
			onclick={handleBackdropClick}
			aria-label="Close"
			tabindex="-1"
		></button>

		<!-- Bottom Sheet -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			bind:this={sheetElement}
			class="
				relative w-full max-w-2xl
				bg-card border-t border-l border-r border-border
				rounded-t-3xl
				shadow-2xl
				flex flex-col
				max-h-[80vh]
				animate-slide-up
				pb-[env(safe-area-inset-bottom,0px)]
			"
			onclick={handleSheetClick}
			ontouchstart={handleSwipeStart}
			ontouchmove={handleSwipeMove}
			ontouchend={handleSwipeEnd}
		>
			<!-- Drag Handle -->
			<div class="flex justify-center pt-3 pb-2">
				<div class="w-12 h-1.5 bg-muted-foreground/30 rounded-full"></div>
			</div>

			<!-- Header -->
			<header class="shrink-0 px-6 py-3 flex items-center justify-between">
				<h2 id="bottom-sheet-title" class="text-lg font-semibold text-foreground">
					{title}
				</h2>

				<!-- Close button -->
				<button
					onclick={onClose}
					class="
						p-2 -mr-2 rounded-lg
						text-muted-foreground hover:text-foreground
						hover:bg-accent
						transition-colors
						touch-manipulation
					"
					aria-label="Close"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</header>

			<!-- Divider -->
			<div class="border-b border-border"></div>

			<!-- Content Area - scrollable -->
			<div class="flex-1 overflow-y-auto overflow-x-hidden">
				<div class="p-4">
					{@render children()}
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	/* Fade in animation for backdrop */
	@keyframes fade-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	.animate-fade-in {
		animation: fade-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	/* Slide up animation for sheet */
	@keyframes slide-up {
		from {
			opacity: 0;
			transform: translateY(100%);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.animate-slide-up {
		animation: slide-up 300ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	/* Smooth scrolling for iOS */
	.overflow-y-auto {
		-webkit-overflow-scrolling: touch;
	}

	/* Touch-friendly scrollbar */
	.overflow-y-auto::-webkit-scrollbar {
		width: 8px;
	}

	.overflow-y-auto::-webkit-scrollbar-track {
		background: transparent;
	}

	.overflow-y-auto::-webkit-scrollbar-thumb {
		background: color-mix(in oklch, var(--muted-foreground) 30%, transparent);
		border-radius: 4px;
	}

	.overflow-y-auto::-webkit-scrollbar-thumb:hover {
		background: color-mix(in oklch, var(--muted-foreground) 50%, transparent);
	}

	/* Ensure touch targets are at least 44px high */
	:global(.bottom-sheet-option) {
		min-height: 44px;
		display: flex;
		align-items: center;
		padding: 12px 16px;
		cursor: pointer;
		transition: background-color 0.15s ease;
		border-radius: 8px;
		-webkit-tap-highlight-color: transparent;
	}

	:global(.bottom-sheet-option:hover),
	:global(.bottom-sheet-option:active) {
		background: var(--accent);
	}

	/* Style for selected items */
	:global(.bottom-sheet-option[data-selected="true"]) {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	/* Touch manipulation for better mobile performance */
	.touch-manipulation {
		touch-action: manipulation;
	}
</style>
