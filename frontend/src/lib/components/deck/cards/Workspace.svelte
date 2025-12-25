<script lang="ts">
	/**
	 * Workspace - Desktop canvas for free-form draggable cards
	 *
	 * Features:
	 * - Contains all visible (non-minimized) cards
	 * - Shows snap preview guides when dragging near edges
	 * - Empty state with welcome message and create buttons
	 * - Right-click context menu with create options
	 * - Tracks workspace bounds
	 * - Card Shuffle UI for layout mode switching
	 */

	import { onMount } from 'svelte';
	import { MessageSquare, Bot, Palette, Terminal } from 'lucide-svelte';
	import type { DeckCard, CardType, SnapGuide, SnapResult } from './types';
	import { SNAP_THRESHOLD, CARD_SNAP_THRESHOLD, SNAP_GRID, WORKSPACE_PADDING } from './types';
	import type { Snippet } from 'svelte';
	import type { LayoutMode } from '$lib/stores/deck';
	import CardShuffle from './CardShuffle.svelte';

	interface Props {
		cards: DeckCard[];
		onCardFocus: (id: string) => void;
		onCardMove: (id: string, x: number, y: number) => void;
		onCardResize: (id: string, width: number, height: number) => void;
		onCardSnap: (id: string, snapTo: DeckCard['snappedTo']) => void;
		onCreateCard: (type: CardType) => void;
		gridSnapEnabled?: boolean;
		cardSnapEnabled?: boolean;
		layoutMode?: LayoutMode;
		onLayoutModeChange?: (mode: LayoutMode) => void;
		onFocusNavigate?: (direction: 'prev' | 'next') => void;
		focusedCardId?: string | null;
		children?: Snippet;
	}

	let {
		cards,
		onCardFocus,
		onCardMove,
		onCardResize,
		onCardSnap,
		onCreateCard,
		gridSnapEnabled = false,
		cardSnapEnabled = true,
		layoutMode = 'freeflow',
		onLayoutModeChange,
		onFocusNavigate,
		focusedCardId = null,
		children
	}: Props = $props();

	// In freeflow mode, disable all snapping
	const isSnappingEnabled = $derived(layoutMode !== 'freeflow');

	// Focus mode navigation state
	const isFocusMode = $derived(layoutMode === 'focus');
	const focusModeCards = $derived(() => {
		if (!isFocusMode) return [];
		return [...visibleCards].sort(
			(a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime()
		);
	});
	const focusModeIndex = $derived(() => {
		const cards = focusModeCards();
		if (cards.length === 0 || !focusedCardId) return 0;
		const idx = cards.findIndex((c) => c.id === focusedCardId);
		return idx >= 0 ? idx : 0;
	});
	const focusModeTotal = $derived(() => focusModeCards().length);

	// Handle layout mode change
	function handleLayoutModeChange(mode: LayoutMode) {
		onLayoutModeChange?.(mode);
	}

	let workspaceEl: HTMLDivElement | undefined = $state();
	let workspaceBounds = $state({ width: 0, height: 0 });

	// Snap preview state
	let snapPreview = $state<{
		show: boolean;
		x: number;
		y: number;
		width: number;
		height: number;
	}>({ show: false, x: 0, y: 0, width: 0, height: 0 });

	// Context menu state
	let contextMenu = $state<{
		show: boolean;
		x: number;
		y: number;
	}>({ show: false, x: 0, y: 0 });

	// Card-to-card snap guides state
	let snapGuides = $state<SnapGuide[]>([]);

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

	// Visible cards (not minimized)
	const visibleCards = $derived(cards.filter((c) => !c.minimized));

	// Sorted by z-index for rendering
	const sortedCards = $derived([...visibleCards].sort((a, b) => a.zIndex - b.zIndex));

	// Check if there's a maximized card
	const maximizedCard = $derived(cards.find((c) => c.maximized));
	const hasMaximizedCard = $derived(!!maximizedCard);

	// Snap detection helper - only active when snapping is enabled (non-freeflow modes)
	function getSnapZone(x: number, y: number, width: number, height: number): DeckCard['snappedTo'] | undefined {
		// In freeflow mode, no edge snapping
		if (!isSnappingEnabled) return undefined;

		const bounds = workspaceBounds;
		const threshold = SNAP_THRESHOLD;

		// Check corners first
		if (x < threshold && y < threshold) return 'topleft';
		if (x + width > bounds.width - threshold && y < threshold) return 'topright';
		if (x < threshold && y + height > bounds.height - threshold) return 'bottomleft';
		if (x + width > bounds.width - threshold && y + height > bounds.height - threshold) return 'bottomright';

		// Check edges
		if (x < threshold) return 'left';
		if (x + width > bounds.width - threshold) return 'right';
		if (y < threshold) return 'top';
		if (y + height > bounds.height - threshold) return 'bottom';

		return undefined;
	}

	// Get snap preview geometry
	function getSnapGeometry(snapTo: DeckCard['snappedTo']): { x: number; y: number; width: number; height: number } {
		const bounds = workspaceBounds;
		const halfWidth = bounds.width / 2;
		const halfHeight = bounds.height / 2;

		switch (snapTo) {
			case 'left':
				return { x: 0, y: 0, width: halfWidth, height: bounds.height };
			case 'right':
				return { x: halfWidth, y: 0, width: halfWidth, height: bounds.height };
			case 'top':
				return { x: 0, y: 0, width: bounds.width, height: halfHeight };
			case 'bottom':
				return { x: 0, y: halfHeight, width: bounds.width, height: halfHeight };
			case 'topleft':
				return { x: 0, y: 0, width: halfWidth, height: halfHeight };
			case 'topright':
				return { x: halfWidth, y: 0, width: halfWidth, height: halfHeight };
			case 'bottomleft':
				return { x: 0, y: halfHeight, width: halfWidth, height: halfHeight };
			case 'bottomright':
				return { x: halfWidth, y: halfHeight, width: halfWidth, height: halfHeight };
			default:
				return { x: 0, y: 0, width: 0, height: 0 };
		}
	}

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

	/**
	 * Apply grid snapping to position
	 */
	export function snapToGrid(x: number, y: number): { x: number; y: number } {
		if (!gridSnapEnabled) return { x, y };

		return {
			x: Math.round(x / SNAP_GRID) * SNAP_GRID,
			y: Math.round(y / SNAP_GRID) * SNAP_GRID
		};
	}

	/**
	 * Check for card-to-card snapping alignment
	 * Returns adjusted position and snap guides to display
	 * Disabled in freeflow mode
	 */
	export function checkCardSnapping(
		cardId: string,
		x: number,
		y: number,
		width: number,
		height: number
	): SnapResult {
		// In freeflow mode, no card-to-card snapping
		if (!cardSnapEnabled || !isSnappingEnabled) {
			return { x, y, guides: [] };
		}

		const otherCards = visibleCards.filter(c => c.id !== cardId && !c.maximized);
		const guides: SnapGuide[] = [];
		let snappedX = x;
		let snappedY = y;

		// Card edges
		const cardLeft = x;
		const cardRight = x + width;
		const cardTop = y;
		const cardBottom = y + height;
		const cardCenterX = x + width / 2;
		const cardCenterY = y + height / 2;

		// Check against each other card
		for (const other of otherCards) {
			const otherLeft = other.x;
			const otherRight = other.x + other.width;
			const otherTop = other.y;
			const otherBottom = other.y + other.height;
			const otherCenterX = other.x + other.width / 2;
			const otherCenterY = other.y + other.height / 2;

			// Vertical alignments (x-axis snapping)
			// Left edge to left edge
			if (Math.abs(cardLeft - otherLeft) < CARD_SNAP_THRESHOLD) {
				snappedX = otherLeft;
				guides.push({
					type: 'vertical',
					position: otherLeft,
					start: Math.min(cardTop, otherTop),
					end: Math.max(cardBottom, otherBottom)
				});
			}
			// Right edge to right edge
			else if (Math.abs(cardRight - otherRight) < CARD_SNAP_THRESHOLD) {
				snappedX = otherRight - width;
				guides.push({
					type: 'vertical',
					position: otherRight,
					start: Math.min(cardTop, otherTop),
					end: Math.max(cardBottom, otherBottom)
				});
			}
			// Left edge to right edge (side-by-side)
			else if (Math.abs(cardLeft - otherRight) < CARD_SNAP_THRESHOLD) {
				snappedX = otherRight;
				guides.push({
					type: 'vertical',
					position: otherRight,
					start: Math.min(cardTop, otherTop),
					end: Math.max(cardBottom, otherBottom)
				});
			}
			// Right edge to left edge (side-by-side)
			else if (Math.abs(cardRight - otherLeft) < CARD_SNAP_THRESHOLD) {
				snappedX = otherLeft - width;
				guides.push({
					type: 'vertical',
					position: otherLeft,
					start: Math.min(cardTop, otherTop),
					end: Math.max(cardBottom, otherBottom)
				});
			}
			// Center to center (horizontal)
			else if (Math.abs(cardCenterX - otherCenterX) < CARD_SNAP_THRESHOLD) {
				snappedX = otherCenterX - width / 2;
				guides.push({
					type: 'vertical',
					position: otherCenterX,
					start: Math.min(cardTop, otherTop),
					end: Math.max(cardBottom, otherBottom)
				});
			}

			// Horizontal alignments (y-axis snapping)
			// Top edge to top edge
			if (Math.abs(cardTop - otherTop) < CARD_SNAP_THRESHOLD) {
				snappedY = otherTop;
				guides.push({
					type: 'horizontal',
					position: otherTop,
					start: Math.min(cardLeft, otherLeft),
					end: Math.max(cardRight, otherRight)
				});
			}
			// Bottom edge to bottom edge
			else if (Math.abs(cardBottom - otherBottom) < CARD_SNAP_THRESHOLD) {
				snappedY = otherBottom - height;
				guides.push({
					type: 'horizontal',
					position: otherBottom,
					start: Math.min(cardLeft, otherLeft),
					end: Math.max(cardRight, otherRight)
				});
			}
			// Top edge to bottom edge (stacking)
			else if (Math.abs(cardTop - otherBottom) < CARD_SNAP_THRESHOLD) {
				snappedY = otherBottom;
				guides.push({
					type: 'horizontal',
					position: otherBottom,
					start: Math.min(cardLeft, otherLeft),
					end: Math.max(cardRight, otherRight)
				});
			}
			// Bottom edge to top edge (stacking)
			else if (Math.abs(cardBottom - otherTop) < CARD_SNAP_THRESHOLD) {
				snappedY = otherTop - height;
				guides.push({
					type: 'horizontal',
					position: otherTop,
					start: Math.min(cardLeft, otherLeft),
					end: Math.max(cardRight, otherRight)
				});
			}
			// Center to center (vertical)
			else if (Math.abs(cardCenterY - otherCenterY) < CARD_SNAP_THRESHOLD) {
				snappedY = otherCenterY - height / 2;
				guides.push({
					type: 'horizontal',
					position: otherCenterY,
					start: Math.min(cardLeft, otherLeft),
					end: Math.max(cardRight, otherRight)
				});
			}
		}

		// Also snap to workspace edges (only if snapping is enabled)
		if (isSnappingEnabled) {
			if (Math.abs(x) < CARD_SNAP_THRESHOLD) {
				snappedX = 0;
				guides.push({ type: 'vertical', position: 0, start: 0, end: workspaceBounds.height });
			} else if (Math.abs(x + width - workspaceBounds.width) < CARD_SNAP_THRESHOLD) {
				snappedX = workspaceBounds.width - width;
				guides.push({ type: 'vertical', position: workspaceBounds.width, start: 0, end: workspaceBounds.height });
			}

			if (Math.abs(y) < CARD_SNAP_THRESHOLD) {
				snappedY = 0;
				guides.push({ type: 'horizontal', position: 0, start: 0, end: workspaceBounds.width });
			} else if (Math.abs(y + height - workspaceBounds.height) < CARD_SNAP_THRESHOLD) {
				snappedY = workspaceBounds.height - height;
				guides.push({ type: 'horizontal', position: workspaceBounds.height, start: 0, end: workspaceBounds.width });
			}
		}

		return { x: snappedX, y: snappedY, guides };
	}

	/**
	 * Update snap guides during drag
	 */
	export function updateSnapGuides(guides: SnapGuide[]) {
		snapGuides = guides;
	}

	/**
	 * Clear snap guides
	 */
	export function clearSnapGuides() {
		snapGuides = [];
	}

	// Show snap preview while dragging (disabled in freeflow mode)
	export function showSnapPreview(x: number, y: number, width: number, height: number) {
		// In freeflow mode, never show snap preview
		if (!isSnappingEnabled) {
			snapPreview = { show: false, x: 0, y: 0, width: 0, height: 0 };
			return;
		}
		const snapTo = getSnapZone(x, y, width, height);
		if (snapTo) {
			const geo = getSnapGeometry(snapTo);
			snapPreview = { show: true, ...geo };
		} else {
			snapPreview = { show: false, x: 0, y: 0, width: 0, height: 0 };
		}
	}

	export function hideSnapPreview() {
		snapPreview = { show: false, x: 0, y: 0, width: 0, height: 0 };
	}

	// Finalize snap when drag ends (disabled in freeflow mode)
	export function finalizeSnap(cardId: string, x: number, y: number, width: number, height: number) {
		// In freeflow mode, never finalize snapping
		if (!isSnappingEnabled) {
			hideSnapPreview();
			return;
		}
		const snapTo = getSnapZone(x, y, width, height);
		if (snapTo) {
			onCardSnap(cardId, snapTo);
		}
		hideSnapPreview();
	}

	// Context menu handling - only show on empty workspace, not on cards or inputs
	function handleContextMenu(e: MouseEvent) {
		const target = e.target as HTMLElement;

		// Don't show context menu if clicking on a card, input, textarea, or any interactive element
		if (target.closest('.base-card') ||
			target.closest('input') ||
			target.closest('textarea') ||
			target.closest('[contenteditable]') ||
			target.closest('button') ||
			target.closest('a')) {
			return; // Allow default context menu for these elements
		}

		e.preventDefault();
		contextMenu = {
			show: true,
			x: e.clientX,
			y: e.clientY,
		};
	}

	function closeContextMenu() {
		contextMenu = { show: false, x: 0, y: 0 };
	}

	function handleCreateFromMenu(type: CardType) {
		onCreateCard(type);
		closeContextMenu();
	}

	// Close context menu on click outside
	function handleClick() {
		if (contextMenu.show) {
			closeContextMenu();
		}
	}

	// Card type config for create menu
	const cardTypes: { type: CardType; label: string; icon: typeof MessageSquare }[] = [
		{ type: 'chat', label: 'New Chat', icon: MessageSquare },
		{ type: 'agent', label: 'New Agent', icon: Bot },
		{ type: 'studio', label: 'New Studio', icon: Palette },
		{ type: 'terminal', label: 'New Terminal', icon: Terminal }
	];
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	bind:this={workspaceEl}
	class="workspace"
	class:has-maximized={hasMaximizedCard}
	oncontextmenu={handleContextMenu}
	onclick={handleClick}
>
	<!-- Card Shuffle UI - Layout mode selector with focus nav integration -->
	{#if sortedCards.length > 0}
		<CardShuffle
			currentMode={layoutMode}
			onModeChange={handleLayoutModeChange}
			focusModeIndex={focusModeIndex()}
			focusModeTotal={focusModeTotal()}
			onFocusNavigate={onFocusNavigate}
		/>
	{/if}

	<!-- Snap Preview Guide -->
	{#if snapPreview.show}
		<div
			class="snap-preview"
			style:left="{snapPreview.x}px"
			style:top="{snapPreview.y}px"
			style:width="{snapPreview.width}px"
			style:height="{snapPreview.height}px"
		></div>
	{/if}

	<!-- Card-to-Card Snap Guides -->
	{#each snapGuides as guide}
		{#if guide.type === 'vertical'}
			<div
				class="snap-guide snap-guide-vertical"
				style:left="{guide.position}px"
				style:top="{guide.start}px"
				style:height="{guide.end - guide.start}px"
			></div>
		{:else}
			<div
				class="snap-guide snap-guide-horizontal"
				style:left="{guide.start}px"
				style:top="{guide.position}px"
				style:width="{guide.end - guide.start}px"
			></div>
		{/if}
	{/each}

	<!-- Render cards via children snippet -->
	{#if children}
		{@render children()}
	{/if}

	<!-- Empty state when no cards -->
	{#if sortedCards.length === 0}
		<div class="empty-state">
			<div class="empty-content">
				<div class="empty-icon">
					<MessageSquare size={48} strokeWidth={1} />
				</div>
				<h2 class="empty-title">Welcome to The Deck</h2>
				<p class="empty-text">Your workspace is empty. Create a card to get started.</p>
				<div class="create-buttons">
					{#each cardTypes as { type, label, icon: Icon }}
						<button
							class="create-btn"
							onclick={() => onCreateCard(type)}
						>
							<Icon size={18} />
							<span>{label}</span>
						</button>
					{/each}
				</div>
			</div>
		</div>
	{/if}

	<!-- Context Menu -->
	{#if contextMenu.show}
		<div
			class="context-menu"
			style:left="{contextMenu.x}px"
			style:top="{contextMenu.y}px"
		>
			<div class="context-menu-header">Create New</div>
			{#each cardTypes as { type, label, icon: Icon }}
				<button
					class="context-menu-item"
					onclick={() => handleCreateFromMenu(type)}
				>
					<Icon size={16} />
					<span>{label}</span>
				</button>
			{/each}
		</div>
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

	/* Snap Preview */
	.snap-preview {
		position: absolute;
		background: hsl(var(--primary) / 0.15);
		border: 2px dashed hsl(var(--primary) / 0.5);
		border-radius: 8px;
		pointer-events: none;
		z-index: 0;
		transition: all 0.15s ease;
	}

	/* Card-to-Card Snap Guides */
	.snap-guide {
		position: absolute;
		pointer-events: none;
		z-index: 9999;
	}

	.snap-guide-vertical {
		width: 1px;
		background: hsl(var(--primary) / 0.7);
		box-shadow: 0 0 4px hsl(var(--primary) / 0.5);
	}

	.snap-guide-horizontal {
		height: 1px;
		background: hsl(var(--primary) / 0.7);
		box-shadow: 0 0 4px hsl(var(--primary) / 0.5);
	}

	/* Empty State */
	.empty-state {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
	}

	.empty-content {
		text-align: center;
		color: hsl(var(--muted-foreground));
		pointer-events: auto;
	}

	.empty-icon {
		width: 80px;
		height: 80px;
		margin: 0 auto 1.5rem;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted) / 0.5);
		border-radius: 50%;
		color: hsl(var(--muted-foreground));
	}

	.empty-title {
		font-size: 1.25rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin-bottom: 0.5rem;
	}

	.empty-text {
		font-size: 0.875rem;
		margin-bottom: 1.5rem;
	}

	.create-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		justify-content: center;
	}

	.create-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1rem;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.create-btn:hover {
		background: hsl(var(--accent));
		border-color: hsl(var(--primary) / 0.3);
	}

	/* Context Menu */
	.context-menu {
		position: fixed;
		min-width: 180px;
		background: hsl(var(--popover));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		box-shadow:
			0 10px 15px -3px rgba(0, 0, 0, 0.3),
			0 4px 6px -4px rgba(0, 0, 0, 0.2);
		padding: 4px;
		z-index: 1000;
	}

	.context-menu-header {
		padding: 8px 12px 4px;
		font-size: 0.75rem;
		font-weight: 500;
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.context-menu-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		cursor: pointer;
		transition: background 0.1s ease;
	}

	.context-menu-item:hover {
		background: hsl(var(--accent));
	}
</style>
