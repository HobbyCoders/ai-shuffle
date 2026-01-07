<script lang="ts">
	/**
	 * CardShuffle - Focus mode navigation for card arrangement
	 *
	 * Only shows when in Focus mode with multiple cards on desktop.
	 * Hidden on mobile (<640px).
	 * Displays page navigation controls (prev/next, current/total).
	 * Layout mode selection has been moved to DeckLayout menu.
	 */

	import { onMount } from 'svelte';
	import { ChevronLeft, ChevronRight } from 'lucide-svelte';
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

	// Track mobile state
	let isMobile = $state(false);

	function checkMobile() {
		isMobile = window.innerWidth < 640;
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});

	// Only show when in focus mode with multiple cards AND not on mobile
	const showFocusNav = $derived(currentMode === 'focus' && focusModeTotal > 1 && !isMobile);

	// Handle focus navigation button click
	function handleFocusNavClick(e: MouseEvent, direction: 'prev' | 'next') {
		e.stopPropagation();
		onFocusNavigate?.(direction);
	}
</script>

<!-- Only render when in focus mode with multiple cards -->
{#if showFocusNav}
	<div
		class="focus-nav-container"
		role="navigation"
		aria-label="Card navigation"
	>
		<div class="focus-nav">
			<button
				class="focus-nav-btn"
				onclick={(e) => handleFocusNavClick(e, 'prev')}
				title="Previous card (←)"
				aria-label="Previous card"
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
				aria-label="Next card"
			>
				<ChevronRight size={18} strokeWidth={2} />
			</button>
		</div>
	</div>
{/if}

<style>
	.focus-nav-container {
		position: absolute;
		top: 6px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10001;
		animation: fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes fadeIn {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0);
		}
	}

	/* Focus Mode Navigation */
	.focus-nav {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 8px;
		background: var(--glass-bg, rgba(255, 255, 255, 0.05));
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.1));
		border-radius: 24px;
		box-shadow: var(--shadow-m, 0 4px 12px rgba(0, 0, 0, 0.3));
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
		background: var(--accent, rgba(255, 255, 255, 0.1));
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
		color: var(--primary, #22d3ee);
		font-weight: 600;
	}

	.focus-nav-separator {
		color: var(--muted-foreground);
	}

	.focus-nav-total {
		color: var(--muted-foreground);
	}

	@media (prefers-reduced-motion: reduce) {
		.focus-nav-container {
			animation: none;
		}
	}
</style>
