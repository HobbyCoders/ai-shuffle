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
	import {
		X,
		Loader2,
		MonitorOff,
		Globe,
		PlugZap,
		MousePointer2,
		ArrowLeft,
		ArrowRight,
		RotateCw,
		X as StopIcon,
	} from 'lucide-svelte';
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
	const pageLoading = $derived(panel.pageLoading);

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
	// $state so toolbar enable/disable reacts to connect/disconnect cycles.
	let client = $state<BrowserStreamClient | null>(null);
	// Last session we held a frame for. The canvas keeps its last picture across
	// reconnects and transient status changes; only a genuine session switch
	// invalidates it (showing another session's page would be wrong).
	let framedSessionId: string | null = null;

	$effect(() => {
		const isOpen = open;
		const sid = sessionId;
		if (!isOpen || !sid) return;

		if (framedSessionId !== sid) {
			latestFrame = null;
			framedSessionId = sid;
			browserPanel.resetPageTracking();
		}
		browserPanel.setStatus('connecting');
		const c = createBrowserStreamClient({
			sessionId: sid,
			getAuthToken,
			onStatus: (s, u) => browserPanel.setStatus(s, u),
			onPage: (u, loading) => browserPanel.setPage(u, loading),
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

	// ── Toolbar: nav buttons + editable address bar ──────────────────────
	let addressDraft = $state<string | null>(null); // non-null while editing
	let addressEl: HTMLInputElement | undefined = $state();

	function startEditing() {
		addressDraft = displayUrl === 'No page open' ? '' : (url ?? '');
		// Select everything so typing replaces, like a real browser.
		queueMicrotask(() => {
			addressEl?.focus();
			addressEl?.select();
		});
	}

	function commitAddress() {
		if (addressDraft === null) return;
		const raw = addressDraft.trim();
		addressDraft = null;
		if (!raw || !client?.isConnected()) return;
		const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw) ? raw : `https://${raw}`;
		client.send({ type: 'navigate', url: withScheme });
	}

	function cancelEditing() {
		addressDraft = null;
	}

	function sendCommand(command: string) {
		if (!client?.isConnected()) return;
		client.send({ type: 'command', command });
	}

	const canNavigate = $derived(client !== null && client.isConnected() && status === 'live');

	const displayUrl = $derived(url || 'No page open');
	const isLive = $derived(status === 'live');
	/** A frame is on the canvas — keep showing it, whatever the socket is doing. */
	const hasFrame = $derived(latestFrame !== null);
	/** Short overlay text shown over a stale frame while the stream isn't live. */
	const overlayLabel = $derived(
		{
			live: '',
			connecting: 'Reconnecting…',
			disconnected: 'Reconnecting…',
			no_page: 'No page',
			browser_off: 'Browser off',
		}[status],
	);
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
			<div class="nav-buttons">
				<button
					class="tool-btn"
					onclick={() => sendCommand('back')}
					disabled={!canNavigate}
					aria-label="Go back"
					title="Back"
				>
					<ArrowLeft size={15} strokeWidth={1.75} />
				</button>
				<button
					class="tool-btn"
					onclick={() => sendCommand('forward')}
					disabled={!canNavigate}
					aria-label="Go forward"
					title="Forward"
				>
					<ArrowRight size={15} strokeWidth={1.75} />
				</button>
				<button
					class="tool-btn"
					class:loading={pageLoading}
					onclick={() => sendCommand(pageLoading ? 'stop' : 'reload')}
					disabled={!canNavigate}
					aria-label={pageLoading ? 'Stop loading' : 'Reload page'}
					title={pageLoading ? 'Stop' : 'Reload'}
				>
					{#if pageLoading}
						<StopIcon size={14} strokeWidth={2} />
					{:else}
						<RotateCw size={14} strokeWidth={1.75} />
					{/if}
				</button>
			</div>

			<form
				class="address-bar"
				role="presentation"
				onsubmit={(e) => {
					e.preventDefault();
					commitAddress();
				}}
			>
				{#if addressDraft !== null}
					<input
						bind:this={addressEl}
						bind:value={addressDraft}
						class="address-input"
						type="text"
						spellcheck="false"
						autocomplete="off"
						aria-label="Page address"
						onkeydown={(e) => {
							if (e.key === 'Escape') cancelEditing();
						}}
						onblur={commitAddress}
					/>
				{:else}
					<button
						type="button"
						class="address-display"
						onclick={startEditing}
						disabled={!canNavigate}
						title={canNavigate ? 'Edit address' : displayUrl}
					>
						<span class="status-dot status-{status}" aria-hidden="true"></span>
						<span class="address-text">{displayUrl}</span>
					</button>
				{/if}
			</form>

			<button class="close-btn" onclick={close} aria-label="Close browser panel">
				<X size={16} strokeWidth={1.5} />
			</button>
		</header>

		<div class="panel-body">
			{#if hasFrame}
				<!-- The canvas stays mounted across reconnects and target re-attaches:
				     unmounting it made every transient status blank the preview. -->
				<div class="live-holder">
					<BrowserLiveView frame={latestFrame} interactive={isLive} onInput={sendInput} />
					{#if !isLive}
						<div class="stream-badge" role="status">
							{#if status === 'connecting' || status === 'disconnected'}
								<Loader2 class="spin" size={12} strokeWidth={2} />
							{/if}
							<span>{overlayLabel}</span>
						</div>
					{/if}
				</div>
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

	/* ── Header / toolbar ──────────────────────────────────────────────── */
	.panel-header {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 10px 6px 10px;
		min-height: 44px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.nav-buttons {
		display: flex;
		align-items: center;
		gap: 2px;
		flex-shrink: 0;
	}

	.tool-btn {
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
		transition: background 150ms ease, color 150ms ease, opacity 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tool-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.tool-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.tool-btn:disabled {
		opacity: 0.4;
		cursor: default;
	}
	/* Mid-navigation: the reload button is now a stop button — pulse it so the
	   mode swap reads as "busy", not as a broken icon. */
	.tool-btn.loading {
		color: var(--foreground);
		animation: dot-pulse 1.4s ease-in-out infinite;
	}

	.address-bar {
		flex: 1;
		min-width: 0;
		display: flex;
	}

	.address-display {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 5px 10px;
		background: color-mix(in oklch, var(--muted) 55%, transparent);
		border: 1px solid transparent;
		border-radius: 999px;
		color: var(--foreground);
		font-size: 0.75rem;
		font-family: var(--font-mono, monospace);
		cursor: text;
		text-align: left;
		transition: background 150ms ease, border-color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.address-display:hover:not(:disabled) {
		background: color-mix(in oklch, var(--muted) 80%, transparent);
		border-color: color-mix(in oklch, var(--border) 60%, transparent);
	}
	.address-display:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.address-display:disabled {
		cursor: default;
	}

	.address-text {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.address-input {
		flex: 1;
		min-width: 0;
		padding: 5px 12px;
		background: var(--background);
		border: 1px solid var(--ring);
		border-radius: 999px;
		color: var(--foreground);
		font-size: 0.75rem;
		font-family: var(--font-mono, monospace);
		outline: none;
	}
	.address-input::placeholder {
		color: var(--muted-foreground);
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

	/* Holds the live canvas plus the non-blocking status overlay. */
	.live-holder {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	/* Small corner pill over a still-visible frame while the stream isn't live. */
	.stream-badge {
		position: absolute;
		top: 8px;
		right: 8px;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 5px;
		padding: 3px 9px;
		font-size: 0.6875rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--foreground);
		background: color-mix(in oklch, var(--popover) 88%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		border-radius: 999px;
		backdrop-filter: blur(6px);
		pointer-events: none;
	}
	.stream-badge :global(.spin) {
		animation: spin 1s linear infinite;
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
		.tool-btn.loading,
		.stream-badge :global(.spin),
		.empty-state :global(.spin) {
			animation: none;
		}
		.resize-handle-bar {
			transition: none;
		}
	}
</style>
