<script lang="ts">
	/**
	 * BrowserPanel — right-docked live browser view.
	 *
	 * Owns the screencast WebSocket lifecycle for the active session: it connects
	 * only while the panel is open and a session is set, tears the socket down on
	 * close / session switch, and folds `status` / `frame` frames into the
	 * browser-panel store + the BrowserLiveView canvas.
	 *
	 * Layout: a right-aligned, left-edge-resizable overlay on wide screens; a
	 * fullscreen overlay at/under the app's 1080px handheld cutoff.
	 */
	import { X, Loader2, MonitorOff, Globe, PlugZap, MousePointer2 } from 'lucide-svelte';
	import { browserPanel } from '$lib/stores/browser-panel.svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import {
		createBrowserStreamClient,
		type BrowserStreamClient,
		type FrameMessageMetadata,
	} from '$lib/browser-panel/stream-client';
	import BrowserLiveView from './BrowserLiveView.svelte';

	interface Props {
		/** Session whose browser page is streamed. */
		sessionId: string;
	}

	let { sessionId }: Props = $props();

	const panel = $derived(browserPanel.current);
	const open = $derived(panel.open);
	const status = $derived(panel.status);
	const url = $derived(panel.url);
	const width = $derived(panel.width);

	let latestFrame = $state<{ data: string; metadata: FrameMessageMetadata } | null>(null);

	// ── Auth token (session cookie), mirroring the chat WS client ──────────
	function getAuthToken(): string | null {
		if (typeof document === 'undefined') return null;
		for (const cookie of document.cookie.split(';')) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') return value ?? null;
		}
		return null;
	}

	// ── Stream client lifecycle: connect only while open && session set ────
	let client: BrowserStreamClient | null = null;

	$effect(() => {
		const isOpen = open;
		const sid = sessionId;
		if (!isOpen || !sid) return;

		latestFrame = null;
		browserPanel.setStatus('connecting');
		const c = createBrowserStreamClient({
			sessionId: sid,
			getAuthToken,
			onStatus: (s, u) => browserPanel.setStatus(s, u),
			onFrame: (data, metadata) => {
				latestFrame = { data, metadata };
			},
			onError: (message) => console.warn('[BrowserPanel] stream error:', message),
			onClose: (wasClean) => {
				if (!wasClean) browserPanel.setStatus('disconnected');
			},
		});
		client = c;
		c.connect();

		return () => {
			c.destroy();
			if (client === c) client = null;
			latestFrame = null;
		};
	});

	function sendInput(payload: Record<string, unknown>) {
		client?.send(payload);
	}

	function close() {
		browserPanel.closePanel(sessionId);
	}

	// ── Fullscreen (handheld) detection ────────────────────────────────────
	function computeFullscreen(): boolean {
		return typeof window !== 'undefined' && isPanelFullscreenWidth(window.innerWidth);
	}
	let isFullscreen = $state(computeFullscreen());
	function handleResize() {
		isFullscreen = computeFullscreen();
	}

	// ── Left-edge drag resize (docked mode only) ───────────────────────────
	let isResizing = $state(false);
	function startResize(e: PointerEvent) {
		if (isFullscreen) return; // fullscreen overlay isn't resizable
		e.preventDefault();
		isResizing = true;
		const startX = e.clientX;
		const startWidth = width;

		function onMove(ev: PointerEvent) {
			// Panel is docked at the right edge → dragging left widens it.
			browserPanel.setWidth(startWidth + (startX - ev.clientX));
		}
		function onUp() {
			isResizing = false;
			window.removeEventListener('pointermove', onMove);
			window.removeEventListener('pointerup', onUp);
		}
		window.addEventListener('pointermove', onMove);
		window.addEventListener('pointerup', onUp);
	}

	// ── Status presentation ────────────────────────────────────────────────
	const statusLabel = $derived(
		{
			live: 'Live',
			connecting: 'Connecting',
			disconnected: 'Disconnected',
			no_page: 'No page',
			browser_off: 'Browser off',
		}[status],
	);

	const displayUrl = $derived(url || 'No page open');
	const isLive = $derived(status === 'live');
</script>

<svelte:window onresize={handleResize} />

