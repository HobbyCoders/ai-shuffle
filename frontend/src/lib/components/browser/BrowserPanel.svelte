<script lang="ts">
	/**
	 * BrowserPanel — right-docked live browser view.
	 *
	 * Owns the screencast WebSocket lifecycle for the active session: it connects
	 * only while the panel is open and a session is set, tears the socket down on
	 * close / session switch, and folds `status` / `frame` frames into the
	 * browser-panel store + the BrowserLiveView canvas.
	 *
	 * Design mode: the header's Design button switches the live view into
	 * element selection. Clicks hit-test the page (backend `inspect` message)
	 * instead of being forwarded, and each picked element becomes a numbered
	 * change in the list beside the page (BrowserDesignRail). The user says
	 * what should change or tries a size or colour, which the page previews
	 * through backend `design` messages, then sends the whole list to the
	 * agent with each element's selector and context attached. Code stays the
	 * single source of truth: the agent makes the edit and the screencast
	 * shows the reload.
	 *
	 * Layout: a right-aligned, left-edge-resizable overlay on the desktop shell
	 * at every width; a fullscreen overlay on the mobile shell. The live-view
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
		PencilRuler,
		ExternalLink,
		Check,
		Maximize2,
		Minimize2,
		Copy,
	} from 'lucide-svelte';
	import { untrack, type Snippet } from 'svelte';
	import { startPanelBrowser, uploadPanelFiles } from '$lib/api/browser-panel';
	import { PanelOperation } from '$lib/browser-panel/panel-operation';
	import { browserPanel } from '$lib/stores/browser-panel.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { platform } from '$lib/stores/platform.svelte';
	import {
		createBrowserStreamClient,
		type BrowserStreamClient,
		type FrameMessageMetadata,
	} from '$lib/browser-panel/stream-client';
	import { isDrawableRect, parseElementHit, type SelectedElement } from '$lib/browser-panel/inspect';
	import {
		composeDesignPrompt,
		isEmptyChange,
		parseDesignDetail,
		safeValue,
		type DesignChange,
		type DesignDetail,
		type DesignProp,
		type DesignScope,
	} from '$lib/browser-panel/design';
	import type { PageRect } from '$lib/browser-panel/coords';
	import BrowserLiveView from './BrowserLiveView.svelte';
	import BrowserDesignRail from './BrowserDesignRail.svelte';
	import BrowserWorkspaceToolbar from './BrowserWorkspaceToolbar.svelte';
	import BrowserWorkspaceNotice from './BrowserWorkspaceNotice.svelte';
	import {
		ownsControl,
		type BrowserEditor,
		type BrowserWorkspaceState,
	} from '$lib/browser-panel/workspace';
	import type { BrowserChoices, PickerChoice } from '$lib/browser-panel/pickers';

	interface Props {
		/** Session whose browser page is streamed. */
		sessionId: string;
		embedded?: boolean;
		panelActions?: Snippet;
	}

	let { sessionId: requestedSessionId, embedded = false, panelActions }: Props = $props();
	// LazyPanel spreads a fresh props object on chat updates. Compare the ID
	// value before effects read it so typing/streaming cannot reconnect the feed.
	const sessionId = $derived(requestedSessionId);

	const panel = $derived(browserPanel.forSession(sessionId));
	const open = $derived(panel.open);
	const status = $derived(panel.status);
	const url = $derived(panel.url);
	const width = $derived(panel.width);
	const pageLoading = $derived(panel.pageLoading);
	/** Design mode: clicks on the page mark elements instead of reaching it. */
	const inspectMode = $derived(panel.inspectMode);

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

	// ── Design mode ───────────────────────────────────────────────────────
	// The user marks elements on the live page, says what each should become
	// (in words, or by trying a size or colour that is previewed on the page),
	// then sends the list to the agent in one message. The list is view-local:
	// it belongs to the page it was marked on.
	let hoverRect = $state<PageRect | null>(null);
	let designChanges = $state<DesignChange[]>([]);
	/** The change open in the editor: a new one, or a working copy of a listed one. */
	let designDraft = $state<DesignChange | null>(null);
	/** Where each marked element is now, in page CSS pixels, by preview key. */
	let designRects = $state<Record<string, PageRect>>({});
	let designOriginal = $state(false);
	let designSending = $state(false);
	let designSent = $state<number | null>(null);
	let designFocus = $state(0);
	let designSeq = 0;
	let expandedForDesign = false;

	const draftIsNew = $derived(!!designDraft && !designChanges.some((c) => c.id === designDraft?.id));
	const designRows = $derived.by(() => {
		const draft = designDraft;
		if (!draft) return designChanges;
		return draftIsNew ? [...designChanges, draft] : designChanges.map((c) => (c.id === draft.id ? draft : c));
	});
	const designHasPreviews = $derived(designRows.some((c) => Object.keys(c.tweaks).length > 0));
	const designPins = $derived(
		inspectMode
			? designRows.flatMap((c, i) => {
					const rect = (c.detail.key && designRects[c.detail.key]) || (c.detail.key ? null : c.element.rect);
					return rect ? [{ rect, label: String(i + 1), active: c.id === designDraft?.id }] : [];
				})
			: [],
	);

	function sameTarget(change: DesignChange, el: SelectedElement, detail: DesignDetail) {
		return detail.key ? change.detail.key === detail.key : change.element.selector === el.selector;
	}

	function sendDesign(payload: Record<string, unknown>) {
		if (streamOpen && frameMatchesWorkspace) sendIdentified({ type: 'design', ...payload });
	}

	/** Put a change's previewed styles on the page (an empty set puts it back). */
	function previewChange(change: DesignChange) {
		if (!change.detail.key) return;
		const styles: Record<string, string> = {};
		for (const [prop, value] of Object.entries(change.tweaks)) {
			const safe = safeValue(value);
			if (safe) styles[prop] = safe;
		}
		designOriginal = false;
		sendDesign({ op: 'style', key: change.detail.key, selector: change.element.selector, scope: change.scope, styles });
	}

	// Sliders and colour pickers fire on every step; the page gets the latest.
	let previewTimer: ReturnType<typeof setTimeout> | null = null;
	function schedulePreview() {
		if (previewTimer !== null) return;
		previewTimer = setTimeout(() => {
			previewTimer = null;
			if (designDraft) previewChange(designDraft);
		}, 40);
	}

	/** Keep or drop the open change, then close the editor. */
	function settleDraft() {
		const draft = designDraft;
		if (!draft) return;
		designDraft = null;
		if (isEmptyChange(draft)) {
			// Emptied on purpose: an existing change with nothing left is removed.
			designChanges = designChanges.filter((c) => c.id !== draft.id);
			return;
		}
		designChanges = designChanges.some((c) => c.id === draft.id)
			? designChanges.map((c) => (c.id === draft.id ? draft : c))
			: [...designChanges, draft];
	}

	function pickDesignElement(el: SelectedElement, detail: DesignDetail) {
		// The list is fixed while it is on its way: a change marked now would be
		// cleared with the sent ones without ever reaching the agent.
		if (designSending) return;
		designSent = null;
		if (detail.key && el.rect) designRects = { ...designRects, [detail.key]: el.rect };
		if (!(designDraft && sameTarget(designDraft, el, detail))) {
			settleDraft();
			const listed = designChanges.find((c) => sameTarget(c, el, detail));
			designDraft = listed
				? { ...listed, tweaks: { ...listed.tweaks } }
				: { id: ++designSeq, element: el, detail, note: '', tweaks: {}, scope: 'one' };
		}
		// A touch keyboard would cover the page the user is still marking up.
		if (!platform.isMobile) designFocus++;
	}

	function setDesignNote(note: string) {
		if (designDraft) designDraft = { ...designDraft, note };
	}

	function setDesignTweak(prop: DesignProp, value: string | null) {
		if (!designDraft) return;
		const tweaks = { ...designDraft.tweaks };
		const safe = value === null ? null : safeValue(value);
		if (safe === null) delete tweaks[prop];
		else tweaks[prop] = safe;
		designDraft = { ...designDraft, tweaks };
		designOriginal = false;
		schedulePreview();
	}

	function setDesignScope(scope: DesignScope) {
		if (!designDraft || designDraft.scope === scope) return;
		designDraft = { ...designDraft, scope };
		previewChange(designDraft);
	}

	function cancelDesignDraft() {
		const draft = designDraft;
		if (!draft) return;
		designDraft = null;
		const listed = designChanges.find((c) => c.id === draft.id);
		// Back to what was saved; a new change leaves the page as it found it.
		if (listed) previewChange(listed);
		else if (draft.detail.key) sendDesign({ op: 'reset', key: draft.detail.key });
	}

	function editDesignChange(id: number) {
		if (designSending || designDraft?.id === id) return;
		settleDraft();
		const listed = designChanges.find((c) => c.id === id);
		if (!listed) return;
		designDraft = { ...listed, tweaks: { ...listed.tweaks } };
		if (!platform.isMobile) designFocus++;
	}

	function removeDesignChange(id: number) {
		if (designSending) return;
		const listed = designRows.find((c) => c.id === id);
		if (designDraft?.id === id) designDraft = null;
		designChanges = designChanges.filter((c) => c.id !== id);
		if (listed?.detail.key) sendDesign({ op: 'reset', key: listed.detail.key });
	}

	function compareDesign(original: boolean) {
		designOriginal = original;
		sendDesign({ op: 'peek', on: original });
	}

	function clearDesign(resetPage: boolean) {
		if (resetPage && designHasPreviews) sendDesign({ op: 'reset' });
		designChanges = [];
		designDraft = null;
		designRects = {};
		designOriginal = false;
	}

	function handleDesignResult(op: string, raw: unknown) {
		const result = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
		if (op === 'rects' && typeof result.rects === 'object' && result.rects !== null) {
			const next: Record<string, PageRect> = {};
			for (const [key, rect] of Object.entries(result.rects as Record<string, unknown>)) {
				if (isDrawableRect(rect)) next[key] = rect;
			}
			designRects = next;
		} else if (op === 'style' && typeof result.key === 'string' && isDrawableRect(result.rect)) {
			designRects = { ...designRects, [result.key]: result.rect };
		} else if (op === 'peek') {
			designOriginal = result.peek === true;
		}
	}

	async function sendDesignChanges() {
		if (designSending || !operation) return;
		settleDraft();
		const list = designChanges;
		if (list.length === 0) return;
		const scope = operation;
		designSending = true;
		commandError = '';
		try {
			const sent = await activeSession.sendMessage(composeDesignPrompt(list, url));
			if (operation !== scope) return;
			if (!sent) throw new Error('Could not send your changes. They are still in the list; try again.');
			// The page goes back to the real code so the agent's edit is what shows next.
			clearDesign(true);
			designSent = list.length;
		} catch (error) {
			if (operation === scope) commandError = error instanceof Error ? error.message : 'Could not send your changes. Try again.';
		} finally {
			if (operation === scope) designSending = false;
		}
	}

	function handleInspectHit(mode: 'hover' | 'select', raw: unknown) {
		const el = parseElementHit(raw);
		if (mode === 'hover') {
			hoverRect = el?.rect ?? null;
			return;
		}
		hoverRect = null;
		if (el) pickDesignElement(el, parseDesignDetail(raw));
	}

	// Pins follow the page: ask where the marked elements are while the mode is on.
	const designTracked = $derived(
		designRows.flatMap((c) => (c.detail.key ? [{ key: c.detail.key, selector: c.element.selector }] : [])),
	);
	$effect(() => {
		if (!inspectMode || designTracked.length === 0) return;
		const items = designTracked.slice(0, 40);
		const ask = () => sendDesign({ op: 'rects', items });
		ask();
		const timer = setInterval(ask, 350);
		return () => clearInterval(timer);
	});

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
	let operation: PanelOperation | null = null;
	let submitting = $state(false);
	let navigationPending = $state(false);
	const loading = $derived(pageLoading || navigationPending);
	let trackedPage = false;
	const replies = new Map<string, { ok: boolean; error?: string; text?: string; choices?: BrowserChoices }>();
	const awaitingReplies = new Set<string>();
	// Last session we held a frame for. The canvas keeps its last picture across
	// reconnects and transient status changes; only a genuine session switch
	// invalidates it (showing another session's page would be wrong).
	let framedSessionId = $state<string | null>(null);

	$effect(() => {
		const isOpen = open;
		const sid = sessionId;
		return untrack(() => {
		if (framedSessionId !== sid) {
			latestFrame = null;
			hoverRect = null;
			frameIdentity = null;
			workspace = null;
			keyboardOpen = false;
			designChanges = [];
			designDraft = null;
			designRects = {};
			designOriginal = false;
			designSent = null;
			addressDraft = null;
			if (framedSessionId) browserPanel.resetPageTracking(framedSessionId);
		}
		if (!isOpen || !sid) return;
		let active = true;
		const scope = new PanelOperation();
		operation = scope;
		submitting = false;
		navigationPending = false;
		trackedPage = false;
		replies.clear();
		awaitingReplies.clear();

		if (framedSessionId !== sid) {
			latestFrame = null;
			framedSessionId = sid;
			hoverRect = null;
			browserPanel.resetPageTracking(sid);
		}
		workspace = null;
		viewerId = null;
		commandError = '';
		browserPanel.setStatus('connecting', null, sid);
		const c = createBrowserStreamClient({
			sessionId: sid,
			getAuthToken,
			onStatus: (s, u) => { if (active) browserPanel.setStatus(s, u, sid); },
			onPage: (u, loading) => { if (active) { trackedPage = true; browserPanel.setPage(u, loading, sid); } },
			onFrame: (data, metadata) => {
				if (!active) return;
				if (!workspace?.available || !workspace.active_tab_id) return;
				latestFrame = { data, metadata };
			},
			onInspectHit: (mode, raw) => { if (active && frameMatchesWorkspace) handleInspectHit(mode, raw); },
			onDesignResult: (op, result) => { if (active) handleDesignResult(op, result); },
			onError: (message) => {
				if (!active) return;
				commandError = message;
			},
			onHello: (id) => {
				if (!active) return;
				viewerId = id;
				scope.notify();
			},
			onCommandResult: (_id, ok, error, text, choices) => {
				if (!active) return;
				if (awaitingReplies.has(_id)) replies.set(_id, { ok, error, text, choices });
				scope.notify();
				// A failed copy or list lookup is reported where it was asked for, in plain words.
				if (!ok && !quietRequests.has(_id))
					commandError =
						error || 'Command failed. Try again after checking control.';
			},
			onWorkspace: (state) => {
				if (!active) return;
				workspace = state;
				if (state) {
					const identity = `${state.runtime_id}:${state.active_tab_id}`;
					if (frameIdentity !== identity) {
						// Keep the last image until this target paints; input stays gated.
						hoverRect = null;
						browserPanel.resetPageTracking(sid);
						frameIdentity = identity;
						trackedPage = false;
					}
					// Workspace busy includes input/viewport/heartbeat work, not page loading.
					if (!trackedPage) browserPanel.setPage(
						state.tabs.find((t) => t.id === state.active_tab_id)?.url ?? null,
						false,
						sid,
					);
				}
				scope.notify();
			},
			onOpen: () => {
				if (!active) return;
				streamOpen = true;
				scope.notify();
			},
			onClose: (wasClean) => {
				if (!active) return;
				streamOpen = false;
				if (!wasClean) browserPanel.setStatus('disconnected', undefined, sid);
			},
		});
		client = c;
		c.connect();

		return () => {
			active = false;
			clearTimeout(releaseTimer);
			controlRequest = null;
			controlGeneration++;
			releasingControl = false;
			scope.close();
			if (operation === scope) operation = null;
			designSending = false;
			if (previewTimer !== null) clearTimeout(previewTimer);
			previewTimer = null;
			submitting = false;
			navigationPending = false;
			c.destroy();
			streamOpen = false;
			if (client === c) client = null;
		};
		});
	});

	// Cross-page navigation ends the list: the marked elements belong to the
	// page they were picked on. A reload of the same address keeps it, and the
	// previews are put back once the page has loaded again.
	let designUrl: string | null = null;
	let designWasLoading = false;
	$effect(() => {
		const u = url;
		const busy = pageLoading;
		untrack(() => {
			if (designRows.length === 0) {
				designUrl = u;
			} else if (designUrl === null) {
				designUrl = u;
			} else if (u !== designUrl) {
				designUrl = u;
				hoverRect = null;
				clearDesign(false);
			} else if (designWasLoading && !busy) {
				for (const change of designRows) if (Object.keys(change.tweaks).length > 0) previewChange(change);
			}
			designWasLoading = busy;
		});
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
	let controlRequest: Promise<void> | null = null;
	let controlGeneration = 0;
	let releasingControl = false;
	let releaseTimer: ReturnType<typeof setTimeout> | undefined;
	async function releaseControl() {
		const scope = operation;
		if (!scope || !viewerId || releasingControl) return;
		const runtimeId = workspace?.runtime_id;
		checkOwner();
		if (!humanControl && workspace?.control.pending_viewer_id !== viewerId) return;
		releasingControl = true;
		controlGeneration++;
		clearTimeout(releaseTimer);
		keyboardOpen = false;
		try {
			await acknowledged(scope, 'release', {});
			await scope.wait(() => !!workspace && workspace.runtime_id === runtimeId &&
				workspace.control.viewer_id !== viewerId && workspace.control.pending_viewer_id !== viewerId,
				'Control has not been returned yet. Try again.');
		} finally {
			if (operation === scope) releasingControl = false;
		}
	}
	function noteInteraction() {
		clearTimeout(releaseTimer);
		releaseTimer = setTimeout(() => {
			if (humanControl && !keyboardOpen && !navigationPending) sendIdentified({ type: 'workspace-command', op: 'release', args: {} });
			else if (humanControl) noteInteraction();
		}, 15000);
	}
	async function ensureControl() {
		const scope = operation;
		const generation = controlGeneration;
		if (releasingControl) throw new Error('Control is being returned. Wait before interacting again.');
		if (!scope || !streamOpen) throw new Error('Browser is reconnecting. Try again.');
		checkOwner();
		if (!humanControl) {
			if (!controlRequest) {
				const request = (async () => {
					await acknowledged(scope, 'takeover', {});
					await scope.wait(() => { checkOwner(); return humanControl; }, 'Browser is busy. Try again.');
				})().finally(() => { if (controlRequest === request) controlRequest = null; });
				controlRequest = request;
			}
			await controlRequest;
		}
		if (operation !== scope) throw new Error('Browser panel changed. Try again.');
		if (generation !== controlGeneration) throw new Error('Browser control was returned. Interact again to take control.');
		noteInteraction();
	}
	function pickFiles(field: { id: string }) {
		// Capture before opening the OS picker, not after it returns. A reconnect,
		// navigation or tab switch must never retarget the selected local files.
		const scope = operation, identity = frameIdentity, sid = sessionId;
		const runtime = workspace?.runtime_id, tab = workspace?.active_tab_id, viewer = viewerId;
		commandError = '';
		void ensureControl().catch(error => {
			if (operation === scope) commandError = error instanceof Error ? error.message : 'Browser is unavailable';
		});
		return async (files: File[]) => {
			try {
				if (!scope || !sid || !runtime || !tab || !viewer || operation !== scope || frameIdentity !== identity)
					throw new Error('Browser changed while choosing files. Choose them again.');
				await ensureControl();
				if (operation !== scope || frameIdentity !== identity || viewerId !== viewer)
					throw new Error('Browser changed while choosing files. Choose them again.');
				await uploadPanelFiles(sid, { runtime_id: runtime, tab_id: tab, viewer_id: viewer, field: field.id }, files, scope.controller.signal);
			} catch (error) {
				if (operation === scope) commandError = error instanceof Error ? error.message : 'File upload failed';
			} finally { if (operation === scope) noteInteraction(); }
		};
	}

	async function workspaceCommand(op: string, args: Record<string, unknown>) {
		if (op === 'tabs' && args.action === 'create') { void browse(); return; }
		const scope = operation;
		const identity = frameIdentity;
		commandError = '';
		try {
			if (op === 'release') { await releaseControl(); return; }
			await ensureControl();
			if (operation !== scope || frameIdentity !== identity) return;
			if (!sendIdentified({ type: 'workspace-command', op, args })) throw new Error('Command not sent. Check the connection and try again.');
		} catch (error) { if (operation === scope) commandError = error instanceof Error ? error.message : 'Browser command failed.'; }
	}

	async function sendInput(payload: Record<string, unknown>) {
		if (!frameMatchesWorkspace || (payload.kind === 'mouse' && payload.event === 'move' && !humanControl)) return;
		// Capture identity before awaiting admission: never replay a gesture on a new tab.
		const identity = frameIdentity;
		const scope = operation;
		try {
			await ensureControl();
			if (identity === frameIdentity && frameMatchesWorkspace) sendIdentified(payload);
		} catch (error) { if (operation === scope) commandError = error instanceof Error ? error.message : 'Browser input failed.'; }
	}

	function sendInspect(x: number, y: number, mode: 'hover' | 'select') {
		if (streamOpen && frameMatchesWorkspace)
			sendIdentified({ type: 'inspect', x, y, mode });
	}

	// ── Viewport follows the live-view box ──────────────────────────────
	// Headless Chrome does not resize with the docked panel. Without this
	// the streamed page stays at its launch (or last agent-resize) size,
	// so its own narrow-window layout never lifts no matter how wide
	// the user drags the panel.
	let panelBodyEl: HTMLElement | undefined = $state();
	let lastViewportW = 0;
	let lastViewportH = 0;
	let viewportTimer: ReturnType<typeof setTimeout> | null = null;

	function sendViewportNow(width: number, height: number) {
		if (!humanControl || keyboardOpen || workspace?.viewport.mode !== 'fit') return;
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
	// An IME resizes the local WebView. Resizing the remote page too would
	// trip the frame gate and unmount the native field that opened the IME.
	// Freeze remote geometry while editing; resync when the field blurs.
	const fitViewportEnabled = $derived(humanControl && !keyboardOpen && workspace?.viewport.mode === 'fit');
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

	function enterDesign() {
		hoverRect = null;
		designSent = null;
		browserPanel.setInspectMode(true, sessionId);
		// The page and the list sit side by side, so give the panel the room.
		if (embedded && !platform.isMobile && !panel.expanded) {
			expandedForDesign = true;
			browserPanel.setExpanded(sessionId, true);
		}
	}

	function exitInspect() {
		hoverRect = null;
		settleDraft();
		if (designOriginal) compareDesign(false);
		browserPanel.setInspectMode(false, sessionId);
		if (expandedForDesign) browserPanel.setExpanded(sessionId, false);
		expandedForDesign = false;
	}

	function toggleInspect() {
		if (inspectMode) exitInspect();
		else enterDesign();
	}

	function close() {
		browserPanel.closePanel(sessionId);
	}

	/** Go and watch the reply: the chat sits beside the panel, or behind it on a phone. */
	function viewChat() {
		if (platform.isMobile || !embedded) browserPanel.closePanel(sessionId);
		else exitInspect();
	}

	// Beside the page when the panel is wide enough for both, under it otherwise.
	let stageWidth = $state(0);
	const designLayout = $derived(stageWidth >= 760 ? 'side' : 'sheet');

	// ── Fullscreen is the mobile shell, not a width ────────────────────────
	const isFullscreen = $derived(platform.isMobile);

	// ── Left-edge drag resize (docked mode only) ───────────────────────────
	let panelEl: HTMLElement | undefined = $state();
	let isResizing = $state(false);
	function startResize(e: PointerEvent) {
		if (isFullscreen || panel.expanded) return; // fullscreen overlay isn't resizable
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

	async function commitAddress() {
		if (addressDraft === null || submitting) return;
		const raw = addressDraft.trim();
		if (!raw) return;
		const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw) ? raw : `https://${raw}`;
		if (!/^https?:\/\//i.test(withScheme)) {
			commandError = 'Use an http or https address.';
			addressEl?.focus();
			return;
		}
		await browse(withScheme);
	}

	async function acknowledged(scope: PanelOperation, op: string, args: Record<string, unknown>, track?: (id: string) => void) {
		if (operation !== scope || scope.controller.signal.aborted || !workspace?.available || !client?.isConnected())
			throw new Error('Browser disconnected. Try the address again.');
		const id = `panel-${Date.now()}-${++commandSequence}`;
		awaitingReplies.add(id);
		track?.(id);
		try {
			if (!client.send({ type: 'workspace-command', id, op, args,
				runtime_id: workspace.runtime_id, tab_id: workspace.active_tab_id }))
				throw new Error('Command not sent. Check the connection and try again.');
			await scope.wait(() => replies.has(id), 'Browser did not respond. Try again.', 30_000);
			const reply = replies.get(id)!;
			if (!reply.ok) throw new Error(reply.error || 'Command failed. Try again.');
			return reply;
		} finally { replies.delete(id); awaitingReplies.delete(id); quietRequests.delete(id); }
	}

	/** Requests whose failure is shown by their own caller, not the panel's error line. */
	const quietRequests = new Set<string>();

	// A select's options for the panel's own list. Like a copy, they travel
	// only to this viewer and never enter workspace state.
	async function loadChoices(field: BrowserEditor): Promise<BrowserChoices> {
		const scope = operation;
		if (!scope) throw new Error('Browser is not connected.');
		await ensureControl();
		const reply = await acknowledged(scope, 'options', { field: field.id }, (id) => quietRequests.add(id));
		if (!reply.choices) throw new Error("Couldn't read this list. Try again.");
		return reply.choices;
	}

	// A picker's choice, set in the page. Like the list it came from, its
	// refusal ("This list changed…") is shown in the picker, not the error line.
	async function setChoice(field: BrowserEditor, choice: PickerChoice): Promise<void> {
		const scope = operation;
		if (!scope) throw new Error('Browser is not connected.');
		await ensureControl();
		await acknowledged(scope, 'edit', { field: field.id, action: 'set', ...choice }, (id) => quietRequests.add(id));
	}

	// Copy from the remote page to this device's clipboard. The text travels
	// only to this viewer; it never enters workspace state or the agent's view.
	let copyNotice = $state('');
	let copyNoticeTimer: ReturnType<typeof setTimeout> | undefined;
	async function copyFromPage(): Promise<string> {
		const scope = operation;
		if (!scope) throw new Error('Browser is not connected.');
		await ensureControl();
		const reply = await acknowledged(scope, 'copy', {}, (id) => quietRequests.add(id));
		return reply.text ?? '';
	}
	function copyToClipboard() {
		// Start the clipboard write inside the user's gesture: Safari and Chrome
		// refuse a write that begins after the round trip to the remote page.
		const text = copyFromPage();
		const done = () => { commandError = ''; showCopyNotice('Copied'); };
		const failed = (error: unknown) => {
			commandError = error instanceof Error && error.message ? error.message : "Couldn't copy. Select the text again and retry.";
		};
		const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
		if (clip?.write && typeof ClipboardItem !== 'undefined') {
			const blob = text.then((value) => new Blob([value], { type: 'text/plain' }));
			clip.write([new ClipboardItem({ 'text/plain': blob })]).then(done, async (error) => {
				// Some browsers reject promised clipboard items; retry the plain way.
				try { await clip.writeText(await text); done(); } catch { failed(await text.then(() => error, (e) => e)); }
			});
		} else if (clip?.writeText) text.then((value) => clip.writeText(value)).then(done, failed);
		else text.then(() => failed(new Error("This browser doesn't allow copying here.")), failed);
	}
	function showCopyNotice(message: string) {
		copyNotice = message;
		clearTimeout(copyNoticeTimer);
		copyNoticeTimer = setTimeout(() => (copyNotice = ''), 1500);
	}

	function checkOwner() {
		if (workspace && ((workspace.control.owner === 'human' && workspace.control.viewer_id !== viewerId) ||
			(workspace.control.pending_viewer_id && workspace.control.pending_viewer_id !== viewerId)))
			throw new Error('Another viewer controls this browser. Ask them to return control, then try again.');
	}

	async function browse(address?: string) {
		const scope = operation;
		if (submitting || navigationPending || releasingControl || !scope) return;
		const generation = controlGeneration;
		const sid = sessionId;
		const draft = addressDraft;
		const assertCurrent = () => {
			if (operation !== scope || scope.controller.signal.aborted || sessionId !== sid || !open)
				throw new Error('Browser panel closed.');
			if (generation !== controlGeneration) throw new Error('Browser control was returned. Try the address again.');
		};
		submitting = true;
		commandError = '';
		try {
			checkOwner();
			const cold = !workspace?.available || !workspace.active_tab_id;
			let runtimeId = workspace?.runtime_id;
			if (cold) {
				const request = new AbortController();
				const abort = () => request.abort();
				scope.controller.signal.addEventListener('abort', abort, { once: true });
				const timer = setTimeout(abort, 30_000);
				try { runtimeId = (await startPanelBrowser(sid, request.signal)).runtime_id; }
				finally {
					clearTimeout(timer);
					scope.controller.signal.removeEventListener('abort', abort);
				}
			}
			// HTTP is not a viewer connection acknowledgement. Wait for the WS state.
			assertCurrent();
			await scope.wait(() => streamOpen && !!viewerId && !!workspace?.available &&
				workspace.runtime_id === runtimeId && !!workspace.active_tab_id,
				'Browser is not ready. Try again.');
			assertCurrent();
			checkOwner();
			if (!ownsControl(workspace, viewerId)) {
				await acknowledged(scope, 'takeover', {});
				await scope.wait(() => { checkOwner(); return ownsControl(workspace, viewerId); },
					'Control was not granted. Try again when the browser is free.');
			}
			assertCurrent();
			noteInteraction();
			if (workspace?.runtime_id !== runtimeId || !ownsControl(workspace, viewerId))
				throw new Error('Browser control changed. Try again.');
			if (address) {
				navigationPending = true;
				await acknowledged(scope, 'navigate', { url: address });
				if (operation === scope && addressDraft === draft) addressDraft = null;
			} else if (!cold) await acknowledged(scope, 'tabs', { action: 'create' });
			// A cold New tab uses the bootstrap tab, never creates a second blank tab.
		} catch (error) {
			if (operation === scope && sessionId === sid && open) {
				commandError = error instanceof Error ? (error.name === 'AbortError'
					? 'Browser startup timed out. Try the address again.' : error.message) :
					(error as { detail?: string })?.detail || 'Browser could not start. Try again.';
				if (address) addressEl?.focus();
			}
		} finally {
			if (operation === scope) { submitting = false; navigationPending = false; }
		}
	}

	function cancelEditing() {
		addressDraft = null;
	}

	async function sendCommand(command: string) {
		if (!client?.isConnected() || !canNavigate) return;
		if (command === 'stop') {
			// Interrupts navigation even while its acknowledgement is outstanding.
			workspaceCommand('command', { command });
			return;
		}
		const scope = operation;
		if (!scope || navigationPending || submitting) return;
		navigationPending = true;
		commandError = '';
		try { await ensureControl(); await acknowledged(scope, 'command', { command }); }
		catch (error) {
			if (operation === scope) commandError = error instanceof Error ? error.message : 'Navigation failed. Try again.';
		} finally { if (operation === scope) navigationPending = false; }
	}

	// Toolbar is usable whenever the panel socket is up and Chromium is on.
	// Gating on `live` left back/forward/reload/address disabled for a visible
	// page whenever status lagged on connecting/no_page (isConnected() also
	// wasn't reactive, so a connecting→connecting handshake never re-enabled).
	const canInteract = $derived(streamOpen && !!workspace?.available && (!workspace.control.viewer_id || workspace.control.viewer_id === viewerId) && (!workspace.control.pending_viewer_id || workspace.control.pending_viewer_id === viewerId));
	const canNavigate = $derived(canInteract && !!workspace?.active_tab_id);

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
	const keyboardEnabled = $derived(isLive && canInteract && !!latestFrame && latestFrame.metadata.runtime_id === workspace?.runtime_id && latestFrame.metadata.tab_id === workspace?.active_tab_id);
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

</script>

{#if open}
	<aside
		bind:this={panelEl}
		class="browser-panel"
		class:embedded
		class:fullscreen={isFullscreen && !embedded}
		class:resizing={isResizing}
		class:expanded={panel.expanded && !embedded}
		style={embedded || isFullscreen || panel.expanded ? '' : `width: ${width}px`}
		aria-label="Live browser view"
	>
		{#if !embedded && !isFullscreen && !panel.expanded}
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

		<div class="tab-row">
			<BrowserWorkspaceToolbar mode="tabs" starting={submitting} {workspace} {viewerId} connected={streamOpen} onCommand={workspaceCommand} />
			{#if !embedded}<div class="window-actions">
				<button class="tool-btn" aria-label={panel.expanded ? 'Restore browser panel' : 'Expand browser panel'} onclick={() => browserPanel.setExpanded(sessionId, !panel.expanded)}>
					{#if panel.expanded}<Minimize2 size={15} />{:else}<Maximize2 size={15} />{/if}
				</button>
				<button class="tool-btn" onclick={close} aria-label="Close browser panel"><X size={16} /></button>
			</div>{/if}
			{@render panelActions?.()}
		</div>
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
					class:loading={loading}
					onclick={() => sendCommand(loading ? 'stop' : 'reload')}
					disabled={!canNavigate}
					aria-label={loading ? 'Stop loading' : 'Reload page'}
					title={loading ? 'Stop' : 'Reload'}
				>
					{#if loading}
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
							if (e.key === 'Escape') { e.preventDefault(); cancelEditing(); }
						}}
					/>
				{:else}
					<button
						type="button"
						class="address-display"
						onclick={startEditing}
						title={canNavigate ? `${displayUrl} — click to edit` : displayUrl}
					>
						<span class="status-dot status-{status}" aria-hidden="true"></span>
						<span class="address-text">{compactUrl}</span>
					</button>
				{/if}
			</form>

			<BrowserWorkspaceToolbar mode="controls" {workspace} {viewerId} connected={streamOpen} onCommand={workspaceCommand} />
			<div class="header-actions">
				<button
					class="tool-btn"
					class:active={!!copyNotice}
					onclick={copyToClipboard}
					disabled={!isLive || !frameMatchesWorkspace}
					aria-label={copyNotice || 'Copy selected text to this device'}
					title="Copy the text selected on the page to this device (Ctrl+C or ⌘C also works)"
				>
					{#if copyNotice}<Check size={15} strokeWidth={1.75} />{:else}<Copy size={15} strokeWidth={1.75} />{/if}
				</button>
				<button
					class="tool-btn design-toggle"
					class:active={inspectMode}
					onclick={toggleInspect}
					disabled={!inspectMode && (!isLive || !frameMatchesWorkspace)}
					aria-label={inspectMode ? 'Leave design mode' : 'Design: mark changes on the page for the AI'}
					aria-pressed={inspectMode}
					title="Design: click elements on the page, say what should change, then send the list to the AI"
				>
					<PencilRuler size={15} strokeWidth={1.75} />
					<span class="design-toggle-label">Design</span>
					{#if !inspectMode && designChanges.length > 0}<span class="design-toggle-count">{designChanges.length}</span>{/if}
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

			</div>
		</header>

		<div class="stage" class:split={inspectMode && designLayout === 'side'} bind:clientWidth={stageWidth}>
		<div class="panel-body" bind:this={panelBodyEl}>
			<BrowserWorkspaceNotice notice={workspace?.notice} />
			{#if commandError}<div class="command-error" role="alert">{commandError}<button onclick={() => commandError = ''} aria-label="Dismiss browser error">×</button></div>{/if}
			{#if hasFrame && framedSessionId === sessionId}
				<!-- The canvas stays mounted across reconnects and target re-attaches:
				     unmounting it made every transient status blank the preview. -->
				<div class="live-holder">
					{#key sessionId}
						<BrowserLiveView
							bind:keyboardOpen
							{keyboardEnabled}
							frame={latestFrame}
							interactive={isLive && frameMatchesWorkspace && (canInteract || inspectMode)}
							editors={workspace?.editors ?? []}
							onEdit={(args) => workspaceCommand('edit', args)}
							onPickFiles={pickFiles}
							onChoices={loadChoices}
							onChoose={setChoice}
							revision={workspace?.revision ?? 0}
							viewport={workspace?.viewport}
							inspecting={inspectMode && isLive && frameMatchesWorkspace}
							{hoverRect}
							pins={designPins}
							onInput={sendInput}
							onInspect={sendInspect}
							onHoverLeave={() => (hoverRect = null)}
							onExitInspect={exitInspect}
							onCopy={copyToClipboard}
						/>
					{/key}
					{#if !frameMatchesWorkspace}<div class="stream-badge" role="status">Previous page · waiting for the current frame · view only</div>{:else if showStreamBadge}
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
						<p class="empty-copy">Connecting to this session’s browser…</p>
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
							Enter an address above and press Enter to start browsing.
						</p>
					{:else if status === 'browser_off'}
						<MonitorOff size={26} strokeWidth={1.5} />
						<p class="empty-title">Browser isn't running</p>
						<p class="empty-copy">
							Enter an address above to start this session’s browser.
						</p>
					{:else}
						<PlugZap size={26} strokeWidth={1.5} />
						<p class="empty-title">Disconnected</p>
						<p class="empty-copy">Lost the live feed — trying to reconnect.</p>
					{/if}
				</div>
			{/if}
		</div>

		{#if inspectMode}
			<BrowserDesignRail
				layout={designLayout}
				rows={designRows}
				editingId={designDraft?.id ?? null}
				editingNew={draftIsNew}
				showingOriginal={designOriginal}
				hasPreviews={designHasPreviews}
				sending={designSending}
				sentCount={designSent}
				focusToken={designFocus}
				onNote={setDesignNote}
				onTweak={setDesignTweak}
				onScope={setDesignScope}
				onCommit={settleDraft}
				onCancel={cancelDesignDraft}
				onEdit={editDesignChange}
				onRemove={removeDesignChange}
				onCompare={compareDesign}
				onSend={sendDesignChanges}
				onDone={exitInspect}
				onViewChat={viewChat}
			/>
		{/if}
		</div>

		{#if !inspectMode}
			<footer class="panel-note">
				<MousePointer2 size={12} strokeWidth={1.5} />
				<span
					>{humanControl
						? 'Click or tap to browse. Control returns automatically when idle.'
						: canInteract ? 'Click or tap the page to interact.' : 'Browser is busy or reconnecting.'}</span
				>
			</footer>
		{/if}
	</aside>
{/if}

<style>
	.tab-row { display: flex; align-items: stretch; min-height: 34px; background: var(--muted); border-bottom: 1px solid var(--border); }
	.window-actions { display: flex; align-items: center; margin-left: auto; padding: 0 4px; flex: none; }
	.browser-panel.expanded { inset: 0; width: 100%; border-left: 0; }
	.command-error { position: absolute; top: 8px; left: 8px; right: 8px; z-index: 3; padding: 6px; background: var(--background); border: 1px solid var(--border); font-size: 11px; }
	.command-error button { float: right; }

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

	.browser-panel.embedded { position: relative; inset: auto; width: 100%; height: 100%; border: 0; padding: 0; }

	.browser-panel.resizing {
		user-select: none;
	}
	.browser-panel.resizing :global(*) {
		pointer-events: none;
	}

	/* Fullscreen overlay on the mobile shell. */
	.browser-panel.fullscreen {
		position: absolute;
		inset: 0;
		z-index: 95;
		width: 100%;
		border-left: none;
		/* Keep the toolbar/footer clear of notches and home indicators. */
		padding-top: env(safe-area-inset-top, 0px);
		padding-bottom: env(safe-area-inset-bottom, 0px);
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
		/* Anchors the settings popover (BrowserWorkspaceToolbar) to this row. */
		position: relative;
		container-type: inline-size;
		display: flex;
		align-items: center;
		gap: 3px;
		padding: 3px 5px;
		min-height: 34px;
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
	.keyboard-toggle.active,
	.keyboard-toggle.active:hover:not(:disabled) {
		background: var(--row-selected);
		color: var(--foreground);
	}

	/* Design mode is the panel's one named tool, so it carries its name. */
	.design-toggle {
		width: auto;
		gap: 6px;
		padding: 0 9px 0 7px;
		font-size: 0.75rem;
		font-weight: 550;
	}
	.design-toggle.active,
	.design-toggle.active:hover:not(:disabled) {
		background: var(--primary);
		color: var(--primary-foreground);
	}
	.design-toggle-count {
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 5px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	@container (max-width: 420px) {
		.design-toggle-label {
			display: none;
		}
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
		border-radius: var(--radius);
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
		border-radius: var(--radius);
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

	/* ── Body ───────────────────────────────────────────────────────────── */
	/* The page, plus the design list beside or under it. */
	.stage {
		flex: 1;
		min-height: 0;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}
	.stage.split {
		flex-direction: row;
	}

	.panel-body {
		position: relative;
		flex: 1;
		min-width: 0;
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
		color: var(--foreground);
		background: color-mix(in oklch, var(--popover) 88%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		border-radius: var(--radius);
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
