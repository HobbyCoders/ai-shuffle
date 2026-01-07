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
	 */

	import { onMount } from 'svelte';
	import { MessageSquare, Bot, Palette, Terminal, Plus } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import { SNAP_THRESHOLD } from './types';
	import type { Snippet } from 'svelte';

	interface Props {
		cards: DeckCard[];
		onCardFocus: (id: string) => void;
		onCardMove: (id: string, x: number, y: number) => void;
		onCardResize: (id: string, width: number, height: number) => void;
		onCardSnap: (id: string, snapTo: DeckCard['snappedTo']) => void;
		onCreateCard: (type: CardType) => void;
		children?: Snippet;
	}

	let {
		cards,
		onCardFocus,
		onCardMove,
		onCardResize,
		onCardSnap,
		onCreateCard,
		children
	}: Props = $props();

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

	// Snap detection helper
	function getSnapZone(x: number, y: number, width: number, height: number): DeckCard['snappedTo'] | undefined {
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

	// Show snap preview while dragging
	export function showSnapPreview(x: number, y: number, width: number, height: number) {
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

	// Finalize snap when drag ends
	export function finalizeSnap(cardId: string, x: number, y: number, width: number, height: number) {
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
		{ type: 'canvas', label: 'New Canvas', icon: Palette },
		{ type: 'terminal', label: 'New Terminal', icon: Terminal },
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