{#if open}
	<aside
		class="browser-panel"
		class:fullscreen={isFullscreen}
		class:resizing={isResizing}
		style={isFullscreen ? '' : `width: ${width}px`}
		aria-label="Live browser view"
	>
		{#if !isFullscreen}
			<div
				class="resize-handle"
				onpointerdown={startResize}
				role="separator"
				aria-orientation="vertical"
				aria-label="Resize browser panel"
			>
				<div class="resize-handle-bar"></div>
			</div>
		{/if}

		<header class="panel-header">
			<span class="status-dot status-{status}" aria-hidden="true"></span>
			<div class="header-text">
				<span class="status-label">{statusLabel}</span>
				<span class="url" title={displayUrl}>{displayUrl}</span>
			</div>
			<button class="close-btn" onclick={close} aria-label="Close browser panel">
				<X size={16} strokeWidth={1.5} />
			</button>
		</header>

		<div class="panel-body">
			{#if isLive}
				<BrowserLiveView frame={latestFrame} interactive={true} onInput={sendInput} />
			{:else}
				<div class="empty-state">
					{#if status === 'connecting'}
						<Loader2 class="spin" size={26} strokeWidth={1.5} />
						<p class="empty-title">Connecting to the live view</p>
						<p class="empty-copy">Hooking up to the agent's browser…</p>
					{:else if status === 'no_page'}
						<Globe size={26} strokeWidth={1.5} />
						<p class="empty-title">No page open yet</p>
						<p class="empty-copy">The view will appear as soon as the agent opens a page.</p>
					{:else if status === 'browser_off'}
						<MonitorOff size={26} strokeWidth={1.5} />
						<p class="empty-title">Browser isn't running</p>
						<p class="empty-copy">Nothing to show until the agent starts a browser session.</p>
					{:else}
						<PlugZap size={26} strokeWidth={1.5} />
						<p class="empty-title">Disconnected</p>
						<p class="empty-copy">Lost the live feed — trying to reconnect.</p>
					{/if}
				</div>
			{/if}
		</div>

		<footer class="panel-note">
			<MousePointer2 size={12} strokeWidth={1.5} />
			<span>The agent may be controlling this page too.</span>
		</footer>
	</aside>
{/if}

<style>
	.browser-panel {
		position: absolute;
		inset: 0 0 0 auto;
		z-index: 20;
		min-height: 0;
		display: flex;
		flex-direction: column;
		background: var(--background);
		border-left: 1px solid var(--border);
		overflow: hidden;
	}

	.browser-panel.resizing {
		user-select: none;
	}
	.browser-panel.resizing :global(*) {
		pointer-events: none;
	}

	/* Fullscreen overlay at/under the handheld cutoff. */
	.browser-panel.fullscreen {
		position: fixed;
		inset: 0;
		z-index: 95;
		width: 100% !important;
		border-left: none;
	}

	@media (max-width: 1080px) {
		.browser-panel {
			position: fixed;
			inset: 0;
			z-index: 95;
			width: 100% !important;
			border-left: none;
		}
	}

	/* ── Resize handle (left edge) ─────────────────────────────────────── */
	.resize-handle {
		position: absolute;
		top: 0;
		left: -4px;
		bottom: 0;
		width: 8px;
		z-index: 10;
		cursor: col-resize;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.resize-handle-bar {
		width: 3px;
		height: 48px;
		border-radius: 2px;
		background: color-mix(in oklch, var(--border) 40%, transparent);
		transition: background 150ms ease, height 150ms ease;
	}
	.resize-handle:hover .resize-handle-bar {
		background: color-mix(in oklch, var(--ring) 60%, transparent);
		height: 64px;
	}

	/* ── Header ─────────────────────────────────────────────────────────── */
	.panel-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px 8px 12px;
		min-height: 40px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
		background: var(--muted-foreground);
	}
	.status-live {
		background: var(--success);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--success) 25%, transparent);
	}
	.status-connecting {
		background: var(--warning);
		animation: dot-pulse 1.4s ease-in-out infinite;
	}
	.status-disconnected {
		background: var(--destructive);
	}
	.status-no_page,
	.status-browser_off {
		background: var(--muted-foreground);
	}

	@keyframes dot-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}

	.header-text {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
		flex: 1;
	}

	.status-label {
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--muted-foreground);
		line-height: 1;
	}

	.url {
		font-size: 0.75rem;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		font-family: var(--font-mono, monospace);
	}

	.close-btn {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 150ms ease, color 150ms ease;
		flex-shrink: 0;
		-webkit-tap-highlight-color: transparent;
	}
	.close-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.close-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* ── Body ───────────────────────────────────────────────────────────── */
	.panel-body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--muted) 30%, transparent);
	}

	.empty-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 32px 24px;
		text-align: center;
		color: var(--muted-foreground);
	}

	.empty-title {
		margin: 4px 0 0;
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.empty-copy {
		margin: 0;
		font-size: 0.75rem;
		max-width: 30ch;
		color: color-mix(in oklch, var(--muted-foreground) 85%, transparent);
		line-height: 1.4;
	}

	.empty-state :global(.spin) {
		animation: spin 1s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* ── Persistent shared-control note ─────────────────────────────────── */
	.panel-note {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		font-size: 0.6875rem;
		color: color-mix(in oklch, var(--muted-foreground) 80%, transparent);
		flex-shrink: 0;
	}
	.panel-note :global(svg) {
		flex-shrink: 0;
	}

	@media (prefers-reduced-motion: reduce) {
		.status-connecting,
		.empty-state :global(.spin) {
			animation: none;
		}
		.resize-handle-bar {
			transition: none;
		}
	}
</style>
