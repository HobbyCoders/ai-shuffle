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
	import { Plus } from 'lucide-svelte';

	interface Props {
		onLogoClick?: () => void;
		children?: import('svelte').Snippet;
	}

	let {
		onLogoClick,
		children
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

		<!-- Floating Dealer Button -->
		<button
			class="dealer-button"
			class:mobile={isMobile}
			onclick={() => onLogoClick?.()}
			title="AI Shuffle"
		>
			<Plus size={24} strokeWidth={2} class="dealer-icon" />
			{#if !isMobile}
				<span class="tooltip">AI Shuffle</span>
			{/if}
		</button>
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
	   FLOATING DEALER BUTTON (AI Shuffle Style)
	   Clean rounded rectangle with + icon
	   =================================== */

	.dealer-button {
		--btn-bg: oklch(0.17 0.01 260);
		--btn-bg-hover: oklch(0.20 0.01 260);
		--btn-border: rgba(255, 255, 255, 0.1);
		--btn-border-hover: #22d3ee;
		--btn-text: #a1a1aa;
		--btn-text-hover: #22d3ee;
		--btn-glow: rgba(34, 211, 238, 0.25);

		position: absolute;
		bottom: 28px;
		left: 28px;
		z-index: 9999;
		width: 56px;
		height: 56px;
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

	.dealer-button:hover {
		background: var(--btn-bg-hover);
		border-color: var(--btn-border-hover);
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.5),
			0 0 20px var(--btn-glow);
		transform: translateY(-2px);
	}

	.dealer-button:active {
		transform: translateY(0) scale(0.96);
	}

	.dealer-button :global(.dealer-icon) {
		color: var(--btn-text);
		transition: all 0.2s ease;
	}

	.dealer-button:hover :global(.dealer-icon) {
		color: var(--btn-text-hover);
		transform: rotate(90deg);
	}

	/* Tooltip */
	.dealer-button .tooltip {
		position: absolute;
		left: calc(100% + 12px);
		top: 50%;
		transform: translateY(-50%) translateX(-4px);
		padding: 8px 14px;
		background: oklch(0.13 0.01 260 / 0.95);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 8px;
		font-size: 0.75rem;
		font-weight: 500;
		color: #f4f4f5;
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.2s ease;
		pointer-events: none;
		box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
	}

	.dealer-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
		transform: translateY(-50%) translateX(0);
	}

	/* Mobile dealer button - bottom center */
	.dealer-button.mobile {
		bottom: max(20px, env(safe-area-inset-bottom, 20px));
		left: 50%;
		transform: translateX(-50%);
		width: 52px;
		height: 52px;
	}

	.dealer-button.mobile:hover {
		transform: translateX(-50%) translateY(-2px);
	}

	.dealer-button.mobile:active {
		transform: translateX(-50%) scale(0.96);
	}

	/* Accessibility: Reduced motion support */
	@media (prefers-reduced-motion: reduce) {
		.dealer-button {
			transition: none;
		}
	}
</style>
