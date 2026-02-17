<script lang="ts">
	/**
	 * BrowserPanel - Interactive side panel for AI + user browser collaboration
	 *
	 * Uses CDP screencast for live frame streaming rendered to a <canvas>.
	 * User input (mouse, keyboard, scroll) is forwarded via dedicated browser
	 * WebSocket for zero-latency interaction. The viewport dynamically matches
	 * the panel dimensions — no letterboxing.
	 *
	 * Features:
	 * - Live canvas rendering of CDP screencast frames (not static screenshots)
	 * - Direct mouse/keyboard/scroll forwarding via WebSocket (not REST API)
	 * - Dynamic viewport resizing via ResizeObserver (fills panel 1:1)
	 * - Editable URL bar with navigation (REST API for intentional nav)
	 * - Back/Forward/Refresh controls
	 * - Resizable panel width (desktop)
	 * - Full-screen overlay on mobile
	 */
	import { browserState, type BrowserState } from '$lib/stores/browser';
	import { onMount } from 'svelte';

	// Detect mobile using same heuristic as AppLayout
	let isMobile = $state(false);

	function checkMobile() {
		const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;
		const isSmall = window.innerWidth < 1024;
		isMobile = window.innerWidth < 640 || (isTouchDevice && isSmall);
	}

	onMount(() => {
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});

	// Local state
	let isResizing = $state(false);
	let startX = $state(0);
	let startWidth = $state(0);
	let urlInput = $state($browserState.url || '');

	// Canvas + container refs
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let canvasContainer: HTMLDivElement | undefined = $state();

	// Track current canvas dimensions for coordinate mapping
	let canvasWidth = $state(0);
	let canvasHeight = $state(0);

	// Keep URL input synced with store (when AI navigates or session switches)
	$effect(() => {
		urlInput = $browserState.url || '';
	});

	// --- Canvas frame rendering ---
	// Register the frame callback so the browser store can push JPEG frames to us.
	// Each frame is a raw JPEG ArrayBuffer from CDP screencast via browser WebSocket.
	$effect(() => {
		if (canvasEl) {
			const canvas = canvasEl;
			const ctx = canvas.getContext('2d');
			if (!ctx) return;

			browserState.setFrameCallback((data: ArrayBuffer) => {
				// Decode JPEG to ImageBitmap (hardware-accelerated, off main thread)
				const blob = new Blob([data], { type: 'image/jpeg' });
				createImageBitmap(blob).then((bitmap) => {
					// Resize canvas to match frame dimensions (if changed)
					if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
						canvas.width = bitmap.width;
						canvas.height = bitmap.height;
					}
					ctx.drawImage(bitmap, 0, 0);
					bitmap.close();
				}).catch(() => {
					// Ignore decode errors (e.g. partial frames during disconnect)
				});
			});

			return () => {
				browserState.setFrameCallback(null);
			};
		}
	});

	// --- Screencast lifecycle ---
	// Connect/disconnect screencast when panel opens/closes or session changes.
	$effect(() => {
		const state: BrowserState = $browserState;
		const container = canvasContainer;

		if (state.isOpen && state.sessionId && container) {
			const rect = container.getBoundingClientRect();
			const width = Math.round(rect.width) || 800;
			const height = Math.round(rect.height) || 600;
			browserState.connectScreencast(state.sessionId, width, height);
			canvasWidth = width;
			canvasHeight = height;
		}

		// Note: disconnecting is handled by switchSession/closePanel in the store
	});

	// --- Dynamic viewport via ResizeObserver ---
	// When the canvas container resizes (panel resize, window resize), update the
	// backend viewport + restart screencast at the new dimensions.
	onMount(() => {
		let resizeTimeout: ReturnType<typeof setTimeout> | null = null;

		const observer = new ResizeObserver((entries) => {
			for (const entry of entries) {
				const { width, height } = entry.contentRect;
				if (width < 10 || height < 10) continue; // Ignore collapsed states

				const w = Math.round(width);
				const h = Math.round(height);

				// Only send resize if dimensions actually changed meaningfully
				if (Math.abs(w - canvasWidth) < 5 && Math.abs(h - canvasHeight) < 5) continue;

				canvasWidth = w;
				canvasHeight = h;

				// Debounce resize to avoid spamming during drag
				if (resizeTimeout) clearTimeout(resizeTimeout);
				resizeTimeout = setTimeout(() => {
					const ws = browserState.getBrowserWs();
					if (ws.isConnected()) {
						ws.sendResize(w, h);
					}
				}, 200);
			}
		});

		// Observe when container becomes available
		const unsubscribe = browserState.subscribe(() => {
			if (canvasContainer) {
				observer.observe(canvasContainer);
			}
		});

		return () => {
			observer.disconnect();
			unsubscribe();
			if (resizeTimeout) clearTimeout(resizeTimeout);
		};
	});

	// --- Panel Resize (drag left edge) ---
	function handleResizeStart(e: PointerEvent) {
		isResizing = true;
		startX = e.clientX;
		startWidth = $browserState.panelWidth;
		e.preventDefault();
		(e.target as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handleResizeMove(e: PointerEvent) {
		if (!isResizing) return;
		const delta = startX - e.clientX;
		browserState.setPanelWidth(startWidth + delta);
	}

	function handleResizeEnd(e: PointerEvent) {
		isResizing = false;
		(e.target as HTMLElement).releasePointerCapture(e.pointerId);
	}

	// --- Navigation (REST API for intentional nav) ---
	function handleUrlKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			const url = urlInput.trim();
			if (url) browserState.navigate(url);
		}
	}

	function handleBack() {
		browserState.goBack();
	}

	function handleForward() {
		browserState.goForward();
	}

	function handleRefresh() {
		browserState.refresh();
	}

	// --- Mouse forwarding via WebSocket ---
	// Map canvas display coordinates to viewport coordinates.
	// CSS may scale the canvas (width:100%; height:100%), so we need the ratio.
	function getCanvasCoords(e: MouseEvent): { x: number; y: number } | null {
		if (!canvasEl) return null;
		const rect = canvasEl.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) return null;

		// Scale from CSS display size to actual canvas/viewport size
		const scaleX = canvasEl.width / rect.width;
		const scaleY = canvasEl.height / rect.height;
		return {
			x: Math.round((e.clientX - rect.left) * scaleX),
			y: Math.round((e.clientY - rect.top) * scaleY),
		};
	}

	function handleCanvasClick(e: MouseEvent) {
		const coords = getCanvasCoords(e);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		ws.sendMouse('click', coords.x, coords.y, 'left');

		// Focus canvas container for keyboard input
		if (isMobile) {
			showInputBar = true;
			requestAnimationFrame(() => inputBarEl?.focus());
		} else {
			canvasContainer?.focus();
		}
	}

	function handleCanvasDblClick(e: MouseEvent) {
		const coords = getCanvasCoords(e);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		ws.sendMouse('dblclick', coords.x, coords.y, 'left');
	}

	function handleCanvasMouseMove(e: MouseEvent) {
		// Only forward mousemove when a button is pressed (drag)
		if (e.buttons === 0) return;

		const coords = getCanvasCoords(e);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		ws.sendMouse('move', coords.x, coords.y, 'left');
	}

	function handleCanvasMouseDown(e: MouseEvent) {
		const coords = getCanvasCoords(e);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		const button = e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left';
		ws.sendMouse('down', coords.x, coords.y, button);
	}

	function handleCanvasMouseUp(e: MouseEvent) {
		const coords = getCanvasCoords(e);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		const button = e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left';
		ws.sendMouse('up', coords.x, coords.y, button);
	}

	// --- Keyboard forwarding via WebSocket (desktop) ---
	// Direct key events with no batching — instant feedback via live screencast.
	function getModifiers(e: KeyboardEvent): number {
		let mod = 0;
		if (e.altKey) mod |= 1;
		if (e.ctrlKey) mod |= 2;
		if (e.metaKey) mod |= 4;
		if (e.shiftKey) mod |= 8;
		return mod;
	}

	function handleCanvasKeydown(e: KeyboardEvent) {
		if (isMobile) return; // Mobile uses the input bar

		// Don't intercept browser shortcuts (Ctrl+T, Ctrl+W, etc.)
		if ((e.ctrlKey || e.metaKey) && !e.altKey) {
			const key = e.key.toLowerCase();
			if (['t', 'w', 'n', 'l', 'r', 'f5'].includes(key)) return;
		}

		e.preventDefault();

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		const text = e.key.length === 1 ? e.key : '';
		ws.sendKey('press', e.key, e.code, text, getModifiers(e));
	}

	// --- Scroll forwarding via WebSocket ---
	function handleCanvasWheel(e: WheelEvent) {
		e.preventDefault();

		const coords = getCanvasCoords(e as unknown as MouseEvent);
		if (!coords) return;

		const ws = browserState.getBrowserWs();
		if (!ws.isConnected()) return;

		ws.sendScroll(coords.x, coords.y, e.deltaX, e.deltaY);
	}

	// --- Context menu prevention ---
	function handleContextMenu(e: MouseEvent) {
		e.preventDefault(); // Prevent browser context menu on right-click
	}

	// --- Input bar (mobile primary, desktop supplementary) ---
	let inputBarText = $state('');
	let inputBarEl: HTMLInputElement | undefined = $state();
	let showInputBar = $state(false);

	function handleInputBarKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			sendInputBarText();
		}
	}

	function sendInputBarText() {
		const text = inputBarText;
		if (!text) return;
		inputBarText = '';

		const ws = browserState.getBrowserWs();
		if (ws.isConnected()) {
			// Send each character as a key press for proper form input
			for (const char of text) {
				ws.sendKey('press', char, '', char, 0);
			}
		}
	}

	function sendEnterKey() {
		const ws = browserState.getBrowserWs();
		if (ws.isConnected()) {
			ws.sendKey('press', 'Enter', 'Enter', '', 0);
		}
	}

	function sendTabKey() {
		const ws = browserState.getBrowserWs();
		if (ws.isConnected()) {
			ws.sendKey('press', 'Tab', 'Tab', '', 0);
		}
	}

	function toggleInputBar() {
		showInputBar = !showInputBar;
		if (showInputBar) {
			requestAnimationFrame(() => inputBarEl?.focus());
		}
	}
