<script lang="ts">
	/**
	 * BaseCard - Draggable, resizable window component for The Deck
	 *
	 * Features:
	 * - Header with drag handle and window controls
	 * - Card type icon (Lucide icons)
	 * - Editable title on double-click
	 * - Window controls: minimize, maximize/restore, close
	 * - 8 resize handles (n, s, e, w, ne, nw, se, sw)
	 * - States: focused, minimized, maximized
	 * - Glassmorphism styling
	 */

	import { MessageSquare, Bot, Palette, Terminal, Minus, Square, X, Maximize2, Copy, Settings, User, Users, FolderKanban } from 'lucide-svelte';
	import type { DeckCard, CardType } from './types';
	import type { Snippet } from 'svelte';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMinimize: () => void;
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
		onMinimize,
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
		canvas: Palette,
		terminal: Terminal,
		settings: Settings,
		profile: User,
		subagent: Users,
		project: FolderKanban,
	};

	const CardIcon = $derived(cardIcons[card.type]);

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

	// Double-click header to maximize/restore
	function handleHeaderDoubleClick() {
		if (!isEditingTitle) {
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
	class:minimized={card.minimized}
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
			<button
				class="control-btn minimize"
				onclick={(e) => { e.stopPropagation(); onMinimize(); }}
				title="Minimize"
				aria-label="Minimize"
			>
				<Minus size={14} />
			</button>
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
		background: var(--card);
		border-radius: 12px;
		border: 1px solid var(--border);
		overflow: hidden;
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.3),
			0 2px 4px -2px rgba(0, 0, 0, 0.15),
			0 0 0 1px var(--border);
		transition: box-shadow 0.15s ease, border-color 0.15s ease;
	}

	.base-card.focused {
		box-shadow:
			0 10px 15px -3px rgba(0, 0, 0, 0.3),
			0 4px 6px -4px rgba(0, 0, 0, 0.2),
			0 0 0 1px hsl(var(--primary) / 0.3);
		border-color: hsl(var(--primary) / 0.4);
	}

	.base-card.maximized {
		border-radius: 0;
		border: none;
	}

	.base-card.minimized {
		display: none;
	}

	.base-card.dragging,
	.base-card.resizing {
		transition: none;
		user-select: none;
	}

	.base-card.dragging {
		opacity: 0.95;
	}

	/* Header */
	.card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 40px;
		padding: 0 8px 0 12px;
		background: var(--secondary);
		border-bottom: 1px solid var(--border);
		cursor: grab;
		user-select: none;
		flex-shrink: 0;
	}

	.base-card.dragging .card-header {
		cursor: grabbing;
	}

	.header-left {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		min-width: 0;
	}

	.card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		color: hsl(var(--muted-foreground));
		flex-shrink: 0;
	}

	.card-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: hsl(var(--foreground) / 0.8);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		cursor: text;
	}

	.base-card.focused .card-title {
		color: hsl(var(--foreground));
	}

	.title-input {
		font-size: 0.8125rem;
		font-weight: 500;
		background: hsl(var(--background));
		border: 1px solid hsl(var(--primary));
		border-radius: 4px;
		padding: 2px 6px;
		color: hsl(var(--foreground));
		outline: none;
		width: 100%;
		max-width: 200px;
	}

	/* Window Controls */
	.window-controls {
		display: flex;
		gap: 0;
	}

	.control-btn {
		width: 36px;
		height: 28px;
		border: none;
		background: transparent;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		color: hsl(var(--muted-foreground));
		transition: background 0.1s ease, color 0.1s ease;
		border-radius: 0;
	}

	.control-btn:hover {
		background: hsl(var(--accent));
		color: hsl(var(--foreground));
	}

	.control-btn.close:hover {
		background: hsl(var(--destructive));
		color: hsl(var(--destructive-foreground));
	}

	/* Content */
	.card-content {
		flex: 1;
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	/* Resize Handles */
	.resize-handle {
		position: absolute;
		z-index: 10;
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
