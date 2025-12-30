<script lang="ts">
	/**
	 * DeckLayout - Main layout component for The Deck
	 *
	 * Provides a clean full-screen workspace with:
	 * - Floating dealer button for card navigation (opens CardDeckNavigator)
	 * - All navigation via the card deck navigator overlay
	 *
	 * On mobile (<640px), dealer button moves to bottom center.
	 */

	import { onMount } from 'svelte';
	import { Plus, ChevronsDown } from 'lucide-svelte';

	interface Props {
		onLogoClick?: () => void;
		hasOpenCards?: boolean;
		children?: import('svelte').Snippet;
	}

	let {
		onLogoClick,
		hasOpenCards = false,
		children
	}: Props = $props();

	// On mobile, hide dealer button when cards are open (user can access via header)
	const showDealerButton = $derived(!isMobile || !hasOpenCards);

	// Track mobile state
	let isMobile = $state(false);

	// Hover state for dealer button reveal
	let isDealerVisible = $state(false);
	let dealerTimeout: ReturnType<typeof setTimeout> | null = null;

	function checkMobile() {
		isMobile = window.innerWidth < 640;
	}

	function handleDealerIndicatorEnter() {
		if (dealerTimeout) clearTimeout(dealerTimeout);
		isDealerVisible = true;
	}

	function handleDealerIndicatorLeave() {
		if (dealerTimeout) clearTimeout(dealerTimeout);
		dealerTimeout = setTimeout(() => {
			isDealerVisible = false;
		}, 300);
	}

	function handleDealerButtonEnter() {
		if (dealerTimeout) clearTimeout(dealerTimeout);
		isDealerVisible = true;
	}

	function handleDealerButtonLeave() {
		if (dealerTimeout) clearTimeout(dealerTimeout);
		dealerTimeout = setTimeout(() => {
			isDealerVisible = false;
		}, 300);
	}

	function handleDealerClick() {
		onLogoClick?.();
		// Hide after click
		setTimeout(() => {
			isDealerVisible = false;
		}, 200);
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});
</script>

