<script lang="ts">
	/**
	 * DeckLayout - Main layout component for The Deck
	 *
	 * Provides a clean full-screen workspace with:
	 * - Floating dealer button for card navigation (opens CardDeckNavigator)
	 * - Card layout mode selector
	 * - All navigation via the card deck navigator overlay
	 *
	 * On mobile (<640px), dealer button moves to bottom left.
	 */

	import { onMount } from 'svelte';
	import {
		Plus,
		ChevronDown,
		Move,
		Columns,
		LayoutGrid,
		Layers,
		Maximize2
	} from 'lucide-svelte';
	import type { LayoutMode } from '$lib/stores/deck';

	interface Props {
		onLogoClick?: () => void;
		hasOpenCards?: boolean;
		layoutMode?: LayoutMode;
		onLayoutModeChange?: (mode: LayoutMode) => void;
		children?: import('svelte').Snippet;
	}

	let {
		onLogoClick,
		hasOpenCards = false,
		layoutMode = 'freeflow',
		onLayoutModeChange,
		children
	}: Props = $props();

	// Layout mode options
	const layoutModes: { mode: LayoutMode; label: string; icon: typeof Move }[] = [
		{ mode: 'freeflow', label: 'Free Flow', icon: Move },
		{ mode: 'sidebyside', label: 'Side by Side', icon: Columns },
		{ mode: 'tile', label: 'Tile', icon: LayoutGrid },
		{ mode: 'stack', label: 'Stack', icon: Layers },
		{ mode: 'focus', label: 'Focus', icon: Maximize2 }
	];

	const currentModeInfo = $derived(layoutModes.find((m) => m.mode === layoutMode) || layoutModes[0]);

	// Track mobile state (must be declared before use in derived)
	let isMobile = $state(false);

	// On mobile, hide dealer button when cards are open (user can access via header)
	const showDealerButton = $derived(!isMobile || !hasOpenCards);

	// Hover state for menu reveal
	let isMenuVisible = $state(false);
	let menuTimeout: ReturnType<typeof setTimeout> | null = null;

	// Layout panel expanded state
	let isLayoutPanelOpen = $state(false);

	function checkMobile() {
		// Match the detection logic from +page.svelte
		// pointer:coarse = touch device, width < 1024 excludes laptops with touchscreens
		// (hover:none is broken on Samsung, any-pointer:fine broken due to Flex Mode)
		const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;
		const isReasonableMobileSize = window.innerWidth < 1024;
		isMobile = window.innerWidth < 640 || (isTouchDevice && isReasonableMobileSize);
	}

	function handleEdgeTriggerEnter() {
		if (menuTimeout) clearTimeout(menuTimeout);
		isMenuVisible = true;
	}

	function handleEdgeTriggerLeave() {
		if (menuTimeout) clearTimeout(menuTimeout);
		// Don't hide if layout panel is open
		if (!isLayoutPanelOpen) {
			menuTimeout = setTimeout(() => {
				isMenuVisible = false;
			}, 300);
		}
	}

	function handleMenuEnter() {
		if (menuTimeout) clearTimeout(menuTimeout);
		isMenuVisible = true;
	}

	function handleMenuLeave() {
		if (menuTimeout) clearTimeout(menuTimeout);
		// Don't hide if layout panel is open
		if (!isLayoutPanelOpen) {
			menuTimeout = setTimeout(() => {
				isMenuVisible = false;
				isLayoutPanelOpen = false;
			}, 300);
		}
	}

	function handleDealerClick() {
		onLogoClick?.();
		// Hide menu after click
		setTimeout(() => {
			isMenuVisible = false;
			isLayoutPanelOpen = false;
		}, 200);
	}

	function handleLayoutButtonClick() {
		isLayoutPanelOpen = !isLayoutPanelOpen;
	}

	function handleLayoutModeSelect(mode: LayoutMode) {
		onLayoutModeChange?.(mode);
		// Close panel after selection
		setTimeout(() => {
			isLayoutPanelOpen = false;
			isMenuVisible = false;
		}, 200);
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape' && (isMenuVisible || isLayoutPanelOpen)) {
			isMenuVisible = false;
			isLayoutPanelOpen = false;
		}
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});
</script>

