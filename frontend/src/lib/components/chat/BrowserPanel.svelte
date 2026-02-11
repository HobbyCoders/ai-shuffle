<script lang="ts">
	/**
	 * BrowserPanel - Side panel showing live browser preview
	 *
	 * Displays the AI agent's browser session with:
	 * - Read-only URL bar
	 * - Live screenshot (updated after each browser tool action)
	 * - Navigation controls (back, forward, refresh)
	 * - Close button
	 * - Resizable width via drag handle
	 */
	import { browserState } from '$lib/stores/browser';
	import type { ChatTab } from '$lib/stores/tabs';

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	// Local state for resize drag
	let isResizing = $state(false);
	let startX = $state(0);
	let startWidth = $state(0);

	function handleResizeStart(e: MouseEvent) {
		isResizing = true;
		startX = e.clientX;
		startWidth = $browserState.panelWidth;
		e.preventDefault();
	}

	function handleResizeMove(e: MouseEvent) {
		if (!isResizing) return;
		// Dragging left edge → increasing width means decreasing x
		const delta = startX - e.clientX;
		browserState.setPanelWidth(startWidth + delta);
	}

	function handleResizeEnd() {
		isResizing = false;
	}

	// Extract filename from URL for display
	function formatUrl(url: string): string {
		if (!url) return 'No page loaded';
		try {
			const u = new URL(url);
			return u.hostname + u.pathname + (u.search || '');
		} catch {
			return url;
		}
	}
</script>

<svelte:window
	onmousemove={handleResizeMove}
	onmouseup={handleResizeEnd}
/>

<div
	class="browser-panel"
	style="width: {$browserState.panelWidth}px"
	class:resizing={isResizing}
>
	<!-- Resize handle (left edge) -->
	<div
		class="resize-handle"
		onmousedown={handleResizeStart}
		role="separator"
		aria-orientation="vertical"
		tabindex="-1"
	></div>

	<div class="panel-content">
		<!-- Header with URL bar -->
		<div class="panel-header">
			<div class="url-bar">
				<!-- Status dot -->
				<div class="status-dot" class:active={$browserState.isActive}></div>

				<!-- URL text -->
				<span class="url-text" title={$browserState.url}>
					{formatUrl($browserState.url)}
				</span>
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

		<!-- Screenshot area -->
		<div class="screenshot-area">
			{#if $browserState.isLoading}
				<div class="loading-state">
					<div class="spinner"></div>
					<span>Loading...</span>
				</div>
			{:else if $browserState.screenshot}
				<img
					src="data:image/jpeg;base64,{$browserState.screenshot}"
					alt="Browser screenshot"
					class="screenshot-img"
				/>
			{:else}
				<div class="empty-state">
					<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
						<circle cx="12" cy="12" r="10"></circle>
						<line x1="2" y1="12" x2="22" y2="12"></line>
						<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
					</svg>
					<span>Browser will appear here when the AI navigates to a page</span>
				</div>
			{/if}
		</div>

		<!-- Control bar -->
		<div class="control-bar">
			<button class="nav-btn" title="Back" disabled={!$browserState.isActive}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="15 18 9 12 15 6"></polyline>
				</svg>
			</button>
			<button class="nav-btn" title="Forward" disabled={!$browserState.isActive}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="9 18 15 12 9 6"></polyline>
				</svg>
			</button>
			<button class="nav-btn" title="Refresh" disabled={!$browserState.isActive}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<polyline points="23 4 23 10 17 10"></polyline>
					<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
				</svg>
			</button>

			<div class="control-spacer"></div>

			<span class="session-label">
				{$browserState.isActive ? 'Active' : 'Inactive'}
			</span>
		</div>
	</div>
</div>

<style>
	.browser-panel {
		position: relative;
		display: flex;
		flex-direction: row;
		height: 100%;
		min-width: 300px;
		max-width: 800px;
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

	/* Header with URL bar */
	.panel-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
		background: var(--muted);
		flex-shrink: 0;
	}

	.url-bar {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 10px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 6px;
		min-width: 0;
	}

	.status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--muted-foreground);
		flex-shrink: 0;
		opacity: 0.4;
	}

	.status-dot.active {
		background: #22c55e;
		opacity: 1;
		box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
	}

	.url-text {
		font-size: 0.75rem;
		font-family: var(--card-font-mono, monospace);
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
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
		background: color-mix(in oklch, var(--destructive) 15%, transparent) !important;
		color: var(--destructive);
	}

	/* Title bar */
	.title-bar {
		padding: 4px 12px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.page-title {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		display: block;
	}

	/* Screenshot area */
	.screenshot-area {
		flex: 1;
		min-height: 0;
		overflow: auto;
		display: flex;
		align-items: flex-start;
		justify-content: center;
		background: color-mix(in oklch, var(--muted) 50%, var(--card));
	}

	.screenshot-img {
		width: 100%;
		height: auto;
		display: block;
		object-fit: contain;
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

	/* Control bar */
	.control-bar {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 12px;
		border-top: 1px solid var(--border);
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
	}

	.nav-btn:hover:not(:disabled) {
		background: var(--hover-overlay) !important;
		color: var(--foreground);
	}

	.nav-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.control-spacer {
		flex: 1;
	}

	.session-label {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		font-weight: 500;
	}
</style>