<div class="deck-layout" class:mobile={isMobile}>
	<!-- Main content area - full width -->
	<main class="workspace-container">
		{#if children}
			{@render children()}
		{:else}
			<div class="workspace-placeholder">
				<p>Workspace content goes here</p>
			</div>
		{/if}

		<!-- AI Shuffle Button - Top left, hidden by default with hover indicator -->
		{#if showDealerButton && !isMobile}
			<div class="dealer-container">
				<!-- Hover indicator - bouncing arrow -->
				<div
					class="dealer-indicator"
					class:hidden={isDealerVisible}
					onmouseenter={handleDealerIndicatorEnter}
					onmouseleave={handleDealerIndicatorLeave}
					role="button"
					tabindex="0"
					aria-label="AI Shuffle"
				>
					<ChevronsDown size={16} strokeWidth={2} class="indicator-arrow" />
				</div>

				<!-- Actual button - revealed on hover -->
				{#if isDealerVisible}
					<button
						class="dealer-button"
						onmouseenter={handleDealerButtonEnter}
						onmouseleave={handleDealerButtonLeave}
						onclick={handleDealerClick}
						title="AI Shuffle"
					>
						<Plus size={20} strokeWidth={2} class="dealer-icon" />
						<span class="dealer-label">AI Shuffle</span>
					</button>
				{/if}
			</div>
		{/if}

		<!-- Mobile: Keep original floating button at bottom center -->
		{#if showDealerButton && isMobile}
			<button
				class="dealer-button-mobile"
				onclick={() => onLogoClick?.()}
				title="AI Shuffle"
			>
				<Plus size={24} strokeWidth={2} class="dealer-icon" />
			</button>
		{/if}
	</main>
</div>

<style>
	.deck-layout {
		display: flex;
		flex-direction: column;
		width: 100%;
		height: 100vh;
		height: 100dvh; /* Dynamic viewport height for mobile browsers */
		background: var(--background);
		overflow: hidden;
	}

	.workspace-container {
		flex: 1;
		overflow: hidden;
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}

	.workspace-placeholder {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}

	/* ===================================
	   DEALER CONTAINER - Top left hover reveal
	   =================================== */

	.dealer-container {
		position: absolute;
		top: 6px;
		left: 16px;
		z-index: 10001;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
	}

	/* Hover indicator - bouncing arrow */
	.dealer-indicator {
		padding: 6px 10px;
		background: rgba(255, 255, 255, 0.04);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: 12px;
		cursor: pointer;
		opacity: 0.5;
		transition: opacity 0.3s ease;
	}

	.dealer-indicator:hover {
		opacity: 0.8;
		background: rgba(255, 255, 255, 0.08);
		border-color: rgba(255, 255, 255, 0.15);
	}

	.dealer-indicator.hidden {
		opacity: 0;
		pointer-events: none;
	}

	.dealer-indicator :global(.indicator-arrow) {
		color: var(--muted-foreground);
		opacity: 0.7;
		animation: bounce 2s ease-in-out infinite;
	}

	@keyframes bounce {
		0%, 100% {
			transform: translateY(0);
			opacity: 0.7;
		}
		50% {
			transform: translateY(3px);
			opacity: 1;
		}
	}

	/* Dealer button - revealed on hover */
	.dealer-button {
		--btn-bg: var(--glass-bg, rgba(255, 255, 255, 0.05));
		--btn-border: var(--glass-border, rgba(255, 255, 255, 0.1));
		--btn-border-hover: #22d3ee;
		--btn-text: var(--muted-foreground);
		--btn-text-hover: #22d3ee;
		--btn-glow: rgba(34, 211, 238, 0.25);

		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 16px;
		background: var(--btn-bg);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid var(--btn-border);
		border-radius: 24px;
		cursor: pointer;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		box-shadow: var(--shadow-m, 0 4px 12px rgba(0, 0, 0, 0.3));
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		animation: fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		margin-top: 4px;
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

	.dealer-button:hover {
		background: var(--card, rgba(255, 255, 255, 0.08));
		border-color: var(--btn-border-hover);
		box-shadow:
			var(--shadow-l, 0 8px 24px rgba(0, 0, 0, 0.4)),
			0 0 20px var(--btn-glow);
		transform: translateY(-1px);
	}

	.dealer-button:active {
		transform: scale(0.96);
	}

	.dealer-button :global(.dealer-icon) {
		color: var(--btn-text);
		transition: all 0.2s ease;
	}

	.dealer-button:hover :global(.dealer-icon) {
		color: var(--btn-text-hover);
		transform: rotate(90deg);
	}

	.dealer-label {
		color: var(--foreground);
		transition: color 0.2s ease;
	}

	.dealer-button:hover .dealer-label {
		color: var(--btn-text-hover);
	}

	/* ===================================
	   MOBILE DEALER BUTTON - Bottom center
	   =================================== */

	.dealer-button-mobile {
		--btn-bg: oklch(0.17 0.01 260);
		--btn-bg-hover: oklch(0.20 0.01 260);
		--btn-border: rgba(255, 255, 255, 0.1);
		--btn-text: #a1a1aa;

		position: absolute;
		bottom: max(20px, env(safe-area-inset-bottom, 20px));
		left: 50%;
		transform: translateX(-50%);
		z-index: 9999;
		width: 52px;
		height: 52px;
		border-radius: 16px;
		border: 1px solid var(--btn-border);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--btn-bg);
		box-shadow:
			0 4px 20px rgba(0, 0, 0, 0.4),
			0 0 0 1px rgba(255, 255, 255, 0.06);
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		-webkit-tap-highlight-color: transparent;
	}

	.dealer-button-mobile:hover {
		background: var(--btn-bg-hover);
		transform: translateX(-50%) translateY(-2px);
	}

	.dealer-button-mobile:active {
		transform: translateX(-50%) scale(0.96);
	}

	.dealer-button-mobile :global(.dealer-icon) {
		color: var(--btn-text);
		transition: all 0.2s ease;
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.dealer-indicator :global(.indicator-arrow) {
			animation: none;
		}
		.dealer-button {
			animation: none;
		}
	}

</style>