<svelte:window on:keydown={handleKeyDown} />

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

		<!-- Left edge hover trigger zone - invisible, triggers menu on hover -->
		{#if showDealerButton && !isMobile}
			<div
				class="left-edge-trigger"
				onmouseenter={handleEdgeTriggerEnter}
				onmouseleave={handleEdgeTriggerLeave}
			></div>

			<!-- Menu Container - Left edge, vertically centered -->
			<div
				class="menu-container"
				class:visible={isMenuVisible}
				onmouseenter={handleMenuEnter}
				onmouseleave={handleMenuLeave}
			>
				<!-- Card Layout Button -->
				{#if hasOpenCards}
					<button
						class="menu-button"
						class:expanded={isLayoutPanelOpen}
						onclick={handleLayoutButtonClick}
						title="Card Layout"
					>
						<svelte:component this={currentModeInfo.icon} size={18} strokeWidth={2} class="menu-icon" />
						<span class="menu-label">{currentModeInfo.label}</span>
						<ChevronDown
							size={14}
							strokeWidth={2}
							class="chevron-icon {isLayoutPanelOpen ? 'rotated' : ''}"
						/>
					</button>

					<!-- Layout Mode Panel - between Card Layout and New Card -->
					{#if isLayoutPanelOpen}
						<div class="layout-panel" role="menu">
							{#each layoutModes as { mode, label, icon: Icon }}
								<button
									class="layout-option"
									class:active={layoutMode === mode}
									onclick={() => handleLayoutModeSelect(mode)}
									role="menuitem"
								>
									<Icon size={16} strokeWidth={1.5} />
									<span>{label}</span>
									{#if layoutMode === mode}
										<div class="check-mark">
											<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
												<path d="M20 6L9 17l-5-5" />
											</svg>
										</div>
									{/if}
								</button>
							{/each}
						</div>
					{/if}
				{/if}

				<!-- New Card Button -->
				<button
					class="menu-button"
					onclick={handleDealerClick}
					title="New Card"
				>
					<Plus size={18} strokeWidth={2} class="menu-icon" />
					<span class="menu-label">New Card</span>
				</button>
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
	   LEFT EDGE HOVER TRIGGER
	   =================================== */

	.left-edge-trigger {
		position: absolute;
		left: 0;
		top: 0;
		width: 24px;
		height: 100%;
		z-index: 50001; /* Above menu container */
		/* Invisible but captures hover events */
	}

	/* ===================================
	   MENU CONTAINER - Left edge, vertically centered
	   =================================== */

	.menu-container {
		position: absolute;
		left: 12px;
		top: 50%;
		transform: translateY(-50%) translateX(-100%);
		z-index: 50000; /* Above all cards and overlays */
		display: flex;
		flex-direction: column;
		gap: 6px;
		opacity: 0;
		pointer-events: none;
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
	}

	.menu-container.visible {
		opacity: 1;
		pointer-events: auto;
		transform: translateY(-50%) translateX(0);
	}

	/* Individual menu button */
	.menu-button {
		--btn-bg: var(--glass-bg, rgba(255, 255, 255, 0.05));
		--btn-border: var(--glass-border, rgba(255, 255, 255, 0.1));
		--btn-border-hover: var(--primary);
		--btn-text: var(--muted-foreground);
		--btn-text-hover: var(--primary);
		--btn-glow: color-mix(in srgb, var(--primary) 25%, transparent);

		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 14px;
		background: var(--btn-bg);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid var(--btn-border);
		border-radius: 20px;
		cursor: pointer;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		box-shadow: var(--shadow-m, 0 4px 12px rgba(0, 0, 0, 0.3));
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		white-space: nowrap;
	}

	.menu-button:hover {
		background: var(--card, rgba(255, 255, 255, 0.08));
		border-color: var(--btn-border-hover);
		box-shadow:
			var(--shadow-l, 0 8px 24px rgba(0, 0, 0, 0.4)),
			0 0 20px var(--btn-glow);
		transform: translateX(4px);
	}

	.menu-button.expanded {
		background: var(--card, rgba(255, 255, 255, 0.08));
		border-color: var(--primary);
	}

	.menu-button:active {
		transform: scale(0.98);
	}

	.menu-button :global(.menu-icon) {
		color: var(--btn-text);
		transition: all 0.2s ease;
		flex-shrink: 0;
	}

	.menu-button:hover :global(.menu-icon) {
		color: var(--btn-text-hover);
	}

	.menu-label {
		color: var(--foreground);
		transition: color 0.2s ease;
	}

	.menu-button:hover .menu-label {
		color: var(--btn-text-hover);
	}

	.menu-button :global(.chevron-icon) {
		color: var(--muted-foreground);
		margin-left: auto;
		transition: transform 0.2s ease;
	}

	.menu-button :global(.chevron-icon.rotated) {
		transform: rotate(180deg);
	}

	/* ===================================
	   LAYOUT PANEL - Dropdown for layout modes
	   =================================== */

	.layout-panel {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 6px;
		background: var(--popover, rgba(20, 20, 25, 0.95));
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--border, rgba(255, 255, 255, 0.1));
		border-radius: 12px;
		box-shadow: var(--shadow-l, 0 8px 32px rgba(0, 0, 0, 0.4));
		min-width: 160px;
		animation: slideUp 0.15s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes slideUp {
		from {
			opacity: 0;
			transform: translateY(8px) scale(0.96);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.layout-option {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.layout-option:hover {
		background: var(--hover-overlay);
	}

	.layout-option.active {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.layout-option :global(svg) {
		flex-shrink: 0;
		opacity: 0.7;
	}

	.layout-option.active :global(svg) {
		opacity: 1;
	}

	.layout-option span {
		flex: 1;
	}

	.check-mark {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		background: var(--primary);
		border-radius: 50%;
		color: var(--primary-foreground);
	}

	/* ===================================
	   MOBILE DEALER BUTTON - Bottom left
	   =================================== */

	.dealer-button-mobile {
		position: absolute;
		bottom: max(20px, env(safe-area-inset-bottom, 20px));
		left: max(16px, env(safe-area-inset-left, 16px));
		z-index: 9999;
		width: 52px;
		height: 52px;
		border-radius: 16px;
		border: 1px solid var(--border);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--card);
		box-shadow: var(--shadow-m);
		transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		-webkit-tap-highlight-color: transparent;
	}

	.dealer-button-mobile:hover {
		border-color: var(--accent);
		transform: translateY(-2px);
		box-shadow: var(--shadow-l), 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
	}

	.dealer-button-mobile:active {
		transform: scale(0.96);
	}

	.dealer-button-mobile :global(.dealer-icon) {
		color: var(--muted-foreground);
		transition: all 0.2s ease;
	}

	.dealer-button-mobile:hover :global(.dealer-icon) {
		color: var(--foreground);
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.menu-container {
			transition: opacity 0.1s ease;
		}
		.menu-container.visible {
			transform: translateY(-50%);
		}
		.layout-panel {
			animation: none;
		}
	}

</style>
