<script lang="ts">
	/**
	 * BaseCard - Draggable, resizable window component for The Deck
	 *
	 * Features:
	 * - Header with drag handle and window controls
	 * - Card type icon (Lucide icons)
	 * - Editable title on double-click
	 * - Window controls: maximize/restore, close
	 * - 8 resize handles (n, s, e, w, ne, nw, se, sw)
	 * - States: focused, maximized
	 * - Glassmorphism styling
	 */

	import { MessageSquare, Bot, Terminal, Square, X, Maximize2, Copy, Settings, User, Users, FolderKanban } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import type { Snippet } from 'svelte';
	import { layoutMode } from '$lib/stores/deck';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		onTitleChange?: (title: string) => void;
		children?: Snippet;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		onTitleChange,
		children
	}: Props = $props();

	// Editing state
	let isEditingTitle = $state(false);
	let editedTitle = $state('');
	let titleInput: HTMLInputElement | undefined = $state();

	// Drag state
	let isDragging = $state(false);
	let dragStart = $state({ x: 0, y: 0, cardX: 0, cardY: 0 });

	// Resize state
	let isResizing = $state(false);
	let resizeEdge = $state<string | null>(null);
	let resizeStart = $state({ x: 0, y: 0, width: 0, height: 0, cardX: 0, cardY: 0 });

	// Icon mapping
	const cardIcons: Record<CardType, typeof MessageSquare> = {
		chat: MessageSquare,
		agent: Bot,
		terminal: Terminal,
		settings: Settings,
		profile: User,
		subagent: Users,
		project: FolderKanban,
	};

	const CardIcon = $derived(cardIcons[card.type]);

	// In focus mode, cards are always fullscreen and cannot be un-maximized
	const isFocusMode = $derived($layoutMode === 'focus');

	// Title editing
	function handleTitleDoubleClick() {
		isEditingTitle = true;
		editedTitle = card.title;
		// Focus input after render
		requestAnimationFrame(() => {
			titleInput?.focus();
			titleInput?.select();
		});
	}

	function handleTitleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			finishTitleEdit();
		} else if (e.key === 'Escape') {
			cancelTitleEdit();
		}
	}

	function finishTitleEdit() {
		if (editedTitle.trim() && editedTitle !== card.title) {
			onTitleChange?.(editedTitle.trim());
		}
		isEditingTitle = false;
	}

	function cancelTitleEdit() {
		isEditingTitle = false;
		editedTitle = '';
	}

	// Drag handling with pointer events
	function handlePointerDown(e: PointerEvent) {
		if (card.maximized || isEditingTitle) return;

		const target = e.target as HTMLElement;
		// Only start drag from header area, not buttons
		if (target.closest('.window-controls') || target.closest('button')) return;

		isDragging = true;
		dragStart = {
			x: e.clientX,
			y: e.clientY,
			cardX: card.x,
			cardY: card.y,
		};

		onFocus();
		(e.target as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent) {
		if (!isDragging) return;

		const dx = e.clientX - dragStart.x;
		const dy = e.clientY - dragStart.y;
		onMove(dragStart.cardX + dx, dragStart.cardY + dy);
	}

	function handlePointerUp(e: PointerEvent) {
		if (isDragging) {
			isDragging = false;
			(e.target as HTMLElement).releasePointerCapture(e.pointerId);
			onDragEnd?.();
		}
	}

	// Resize handling
	function handleResizeStart(e: PointerEvent, edge: string) {
		if (card.maximized) return;

		e.stopPropagation();
		isResizing = true;
		resizeEdge = edge;
		resizeStart = {
			x: e.clientX,
			y: e.clientY,
			width: card.width,
			height: card.height,
			cardX: card.x,
			cardY: card.y,
		};

		onFocus();
		(e.target as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handleResizeMove(e: PointerEvent) {
		if (!isResizing || !resizeEdge) return;

		const dx = e.clientX - resizeStart.x;
		const dy = e.clientY - resizeStart.y;

		let newWidth = resizeStart.width;
		let newHeight = resizeStart.height;
		let newX = resizeStart.cardX;
		let newY = resizeStart.cardY;

		// Calculate new dimensions based on edge
		if (resizeEdge.includes('e')) {
			newWidth = Math.max(320, resizeStart.width + dx);
		}
		if (resizeEdge.includes('w')) {
			const maxDx = resizeStart.width - 320;
			const actualDx = Math.min(dx, maxDx);
			newWidth = resizeStart.width - actualDx;
			newX = resizeStart.cardX + actualDx;
		}
		if (resizeEdge.includes('s')) {
			newHeight = Math.max(200, resizeStart.height + dy);
		}
		if (resizeEdge.includes('n')) {
			const maxDy = resizeStart.height - 200;
			const actualDy = Math.min(dy, maxDy);
			newHeight = resizeStart.height - actualDy;
			newY = resizeStart.cardY + actualDy;
		}

		onMove(newX, newY);
		onResize(newWidth, newHeight);
	}

	function handleResizeEnd(e: PointerEvent) {
		if (isResizing) {
			isResizing = false;
			resizeEdge = null;
			(e.target as HTMLElement).releasePointerCapture(e.pointerId);
			onResizeEnd?.();
		}
	}

	// Double-click header to maximize/restore (disabled in focus mode)
	function handleHeaderDoubleClick() {
		// In focus mode, don't allow toggling maximize - cards are always fullscreen
		if (!isEditingTitle && !isFocusMode) {
			onMaximize();
		}
	}

	// Handle click on card body for focus
	function handleCardClick() {
		onFocus();
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	class="base-card"
	class:focused={card.focused}
	class:maximized={card.maximized}
	class:dragging={isDragging}
	class:resizing={isResizing}
	onclick={handleCardClick}
>
	<!-- Header / Title Bar -->
	<div
		class="card-header"
		onpointerdown={handlePointerDown}
		onpointermove={handlePointerMove}
		onpointerup={handlePointerUp}
		ondblclick={handleHeaderDoubleClick}
	>
		<div class="header-left">
			<div class="card-icon">
				<CardIcon size={16} />
			</div>
			{#if isEditingTitle}
				<input
					bind:this={titleInput}
					type="text"
					bind:value={editedTitle}
					onkeydown={handleTitleKeydown}
					onblur={finishTitleEdit}
					class="title-input"
				/>
			{:else}
				<span
					class="card-title"
					ondblclick={handleTitleDoubleClick}
					role="button"
					tabindex="0"
				>
					{card.title}
				</span>
			{/if}
		</div>

		<div class="window-controls">
			<!-- Hide maximize button in focus mode since cards are always fullscreen -->
			{#if !isFocusMode}
				<button
					class="control-btn maximize"
					onclick={(e) => { e.stopPropagation(); onMaximize(); }}
					title={card.maximized ? 'Restore' : 'Maximize'}
					aria-label={card.maximized ? 'Restore' : 'Maximize'}
				>
					{#if card.maximized}
						<Copy size={12} />
					{:else}
						<Square size={12} />
					{/if}
				</button>
			{/if}
			<button
				class="control-btn close"
				onclick={(e) => { e.stopPropagation(); onClose(); }}
				title="Close"
				aria-label="Close"
			>
				<X size={14} />
			</button>
		</div>
	</div>

	<!-- Content Area -->
	<div class="card-content">
		{#if children}
			{@render children()}
		{/if}
	</div>

	<!-- Resize Handles (hidden when maximized) -->
	{#if !card.maximized}
		<div
			class="resize-handle n"
			onpointerdown={(e) => handleResizeStart(e, 'n')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle s"
			onpointerdown={(e) => handleResizeStart(e, 's')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle e"
			onpointerdown={(e) => handleResizeStart(e, 'e')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle w"
			onpointerdown={(e) => handleResizeStart(e, 'w')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle ne"
			onpointerdown={(e) => handleResizeStart(e, 'ne')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle nw"
			onpointerdown={(e) => handleResizeStart(e, 'nw')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle se"
			onpointerdown={(e) => handleResizeStart(e, 'se')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
		<div
			class="resize-handle sw"
			onpointerdown={(e) => handleResizeStart(e, 'sw')}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
		></div>
	{/if}
</div>

<style>
	.base-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--glass-bg);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border-radius: 12px;
		border: 1px solid var(--glass-border);
		overflow: visible;
		box-shadow: var(--shadow-m);
		transition:
			box-shadow 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			border-color 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
	}

	.base-card:hover:not(.focused):not(.dragging):not(.resizing) {
		border-color: var(--border-subtle);
		box-shadow: var(--shadow-l);
	}

	.base-card.focused {
		box-shadow:
			var(--shadow-l),
			0 0 0 1px color-mix(in oklch, var(--primary) 30%, transparent),
			0 0 20px var(--glow-color-soft);
		border-color: color-mix(in oklch, var(--primary) 40%, transparent);
	}

	.base-card.maximized {
		border-radius: 0;
		border: none;
		box-shadow: none;
	}

	.base-card.dragging,
	.base-card.resizing {
		transition: none;
		user-select: none;
	}

	.base-card.dragging {
		opacity: 0.92;
		transform: scale(1.005);
		box-shadow:
			var(--shadow-l),
			0 20px 40px color-mix(in oklch, var(--panel-shadow-outer) 60%, transparent);
	}

	/* Header */
	.card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 44px;
		padding: 0 10px 0 14px;
		background: linear-gradient(
			180deg,
			color-mix(in oklch, var(--secondary) 100%, transparent) 0%,
			color-mix(in oklch, var(--secondary) 85%, var(--card)) 100%
		);
		border-bottom: 1px solid var(--border);
		cursor: grab;
		user-select: none;
		flex-shrink: 0;
		position: relative;
	}

	/* Subtle top highlight for depth */
	.card-header::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: var(--panel-shadow-inset);
		pointer-events: none;
	}

	/* Mobile-friendly header height */
	@media (pointer: coarse) {
		.card-header {
			height: 48px;
			padding: 0 12px 0 16px;
		}
	}

	.base-card.dragging .card-header {
		cursor: grabbing;
	}

	.header-left {
		display: flex;
		align-items: center;
		gap: 10px;
		flex: 1;
		min-width: 0;
	}

	.card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: color 0.15s ease, background-color 0.15s ease;
		border-radius: 6px;
		background: transparent;
	}

	.base-card.focused .card-icon {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.card-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		cursor: text;
		transition: color 0.15s ease;
		/* Improved text rendering for readability */
		-webkit-font-smoothing: antialiased;
		-moz-osx-font-smoothing: grayscale;
	}

	.base-card.focused .card-title {
		color: var(--foreground);
		font-weight: 600;
	}

	.title-input {
		font-size: 0.8125rem;
		font-weight: 500;
		background: var(--background);
		border: 1px solid var(--primary);
		border-radius: 4px;
		padding: 2px 6px;
		color: var(--foreground);
		outline: none;
		width: 100%;
		max-width: 200px;
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* Window Controls */
	.window-controls {
		display: flex;
		gap: 4px;
	}

	.control-btn {
		width: 32px;
		height: 28px;
		border: none;
		background: transparent;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--muted-foreground);
		transition:
			background 0.15s ease,
			color 0.15s ease,
			transform 0.1s ease;
		border-radius: 6px;
	}

	/* Larger touch targets on mobile */
	@media (pointer: coarse) {
		.control-btn {
			width: 40px;
			height: 36px;
		}
	}

	.control-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
		transform: scale(1.05);
	}

	.control-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.control-btn:active {
		transform: scale(0.95);
	}

	.control-btn.maximize:hover {
		background: color-mix(in oklch, var(--success) 20%, transparent);
		color: var(--success);
	}

	.control-btn.close:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
		color: var(--destructive);
	}

	/* Content */
	.card-content {
		flex: 1;
		min-height: 0; /* Critical: allows flex child to shrink and enables internal scrolling */
		overflow: visible;
		display: flex;
		flex-direction: column;
		background: var(--card);
		/* Allow fixed-position children (dropdowns, modals) to escape */
		position: relative;
	}

	/* Resize Handles */
	.resize-handle {
		position: absolute;
		z-index: 10;
		transition: background 0.15s ease;
	}

	.resize-handle:hover {
		background: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.resize-handle.n,
	.resize-handle.s {
		left: 8px;
		right: 8px;
		height: 6px;
		cursor: ns-resize;
	}

	.resize-handle.n {
		top: -3px;
	}

	.resize-handle.s {
		bottom: -3px;
	}

	.resize-handle.e,
	.resize-handle.w {
		top: 8px;
		bottom: 8px;
		width: 6px;
		cursor: ew-resize;
	}

	.resize-handle.e {
		right: -3px;
	}

	.resize-handle.w {
		left: -3px;
	}

	.resize-handle.ne,
	.resize-handle.nw,
	.resize-handle.se,
	.resize-handle.sw {
		width: 12px;
		height: 12px;
	}

	.resize-handle.ne {
		top: -3px;
		right: -3px;
		cursor: nesw-resize;
	}

	.resize-handle.nw {
		top: -3px;
		left: -3px;
		cursor: nwse-resize;
	}

	.resize-handle.se {
		bottom: -3px;
		right: -3px;
		cursor: nwse-resize;
	}

	.resize-handle.sw {
		bottom: -3px;
		left: -3px;
		cursor: nesw-resize;
	}
</style>
