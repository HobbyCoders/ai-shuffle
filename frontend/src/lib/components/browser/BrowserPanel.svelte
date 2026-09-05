<script lang="ts">
	/**
	 * BrowserPanel — right-docked live browser view.
	 *
	 * Owns the screencast WebSocket lifecycle for the active session: it connects
	 * only while the panel is open and a session is set, tears the socket down on
	 * close / session switch, and folds `status` / `frame` frames into the
	 * browser-panel store + the BrowserLiveView canvas.
	 *
	 * Point-and-prompt: the header's inspect toggle switches the live view into
	 * element selection — clicks hit-test the page (backend `inspect` message)
	 * instead of being forwarded, and the selected element feeds a small prompt
	 * composer that routes "change this" requests to the agent with the
	 * element's selector/context attached. Code stays the single source of
	 * truth; the agent makes the edit and the screencast shows the reload.
	 *
	 * Layout: a right-aligned, left-edge-resizable overlay on wide screens; a
	 * fullscreen overlay at/under the app's 1080px handheld cutoff. The live-view
	 * box only affects page geometry when its controlling viewer explicitly
	 * chooses Fit. Fixed agent viewports remain authoritative through resizes
	 * and reconnects.
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
		Crosshair,
		ExternalLink,
		Send,
		Check,
		MessageSquare,
		Keyboard,
	} from 'lucide-svelte';
	import { browserPanel } from '$lib/stores/browser-panel.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import {
		createBrowserStreamClient,
		type BrowserStreamClient,
		type FrameMessageMetadata,
	} from '$lib/browser-panel/stream-client';
	import {
		composeElementPrompt,
		describeElement,
		parseElementHit,
	} from '$lib/browser-panel/inspect';
	import type { PageRect } from '$lib/browser-panel/coords';
	import BrowserLiveView from './BrowserLiveView.svelte';
	import BrowserWorkspaceToolbar from './BrowserWorkspaceToolbar.svelte';
	import {
		ownsControl,
		type BrowserWorkspaceState,
	} from '$lib/browser-panel/workspace';

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
	const inspectMode = $derived(panel.inspectMode);
	const selectedElement = $derived(panel.selectedElement);

	let latestFrame = $state<{
		data: string;
		metadata: FrameMessageMetadata;
	} | null>(null);

	// ── Auth token (session cookie), mirroring the chat WS client ──────────
	function getAuthToken(): string | null {
		if (typeof document === 'undefined') return null;
		for (const cookie of document.cookie.split(';')) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') return value ?? null;
		}
		return null;
	}

	// ── Inspect state (hover highlight is view-local, selection is stored) ─
	let hoverRect = $state<PageRect | null>(null);
	const selectionRect = $derived(selectedElement?.rect ?? null);

	function handleInspectHit(mode: 'hover' | 'select', raw: unknown) {
		const el = parseElementHit(raw);
		if (mode === 'hover') {
			hoverRect = el?.rect ?? null;
			return;
		}
		hoverRect = null;
		browserPanel.setSelectedElement(el);
		if (el) {
			// Land the cursor in the composer so "click, type, send" flows.
			queueMicrotask(() => promptEl?.focus());
		}
	}

	// ── Stream client lifecycle: connect only while open && session set ────
	// $state so toolbar enable/disable reacts to connect/disconnect cycles.
	let client = $state<BrowserStreamClient | null>(null);
	// isConnected() is not reactive — track OPEN ourselves so the toolbar
	// enables as soon as the socket is up, not only after a `live` status.
	let streamOpen = $state(false);
	let workspace = $state<BrowserWorkspaceState | null>(null);
	let viewerId = $state<string | null>(null);
	let commandError = $state('');
	const humanControl = $derived(streamOpen && ownsControl(workspace, viewerId));
	let frameIdentity = $state<string | null>(null);
	let liveView: BrowserLiveView | undefined = $state();
	let keyboardOpen = $state(false);
	// CSS viewport metadata, not JPEG dimensions (the stream may be downscaled).
	const frameMatchesWorkspace = $derived(
		!!workspace?.available && !!latestFrame &&
		latestFrame.metadata.runtime_id === workspace.runtime_id &&
		latestFrame.metadata.tab_id === workspace.active_tab_id &&
		latestFrame.metadata.deviceWidth === workspace.viewport.width &&
		latestFrame.metadata.deviceHeight === workspace.viewport.height,
	);
	let commandSequence = 0;
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
			hoverRect = null;
			browserPanel.resetPageTracking();
		}
		workspace = null;
		viewerId = null;
		commandError = '';
		browserPanel.setStatus('connecting');
		const c = createBrowserStreamClient({
			sessionId: sid,
			getAuthToken,
			onStatus: (s, u) => browserPanel.setStatus(s, u),
			onPage: (u, loading) => browserPanel.setPage(u, loading),
			onFrame: (data, metadata) => {
				if (!workspace?.available || !workspace.active_tab_id) return;
				latestFrame = { data, metadata };
			},
			onInspectHit: handleInspectHit,
			onError: (message) => {
				commandError = message;
			},
			onHello: (id) => {
				viewerId = id;
			},
			onCommandResult: (_id, ok, error) => {
				if (!ok)
					commandError =
						error || 'Command failed. Try again after checking control.';
			},
			onWorkspace: (state) => {
				workspace = state;
				if (state) {
					const identity = `${state.runtime_id}:${state.active_tab_id}`;
					if (frameIdentity !== identity) {
						latestFrame = null;
						hoverRect = null;
						browserPanel.resetPageTracking();
						frameIdentity = identity;
					}
					browserPanel.setPage(
						state.tabs.find((t) => t.id === state.active_tab_id)?.url ?? null,
						state.busy,
					);
				}
			},
			onOpen: () => {
				streamOpen = true;
			},
			onClose: (wasClean) => {
				streamOpen = false;
				if (!wasClean) browserPanel.setStatus('disconnected');
			},
		});
		client = c;
		c.connect();

		return () => {
			c.destroy();
			streamOpen = false;
			if (client === c) client = null;
		};
	});

	// Cross-page navigation invalidates the selection: the element (and its
	// rect) belong to the page it was picked on. Same-URL reloads keep it —
	// that's the hot-reload-after-edit case the composer exists for.
	let selectionUrl: string | null = null;
	$effect(() => {
		const u = url;
		if (selectedElement === null) {
			selectionUrl = u;
			return;
		}
		if (selectionUrl === null) selectionUrl = u;
		else if (u !== selectionUrl) {
			selectionUrl = u;
			hoverRect = null;
			browserPanel.setSelectedElement(null);
		}
	});

	function sendIdentified(payload: Record<string, unknown>) {
		if (!workspace?.available || !client) return false;
		return client.send({
			...payload,
			id: `panel-${Date.now()}-${++commandSequence}`,
			runtime_id: workspace.runtime_id,
			tab_id: workspace.active_tab_id,
		});
	}
	function workspaceCommand(op: string, args: Record<string, unknown>) {
		if (!['takeover', 'release'].includes(op) && !humanControl) {
			commandError = 'Take control before changing the browser.';
			return;
		}
		commandError = '';
		if (!sendIdentified({ type: 'workspace-command', op, args }))
			commandError = 'Command not sent. Check the connection and try again.';
	}

	function sendInput(payload: Record<string, unknown>) {
		if (!humanControl || !(payload.kind === 'text' || payload.kind === 'key'
			? keyboardEnabled : frameMatchesWorkspace)) return;
		sendIdentified(payload);
	}

	function sendInspect(x: number, y: number, mode: 'hover' | 'select') {
		if (streamOpen && frameMatchesWorkspace)
			sendIdentified({ type: 'inspect', x, y, mode });
	}

	// ── Viewport follows the live-view box ──────────────────────────────
	// Headless Chrome does not resize with the docked panel. Without this
	// the streamed page stays at its launch (or last agent-resize) size,
	// so this app's 1080px mobile shell never lifts no matter how wide
	// the user drags the panel.
	let panelBodyEl: HTMLElement | undefined = $state();
	let lastViewportW = 0;
	let lastViewportH = 0;
	let viewportTimer: ReturnType<typeof setTimeout> | null = null;

	function sendViewportNow(width: number, height: number) {
		if (!humanControl || workspace?.viewport.mode !== 'fit') return;
		const w = Math.min(3840, Math.max(240, Math.round(width)));
		const h = Math.min(2160, Math.max(240, Math.round(height)));
		if (w < 200 || h < 200) return;
		if (w === lastViewportW && h === lastViewportH) return;
		if (!client?.isConnected()) return;
		lastViewportW = w;
		lastViewportH = h;
		workspaceCommand('viewport', { mode: 'fit', width: w, height: h });
	}

	function scheduleViewport(width: number, height: number) {
		if (viewportTimer) clearTimeout(viewportTimer);
		viewportTimer = setTimeout(() => {
			viewportTimer = null;
			sendViewportNow(width, height);
		}, 100);
	}

	// Workspace acknowledgments replace the state object on every command.
	// Observe stable eligibility/identity, not that object: resetting the last
	// size on an acknowledgment creates an identical resize-command feedback loop.
	const fitViewportEnabled = $derived(humanControl && workspace?.viewport.mode === 'fit');
	const fitViewportTarget = $derived(
		workspace ? `${workspace.runtime_id}:${workspace.active_tab_id}` : null,
	);
	$effect(() => {
		const el = panelBodyEl;
		const target = fitViewportTarget;
		const connection = client;
		if (!fitViewportEnabled || !target || !connection) return;
		lastViewportW = 0;
		lastViewportH = 0;
		if (!el || typeof ResizeObserver === 'undefined') return;
		const report = () => {
			const r = el.getBoundingClientRect();
			scheduleViewport(r.width, r.height);
		};
		const ro = new ResizeObserver(report);
		ro.observe(el);
		report();
		return () => {
			ro.disconnect();
			if (viewportTimer) {
				clearTimeout(viewportTimer);
				viewportTimer = null;
			}
		};
	});

	function toggleInspect() {
		hoverRect = null;
		browserPanel.setInspectMode(!inspectMode);
	}

	function exitInspect() {
		hoverRect = null;
		browserPanel.setInspectMode(false);
	}

	function clearSelection() {
		browserPanel.setSelectedElement(null);
	}

	function close() {
		browserPanel.closePanel(sessionId);
	}

	// ── Point-and-prompt composer ──────────────────────────────────────────
	let promptText = $state('');
	let promptEl: HTMLTextAreaElement | undefined = $state();
	let sentFlash = $state(false);
	let sentFlashTimer: ReturnType<typeof setTimeout> | null = null;

	function sendElementPrompt() {
		const el = selectedElement;
		const text = promptText.trim();
		if (!el || !text) return;
		void activeSession.sendMessage(composeElementPrompt(el, url, text));
		promptText = '';
		sentFlash = true;
		if (sentFlashTimer !== null) clearTimeout(sentFlashTimer);
		sentFlashTimer = setTimeout(() => {
			sentFlash = false;
			sentFlashTimer = null;
		}, 3000);
	}

	function onPromptKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			sendElementPrompt();
		} else if (e.key === 'Escape') {
			clearSelection();
		}
	}

	/** Fullscreen (handheld) mode: jump back to the chat to watch the reply. */
	function viewChat() {
		browserPanel.closePanel(sessionId);
	}

	// ── Fullscreen (handheld) detection ────────────────────────────────────
	function computeFullscreen(): boolean {
		return (
			typeof window !== 'undefined' && isPanelFullscreenWidth(window.innerWidth)
		);
	}
	let isFullscreen = $state(computeFullscreen());
	function handleResize() {
		isFullscreen = computeFullscreen();
	}

	// ── Left-edge drag resize (docked mode only) ───────────────────────────
	let panelEl: HTMLElement | undefined = $state();
	let isResizing = $state(false);
	function startResize(e: PointerEvent) {
		if (isFullscreen) return; // fullscreen overlay isn't resizable
		e.preventDefault();
		isResizing = true;
		const startX = e.clientX;
		// Use the rendered width so a CSS-capped panel (max-width: 100%)
		// starts shrinking immediately instead of eating leftover stored px.
		const rendered = panelEl?.getBoundingClientRect().width;
		const startWidth = rendered && rendered > 0 ? rendered : width;
		const parentWidth =
			panelEl?.parentElement?.clientWidth ?? Number.POSITIVE_INFINITY;

		function onMove(ev: PointerEvent) {
			// Panel is docked at the right edge → dragging left widens it.
			// Ceiling is the chat inner panel (`.chat-row`), which stops at the sidebar.
			browserPanel.setWidth(
				Math.min(parentWidth, startWidth + (startX - ev.clientX)),
			);
		}
		function onUp() {
			isResizing = false;
			window.removeEventListener('pointermove', onMove);
			window.removeEventListener('pointerup', onUp);
		}
		window.addEventListener('pointermove', onMove);
		window.addEventListener('pointerup', onUp);
	}

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
		if (!raw || !canNavigate || !client?.isConnected()) return;
		const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw)
			? raw
			: `https://${raw}`;
		if (!/^https?:\/\//i.test(withScheme)) {
			commandError = 'Use an http or https address.';
			return;
		}
		workspaceCommand('navigate', { url: withScheme });
	}

	function cancelEditing() {
		addressDraft = null;
	}

	function sendCommand(command: string) {
		if (!client?.isConnected()) return;
		workspaceCommand('command', { command });
	}

	// Toolbar is usable whenever the panel socket is up and Chromium is on.
	// Gating on `live` left back/forward/reload/address disabled for a visible
	// page whenever status lagged on connecting/no_page (isConnected() also
	// wasn't reactive, so a connecting→connecting handshake never re-enabled).
	const canNavigate = $derived(humanControl && !!workspace?.active_tab_id);

	const displayUrl = $derived(url || 'No page open');
	/** Address text with the scheme trimmed, like a real browser's omnibox. */
	const compactUrl = $derived(
		url ? url.replace(/^https?:\/\//, '') : 'No page open',
	);
	/** Only genuinely web-openable pages get the open-in-new-tab affordance. */
	const externalHref = $derived(url && /^https?:\/\//.test(url) ? url : null);
	const isLive = $derived(
		streamOpen &&
			!!workspace?.available &&
			!!workspace.active_tab_id &&
			status === 'live',
	);
	const keyboardEnabled = $derived(isLive && humanControl && !!latestFrame &&
		latestFrame.metadata.runtime_id === workspace?.runtime_id &&
		latestFrame.metadata.tab_id === workspace?.active_tab_id);
	/** A frame is on the canvas — keep showing it, whatever the socket is doing. */
	const hasFrame = $derived(latestFrame !== null);
	/** Short overlay text shown over a stale frame while the stream isn't live. */
	const overlayLabel = $derived(
		{
			live: '',
			connecting: 'Reconnecting…',
			disconnected: 'Reconnecting · last image is stale',
			no_page: 'No page',
			browser_off: 'Browser off',
		}[status],
	);

	// Debounced status badge: agent-driven tab switches and upstream re-attaches
	// produce sub-second connecting→live blips. Flashing "Reconnecting…" over a
	// perfectly good frame for each one made the panel look broken (the flicker
	// bug) — only a non-live spell that actually lasts earns the badge.
	const BADGE_DELAY_MS = 1200;
	let showStreamBadge = $state(false);
	$effect(() => {
		if (isLive) {
			showStreamBadge = false;
			return;
		}
		const t = setTimeout(() => (showStreamBadge = true), BADGE_DELAY_MS);
		return () => clearTimeout(t);
	});

	const chipLabel = $derived(
		selectedElement ? describeElement(selectedElement) : '',
	);
</script>

<svelte:window onresize={handleResize} />

{#if open}
	<aside
		bind:this={panelEl}
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

		<BrowserWorkspaceToolbar
			{workspace}
			{viewerId}
			connected={streamOpen}
			onCommand={workspaceCommand}
		/>
		{#if commandError}<div class="workspace-banner" role="alert">
				{commandError}<button
					onclick={() => (commandError = '')}
					aria-label="Dismiss browser error">×</button
				>
			</div>{/if}
		{#if streamOpen && !workspace?.available}<div
				class="workspace-banner"
				role="status"
			>
				Browser runtime unavailable. Ask the agent to start the browser.
			</div>{:else if !streamOpen}<div class="workspace-banner" role="status">
				Connecting to the browser workspace…{hasFrame
					? ' Last image is stale.'
					: ''}
			</div>{/if}
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
					class="tool-btn nav-forward"
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
						autocapitalize="off"
						enterkeyhint="go"
						inputmode="url"
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
						title={canNavigate ? `${displayUrl} — click to edit` : displayUrl}
					>
						<span class="status-dot status-{status}" aria-hidden="true"></span>
						<span class="address-text">{compactUrl}</span>
					</button>
				{/if}
			</form>

			<div class="header-actions">
				<button
					class="tool-btn keyboard-toggle"
					class:active={keyboardOpen}
					disabled={!keyboardOpen && (!keyboardEnabled || inspectMode || !liveView)}
					aria-label={keyboardOpen ? 'Hide keyboard' : 'Show keyboard'}
					aria-pressed={keyboardOpen}
					title={keyboardOpen ? 'Hide keyboard' : 'Show keyboard'}
					onpointerdown={(e) => e.preventDefault()}
					onclick={() => {
						if (keyboardOpen) liveView?.dismissKeyboard();
						else liveView?.openKeyboard();
					}}
				>
					<Keyboard size={16} strokeWidth={1.75} />
				</button>
				<button
					class="tool-btn inspect-toggle"
					class:active={inspectMode}
					onclick={toggleInspect}
					disabled={!isLive}
					aria-label="Select an element to edit with AI"
					aria-pressed={inspectMode}
					title="Point & prompt — click an element, then tell the AI what to change"
				>
					<Crosshair size={15} strokeWidth={1.75} />
				</button>
				{#if externalHref}
					<a
						class="tool-btn"
						href={externalHref}
						target="_blank"
						rel="noopener noreferrer"
						aria-label="Open page in a new browser tab"
						title="Open in new tab"
					>
						<ExternalLink size={14} strokeWidth={1.75} />
					</a>
				{/if}
				<button
					class="tool-btn close-btn"
					onclick={close}
					aria-label="Close browser panel"
				>
					<X size={16} strokeWidth={1.5} />
				</button>
			</div>
		</header>

		<div class="panel-body" bind:this={panelBodyEl}>
			{#if hasFrame}
				<!-- The canvas stays mounted across reconnects and target re-attaches:
				     unmounting it made every transient status blank the preview. -->
				<div class="live-holder">
					{#key frameIdentity}
						<BrowserLiveView
							bind:this={liveView}
							bind:keyboardOpen
							{keyboardEnabled}
							frame={latestFrame}
							interactive={isLive && frameMatchesWorkspace && (humanControl || inspectMode)}
							viewport={workspace?.viewport}
							inspecting={inspectMode && isLive}
							{hoverRect}
							{selectionRect}
							onInput={sendInput}
							onInspect={sendInspect}
							onHoverLeave={() => (hoverRect = null)}
							onExitInspect={exitInspect}
						/>
					{/key}
					{#if showStreamBadge}
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
					{:else if status === 'live'}
						<!-- Attached and healthy, but the page hasn't painted a frame
						     yet (background tab / nothing to draw). Not an error. -->
						<Loader2 class="spin" size={26} strokeWidth={1.5} />
						<p class="empty-title">Connected</p>
						<p class="empty-copy">
							Waiting for the page to draw its first frame…
						</p>
					{:else if status === 'no_page'}
						<Globe size={26} strokeWidth={1.5} />
						<p class="empty-title">No page open yet</p>
						<p class="empty-copy">
							Take control, then choose New tab to open the first page.
						</p>
					{:else if status === 'browser_off'}
						<MonitorOff size={26} strokeWidth={1.5} />
						<p class="empty-title">Browser isn't running</p>
						<p class="empty-copy">
							Nothing to show until the agent starts a browser session.
						</p>
					{:else}
						<PlugZap size={26} strokeWidth={1.5} />
						<p class="empty-title">Disconnected</p>
						<p class="empty-copy">Lost the live feed — trying to reconnect.</p>
					{/if}
				</div>
			{/if}
		</div>

		{#if selectedElement}
			<div
				class="composer"
				aria-label="Ask the AI to change the selected element"
			>
				<div class="composer-chip-row">
					<span class="element-chip" title={selectedElement.selector}>
						<Crosshair size={12} strokeWidth={2} />
						<span class="element-chip-label">{chipLabel}</span>
					</span>
					{#if selectedElement.text}
						<span class="element-text" title={selectedElement.text}>
							“{selectedElement.text}”
						</span>
					{/if}
					<button
						class="chip-clear"
						onclick={clearSelection}
						aria-label="Clear selected element"
						title="Clear selection"
					>
						<X size={13} strokeWidth={1.75} />
					</button>
				</div>
				<form
					class="composer-form"
					onsubmit={(e) => {
						e.preventDefault();
						sendElementPrompt();
					}}
				>
					<textarea
						bind:this={promptEl}
						bind:value={promptText}
						class="composer-input"
						rows="2"
						placeholder="Describe the change — “make this button bigger and blue”"
						aria-label="Describe the change for the selected element"
						onkeydown={onPromptKeydown}></textarea>
					<button
						type="submit"
						class="composer-send"
						disabled={!promptText.trim()}
						aria-label="Send to the AI"
						title="Send to the AI (Enter)"
					>
						<Send size={15} strokeWidth={1.75} />
					</button>
				</form>
				{#if sentFlash}
					<div class="composer-sent" role="status">
						<Check size={13} strokeWidth={2} />
						<span>Sent — the preview reloads when the agent applies it.</span>
						{#if isFullscreen}
							<button class="composer-viewchat" onclick={viewChat}>
								<MessageSquare size={12} strokeWidth={1.75} />
								View chat
							</button>
						{/if}
					</div>
				{/if}
			</div>
		{:else if inspectMode}
			<footer class="panel-note inspect-note">
				<Crosshair size={12} strokeWidth={1.75} />
				<span
					>Point &amp; prompt — click any element in the preview to select it.</span
				>
				<button class="note-exit" onclick={exitInspect}>Done</button>
			</footer>
		{:else}
			<footer class="panel-note">
				<MousePointer2 size={12} strokeWidth={1.5} />
				<span
					>{humanControl
						? 'You control this page. Return control when you are done.'
						: 'View only. Take control to interact with the page.'}</span
				>
			</footer>
		{/if}
	</aside>
{/if}

<style>
	.workspace-banner {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 10px;
		font-size: 11px;
		line-height: 1.5;
		background: var(--muted);
		color: var(--foreground);
		border-bottom: 1px solid var(--border);
	}
	.workspace-banner button {
		border: 0;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.workspace-banner button:focus-visible {
		outline: 2px solid var(--primary);
	}
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
		/* Fill at most the chat inner panel (`.chat-row`), which ends at the sidebar. */
		max-width: 100%;
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
		/* Keep the toolbar/footer clear of notches and home indicators. */
		padding-top: env(safe-area-inset-top, 0px);
		padding-bottom: env(safe-area-inset-bottom, 0px);
	}

	@media (max-width: 1080px) {
		.browser-panel {
			position: fixed;
			inset: 0;
			z-index: 95;
			width: 100% !important;
			border-left: none;
			padding-top: env(safe-area-inset-top, 0px);
			padding-bottom: env(safe-area-inset-bottom, 0px);
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
		transition:
			background 150ms ease,
			height 150ms ease;
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
		padding: 6px 8px;
		min-height: 44px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.nav-buttons,
	.header-actions {
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
		transition:
			background 150ms ease,
			color 150ms ease,
			opacity 150ms ease;
		-webkit-tap-highlight-color: transparent;
		text-decoration: none;
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

	/* Header toggles report their active state without covering the page. */
	.inspect-toggle.active,
	.keyboard-toggle.active {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		color: var(--primary);
	}
	.inspect-toggle.active:hover:not(:disabled),
	.keyboard-toggle.active:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
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
		height: 30px;
		padding: 0 10px;
		background: color-mix(in oklch, var(--muted) 55%, transparent);
		border: 1px solid transparent;
		border-radius: 999px;
		color: var(--foreground);
		font-size: 0.75rem;
		font-family: var(--font-mono, monospace);
		cursor: text;
		text-align: left;
		transition:
			background 150ms ease,
			border-color 150ms ease;
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
		height: 30px;
		padding: 0 12px;
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

	/* ── Handheld header ergonomics: real touch targets, roomier bar ────── */
	@media (max-width: 1080px) {
		.panel-header {
			gap: 4px;
			padding: 8px 10px;
			min-height: 54px;
		}
		.tool-btn {
			width: 38px;
			height: 38px;
			border-radius: calc(var(--radius) - 2px);
		}
		.address-display,
		.address-input {
			height: 38px;
			font-size: 0.8125rem;
		}
	}

	/* The forward button is the least-used control — cede its space to the
	   address bar on very narrow phones. */
	@media (max-width: 400px) {
		.nav-forward {
			display: none;
		}
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

	/* ── Point-and-prompt composer ──────────────────────────────────────── */
	.composer {
		flex-shrink: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px 10px 10px;
		border-top: 1px solid var(--border);
		background: var(--background);
	}

	.composer-chip-row {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}

	.element-chip {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 24px;
		padding: 0 9px;
		max-width: 60%;
		min-width: 0;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 35%, transparent);
		border-radius: 999px;
		color: var(--primary);
		font-size: 0.6875rem;
		font-family: var(--font-mono, monospace);
	}
	.element-chip :global(svg) {
		flex-shrink: 0;
	}
	.element-chip-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.element-text {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.chip-clear {
		margin-left: auto;
		width: 24px;
		height: 24px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition:
			background 150ms ease,
			color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.chip-clear:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.composer-form {
		display: flex;
		align-items: flex-end;
		gap: 6px;
	}

	.composer-input {
		flex: 1;
		min-width: 0;
		resize: none;
		padding: 7px 10px;
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.8125rem;
		font-family: inherit;
		line-height: 1.4;
		outline: none;
		transition: border-color 150ms ease;
	}
	.composer-input:focus {
		border-color: var(--ring);
	}
	.composer-input::placeholder {
		color: var(--muted-foreground);
	}

	.composer-send {
		width: 34px;
		height: 34px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		background: var(--primary);
		border: none;
		border-radius: calc(var(--radius) - 2px);
		color: var(--primary-foreground, #fff);
		cursor: pointer;
		transition: opacity 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.composer-send:hover:not(:disabled) {
		opacity: 0.9;
	}
	.composer-send:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.composer-send:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.composer-sent {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 0.6875rem;
		color: var(--success, var(--primary));
	}

	.composer-viewchat {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-left: auto;
		padding: 2px 8px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--foreground);
		font-size: 0.6875rem;
		cursor: pointer;
	}
	.composer-viewchat:hover {
		background: var(--hover-overlay);
	}

	/* 16px stops iOS Safari from zooming into the textarea on focus. */
	@media (max-width: 1080px) {
		.composer-input {
			font-size: 16px;
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

	.inspect-note {
		color: var(--primary);
		border-top-color: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.note-exit {
		margin-left: auto;
		padding: 2px 10px;
		background: transparent;
		border: 1px solid color-mix(in oklch, var(--primary) 40%, transparent);
		border-radius: 999px;
		color: var(--primary);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
	}
	.note-exit:hover {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
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
