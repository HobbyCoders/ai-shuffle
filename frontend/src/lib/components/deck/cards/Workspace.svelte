<script lang="ts">
	/**
	 * Workspace - Desktop canvas for free-form draggable cards
	 *
	 * Features:
	 * - Contains all cards in the deck
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
	import { WelcomeHero } from '../welcome';

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
		isApiUser?: boolean;
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
		isApiUser = false,
		children
	}: Props = $props();

	// Focus mode navigation state - uses all cards, not filtered
	const isFocusMode = $derived(layoutMode === 'focus');
	const focusModeCards = $derived(
		isFocusMode
			? [...cards].sort(
					(a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime()
				)
			: []
	);
	const focusModeIndex = $derived.by(() => {
		if (focusModeCards.length === 0 || !focusedCardId) return 0;
		const idx = focusModeCards.findIndex((c) => c.id === focusedCardId);
		return idx >= 0 ? idx : 0;
	});
	const focusModeTotal = $derived(focusModeCards.length);

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

	// Sorted by z-index for rendering
	const sortedCards = $derived([...cards].sort((a, b) => a.zIndex - b.zIndex));

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
			focusModeIndex={focusModeIndex}
			focusModeTotal={focusModeTotal}
			onFocusNavigate={onFocusNavigate}
		/>
	{/if}

	<!-- Render cards via children snippet -->
	{#if children}
		{@render children()}
	{/if}

	<!-- Empty state when no cards - Casino Noir Welcome -->
	{#if sortedCards.length === 0}
		<WelcomeHero {onCreateCard} {isApiUser} />
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
</style>