</script>

<div
	class="browser-panel"
	class:mobile={isMobile}
	style={isMobile ? '' : `width: ${$browserState.panelWidth}px`}
	class:resizing={isResizing}
>
	{#if isMobile}
		<!-- Mobile: swipe-down drag handle to dismiss -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="mobile-drag-handle" onclick={() => browserState.closePanel()}>
			<div class="drag-pill"></div>
		</div>
	{:else}
		<!-- Desktop: Resize handle (left edge) -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="resize-handle"
			onpointerdown={handleResizeStart}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeEnd}
			role="separator"
			aria-orientation="vertical"
			tabindex="-1"
		></div>
	{/if}

	<div class="panel-content">
		<!-- Navigation bar -->
		<div class="nav-bar">
			<button
				class="nav-btn"
				title="Back"
				disabled={!$browserState.isActive || $browserState.isLoading}
				onclick={handleBack}
			>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="15 18 9 12 15 6"></polyline>
				</svg>
			</button>
			<button
				class="nav-btn"
				title="Forward"
				disabled={!$browserState.isActive || $browserState.isLoading}
				onclick={handleForward}
			>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="9 18 15 12 9 6"></polyline>
				</svg>
			</button>
			<button
				class="nav-btn"
				title="Refresh"
				disabled={!$browserState.isActive || $browserState.isLoading}
				onclick={handleRefresh}
			>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="23 4 23 10 17 10"></polyline>
					<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
				</svg>
			</button>

			<!-- URL bar -->
			<div class="url-bar">
				<div class="status-dot" class:active={$browserState.isActive} class:streaming={$browserState.isStreaming}></div>
				<input
					type="text"
					class="url-input"
					placeholder="Enter URL..."
					bind:value={urlInput}
					onkeydown={handleUrlKeydown}
				/>
			</div>

			<!-- Close button -->
			<button
				class="close-btn"
				onclick={() => browserState.closePanel()}
				title="Close browser panel"
			>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<line x1="18" y1="6" x2="6" y2="18"></line>
					<line x1="6" y1="6" x2="18" y2="18"></line>
				</svg>
			</button>
		</div>

		<!-- Title bar -->
		{#if $browserState.title}
			<div class="title-bar">
				<span class="page-title">{$browserState.title}</span>
			</div>
		{/if}

		<!-- Canvas area (interactive) -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div
			class="canvas-area"
			bind:this={canvasContainer}
			tabindex="0"
			onclick={handleCanvasClick}
			ondblclick={handleCanvasDblClick}
			onmousedown={handleCanvasMouseDown}
			onmouseup={handleCanvasMouseUp}
			onmousemove={handleCanvasMouseMove}
			onkeydown={handleCanvasKeydown}
			onwheel={handleCanvasWheel}
			oncontextmenu={handleContextMenu}
		>
			{#if $browserState.isStreaming}
				<canvas
					bind:this={canvasEl}
					class="browser-canvas"
				></canvas>
			{:else if $browserState.isLoading}
				<div class="loading-state">
					<div class="spinner"></div>
					<span>Connecting...</span>
				</div>
			{:else if $browserState.isActive}
				<div class="loading-state">
					<div class="spinner"></div>
					<span>Starting screencast...</span>
				</div>
			{:else}
				<div class="empty-state">
					<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
						<circle cx="12" cy="12" r="10"></circle>
						<line x1="2" y1="12" x2="22" y2="12"></line>
						<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
					</svg>
					<span>Enter a URL above to start browsing, or let the AI navigate for you</span>
				</div>
			{/if}
		</div>

		<!-- Input bar — type text into the focused browser element -->
		{#if showInputBar && ($browserState.isStreaming || $browserState.isActive)}
			<div class="input-bar">
				<input
					type="text"
					class="input-bar-field"
					placeholder="Type here..."
					bind:value={inputBarText}
					bind:this={inputBarEl}
					onkeydown={handleInputBarKeydown}
				/>
				<button
					class="input-bar-btn"
					title="Send text"
					disabled={!inputBarText}
					onclick={sendInputBarText}
				>
					<!-- Send arrow icon -->
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<line x1="22" y1="2" x2="11" y2="13"></line>
						<polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
					</svg>
				</button>
				<button class="input-bar-btn" title="Press Tab" onclick={sendTabKey}>
					<span style="font-size: 0.6875rem; font-weight: 500;">Tab</span>
				</button>
				<button class="input-bar-btn submit-btn" title="Press Enter" onclick={sendEnterKey}>
					<!-- Enter/return icon -->
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<polyline points="9 10 4 15 9 20"></polyline>
						<path d="M20 4v7a4 4 0 01-4 4H4"></path>
					</svg>
				</button>
			</div>
		{/if}

		<!-- Status bar -->
		<div class="status-bar">
			{#if $browserState.isActive}
				<button
					class="keyboard-toggle"
					class:active={showInputBar}
					title={showInputBar ? 'Hide keyboard' : 'Show keyboard'}
					onclick={toggleInputBar}
				>
					<!-- Keyboard icon -->
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
						<rect x="2" y="4" width="20" height="16" rx="2" ry="2"></rect>
						<line x1="6" y1="8" x2="6" y2="8"></line>
						<line x1="10" y1="8" x2="10" y2="8"></line>
						<line x1="14" y1="8" x2="14" y2="8"></line>
						<line x1="18" y1="8" x2="18" y2="8"></line>
						<line x1="6" y1="12" x2="6" y2="12"></line>
						<line x1="10" y1="12" x2="10" y2="12"></line>
						<line x1="14" y1="12" x2="14" y2="12"></line>
						<line x1="18" y1="12" x2="18" y2="12"></line>
						<line x1="8" y1="16" x2="16" y2="16"></line>
					</svg>
				</button>
			{/if}
			<span class="status-label">
				{#if $browserState.isStreaming}
					Live · Shared with AI
				{:else if $browserState.isLoading}
					Connecting...
				{:else if $browserState.isActive}
					Starting...
				{:else}
					Ready — enter a URL to browse
				{/if}
			</span>
			{#if $browserState.isStreaming}
				<span class="streaming-indicator"></span>
			{/if}
		</div>
	</div>
</div>

<style>
	.browser-panel {
		position: relative;
		display: flex;
		flex-direction: row;
		height: 100%;
		min-width: 320px;
		max-width: 70vw;
		border-left: 1px solid var(--border);
		background: var(--card);
		flex-shrink: 0;
	}

	.browser-panel.resizing {
		user-select: none;
	}

	.resize-handle {
		position: absolute;
		left: -3px;
		top: 0;
		bottom: 0;
		width: 6px;
		cursor: col-resize;
		z-index: 10;
		touch-action: none;
	}

	.resize-handle:hover,
	.browser-panel.resizing .resize-handle {
		background: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.panel-content {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		overflow: hidden;
	}

	/* Navigation bar (browser chrome) */
	.nav-bar {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		background: var(--muted);
		flex-shrink: 0;
	}

	.nav-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
	}

	.nav-btn:hover:not(:disabled) {
		background: var(--hover-overlay, color-mix(in oklch, var(--foreground) 8%, transparent));
		color: var(--foreground);
	}

	.nav-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* URL bar */
	.url-bar {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 2px 8px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 6px;
		min-width: 0;
		height: 28px;
	}

	.url-bar:focus-within {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	.status-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--muted-foreground);
		flex-shrink: 0;
		opacity: 0.4;
	}

	.status-dot.active {
		background: #f59e0b;
		opacity: 1;
	}

	.status-dot.streaming {
		background: #22c55e;
		opacity: 1;
		box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
	}

	.url-input {
		flex: 1;
		border: none;
		background: transparent;
		font-size: 0.75rem;
		font-family: var(--card-font-mono, monospace);
		color: var(--foreground);
		outline: none;
		min-width: 0;
		padding: 0;
	}

	.url-input::placeholder {
		color: var(--muted-foreground);
	}

	.url-input:disabled {
		opacity: 0.5;
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
	}

	.close-btn:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	/* Title bar */
	.title-bar {
		padding: 3px 12px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.page-title {
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		display: block;
	}

	/* Canvas area — replaces screenshot area */
	.canvas-area {
		flex: 1;
		min-height: 0;
		overflow: hidden;
		display: flex;
		align-items: stretch;
		justify-content: center;
		background: color-mix(in oklch, var(--muted) 50%, var(--card));
		cursor: default;
		outline: none;
		position: relative;
	}

	.canvas-area:focus {
		box-shadow: inset 0 0 0 2px color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.browser-canvas {
		width: 100%;
		height: 100%;
		display: block;
		object-fit: fill;
	}

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 2rem;
		text-align: center;
		height: 100%;
		color: var(--muted-foreground);
	}

	.empty-state span {
		font-size: 0.8125rem;
		max-width: 200px;
		line-height: 1.4;
	}

	.loading-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		height: 100%;
		color: var(--muted-foreground);
	}

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Input bar — between canvas and status bar */
	.input-bar {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 8px;
		border-top: 1px solid var(--border);
		background: var(--muted);
		flex-shrink: 0;
	}

	.input-bar-field {
		flex: 1;
		height: 32px;
		padding: 0 8px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--card);
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		min-width: 0;
	}

	.input-bar-field:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	.input-bar-field::placeholder {
		color: var(--muted-foreground);
	}

	.input-bar-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--card);
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
	}

	.input-bar-btn:hover:not(:disabled) {
		background: var(--hover-overlay, color-mix(in oklch, var(--foreground) 8%, transparent));
		color: var(--foreground);
	}

	.input-bar-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.input-bar-btn.submit-btn {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	.input-bar-btn.submit-btn:hover {
		opacity: 0.9;
	}

	/* Status bar */
	.status-bar {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 12px;
		border-top: 1px solid var(--border);
		background: var(--muted);
		flex-shrink: 0;
	}

	.keyboard-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		border: none;
		border-radius: 4px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
		padding: 0;
	}

	.keyboard-toggle:hover,
	.keyboard-toggle.active {
		color: var(--foreground);
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
	}

	.keyboard-toggle.active {
		color: var(--primary);
	}

	.status-label {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		letter-spacing: 0.02em;
		flex: 1;
	}

	/* Streaming indicator — pulsing green dot */
	.streaming-indicator {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: #22c55e;
		flex-shrink: 0;
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}

	/* ========================================
	   MOBILE: Full-screen overlay
	   ======================================== */

	.browser-panel.mobile {
		position: fixed;
		inset: 0;
		z-index: 1000;
		width: 100% !important;
		max-width: none;
		min-width: 0;
		border-left: none;
		border-radius: 0;
		flex-direction: column;
		animation: slideUp 0.25s ease-out;
	}

	@keyframes slideUp {
		from {
			transform: translateY(100%);
		}
		to {
			transform: translateY(0);
		}
	}

	.mobile-drag-handle {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 8px 0 4px;
		cursor: pointer;
		flex-shrink: 0;
		background: var(--muted);
		-webkit-tap-highlight-color: transparent;
	}

	.drag-pill {
		width: 36px;
		height: 4px;
		border-radius: 2px;
		background: var(--muted-foreground);
		opacity: 0.4;
	}

	/* Mobile nav bar: larger touch targets */
	.browser-panel.mobile .nav-btn {
		width: 44px;
		height: 44px;
	}

	.browser-panel.mobile .close-btn {
		width: 44px;
		height: 44px;
	}

	.browser-panel.mobile .url-bar {
		height: 36px;
	}

	.browser-panel.mobile .url-input {
		font-size: 0.875rem;
	}

	/* Mobile input bar: larger touch targets */
	.browser-panel.mobile .input-bar {
		padding: 8px;
		gap: 6px;
	}

	.browser-panel.mobile .input-bar-field {
		height: 40px;
		font-size: 1rem;
	}

	.browser-panel.mobile .input-bar-btn {
		width: 40px;
		height: 40px;
	}

	.browser-panel.mobile .keyboard-toggle {
		width: 32px;
		height: 32px;
	}

	/* Mobile: safe area insets for notched devices */
	.browser-panel.mobile .status-bar {
		padding-bottom: max(4px, env(safe-area-inset-bottom, 4px));
	}
</style>
