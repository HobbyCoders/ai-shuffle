<script lang="ts">
	/**
	 * WorkspaceCard - Draggable, resizable card wrapper
	 *
	 * Handles:
	 * - Drag to reposition
	 * - Resize from edges/corners
	 * - Focus on click
	 * - Keyboard navigation
	 */

	import { workspace, focusedCardId, type WorkspaceCard, type CardPosition, type CardSize } from '$lib/stores/workspace';
	import BaseCard from './BaseCard.svelte';

	interface Props {
		card: WorkspaceCard;
		bounds: { width: number; height: number };
		isMaximized?: boolean;
	}

	let { card, bounds, isMaximized = false }: Props = $props();

	// Drag state
	let isDragging = $state(false);
	let dragOffset = $state({ x: 0, y: 0 });

	// Resize state
	let isResizing = $state(false);
	let resizeEdge = $state<string | null>(null);
	let resizeStart = $state({ x: 0, y: 0, width: 0, height: 0, posX: 0, posY: 0 });

	const isFocused = $derived($focusedCardId === card.id);

	// Handle focus on click
	function handleFocus() {
		if (!isFocused) {
			workspace.focusCard(card.id);
		}
	}

	// Drag handlers
	function startDrag(e: MouseEvent) {
		if (isMaximized) return;

		isDragging = true;
		dragOffset = {
			x: e.clientX - card.position.x,
			y: e.clientY - card.position.y
		};

		workspace.focusCard(card.id);

		window.addEventListener('mousemove', onDrag);
		window.addEventListener('mouseup', stopDrag);
	}

	function onDrag(e: MouseEvent) {
		if (!isDragging) return;

		const newX = Math.max(0, Math.min(e.clientX - dragOffset.x, bounds.width - card.size.width));
		const newY = Math.max(0, Math.min(e.clientY - dragOffset.y, bounds.height - 40)); // Keep title bar visible

		workspace.moveCard(card.id, { x: newX, y: newY });
	}

	function stopDrag() {
		isDragging = false;
		window.removeEventListener('mousemove', onDrag);
		window.removeEventListener('mouseup', stopDrag);
	}

	// Resize handlers
	function startResize(e: MouseEvent, edge: string) {
		if (isMaximized) return;

		e.stopPropagation();
		isResizing = true;
		resizeEdge = edge;
		resizeStart = {
			x: e.clientX,
			y: e.clientY,
			width: card.size.width,
			height: card.size.height,
			posX: card.position.x,
			posY: card.position.y
		};

		workspace.focusCard(card.id);

		window.addEventListener('mousemove', onResize);
		window.addEventListener('mouseup', stopResize);
	}

	function onResize(e: MouseEvent) {
		if (!isResizing || !resizeEdge) return;

		const dx = e.clientX - resizeStart.x;
		const dy = e.clientY - resizeStart.y;

		let newWidth = resizeStart.width;
		let newHeight = resizeStart.height;
		let newX = resizeStart.posX;
		let newY = resizeStart.posY;

		// Handle different edges
		if (resizeEdge.includes('e')) {
			newWidth = Math.max(320, resizeStart.width + dx);
		}
		if (resizeEdge.includes('w')) {
			const maxDx = resizeStart.width - 320;
			const actualDx = Math.min(dx, maxDx);
			newWidth = resizeStart.width - actualDx;
			newX = resizeStart.posX + actualDx;
		}
		if (resizeEdge.includes('s')) {
			newHeight = Math.max(200, resizeStart.height + dy);
		}
		if (resizeEdge.includes('n')) {
			const maxDy = resizeStart.height - 200;
			const actualDy = Math.min(dy, maxDy);
			newHeight = resizeStart.height - actualDy;
			newY = resizeStart.posY + actualDy;
		}

		// Constrain to workspace bounds
		newX = Math.max(0, newX);
		newY = Math.max(0, newY);

		workspace.moveCard(card.id, { x: newX, y: newY });
		workspace.resizeCard(card.id, { width: newWidth, height: newHeight });
	}

	function stopResize() {
		isResizing = false;
		resizeEdge = null;
		window.removeEventListener('mousemove', onResize);
		window.removeEventListener('mouseup', stopResize);
	}

	// Card actions
	function handleMinimize() {
		workspace.minimizeCard(card.id);
	}

	function handleMaximize() {
		if (isMaximized) {
			workspace.restoreCard(card.id);
		} else {
			workspace.maximizeCard(card.id);
		}
	}

	function handleClose() {
		workspace.closeCard(card.id);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	class="workspace-card"
	class:focused={isFocused}
	class:maximized={isMaximized}
	class:dragging={isDragging}
	class:resizing={isResizing}
	style:left={isMaximized ? '0' : `${card.position.x}px`}
	style:top={isMaximized ? '0' : `${card.position.y}px`}
	style:width={isMaximized ? '100%' : `${card.size.width}px`}
	style:height={isMaximized ? '100%' : `${card.size.height}px`}
	style:z-index={card.zIndex}
	onclick={handleFocus}
>
	<BaseCard
		title={card.title}
		cardType={card.type}
		{isFocused}
		{isMaximized}
		onDragStart={startDrag}
		onMinimize={handleMinimize}
		onMaximize={handleMaximize}
		onClose={handleClose}
		cardId={card.id}
		dataId={card.dataId}
	/>

	<!-- Resize handles (hidden when maximized) -->
	{#if !isMaximized}
		<div class="resize-handle n" onmousedown={(e) => startResize(e, 'n')}></div>
		<div class="resize-handle s" onmousedown={(e) => startResize(e, 's')}></div>
		<div class="resize-handle e" onmousedown={(e) => startResize(e, 'e')}></div>
		<div class="resize-handle w" onmousedown={(e) => startResize(e, 'w')}></div>
		<div class="resize-handle ne" onmousedown={(e) => startResize(e, 'ne')}></div>
		<div class="resize-handle nw" onmousedown={(e) => startResize(e, 'nw')}></div>
		<div class="resize-handle se" onmousedown={(e) => startResize(e, 'se')}></div>
		<div class="resize-handle sw" onmousedown={(e) => startResize(e, 'sw')}></div>
	{/if}
</div>

<style>
	.workspace-card {
		position: absolute;
		display: flex;
		flex-direction: column;
		border-radius: 12px;
		overflow: hidden;
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.3),
			0 2px 4px -2px rgba(0, 0, 0, 0.2);
		transition: box-shadow 0.15s ease;
		background: #1a1a1a;
		border: 1px solid rgba(255, 255, 255, 0.08);
	}

	.workspace-card.focused {
		box-shadow:
			0 10px 15px -3px rgba(0, 0, 0, 0.4),
			0 4px 6px -4px rgba(0, 0, 0, 0.3),
			0 0 0 1px rgba(255, 165, 0, 0.3);
		border-color: rgba(255, 165, 0, 0.3);
	}

	.workspace-card.maximized {
		border-radius: 0;
		border: none;
	}

	.workspace-card.dragging,
	.workspace-card.resizing {
		transition: none;
		user-select: none;
	}

	.workspace-card.dragging {
		cursor: grabbing;
		opacity: 0.95;
	}

	/* Resize handles */
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
