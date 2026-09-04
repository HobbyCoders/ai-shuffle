<script lang="ts">
	/**
	 * BrowserLiveView — draws the screencast frames into a letterboxed,
	 * aspect-preserving canvas and forwards user input (mouse / wheel / keyboard)
	 * back to the agent's page in PAGE CSS pixels.
	 *
	 * Drawing: each incoming base64 JPEG is decoded to an ImageBitmap and painted
	 * on requestAnimationFrame (intermediate frames are coalesced). The paint uses
	 * the SAME fit-scale + letterbox math as the input mapping (coords.ts), so a
	 * click always lands where the cursor visually is. Inspect-mode element
	 * highlights (hover + selection rects, in page CSS pixels) are drawn straight
	 * onto the canvas after the frame, so they stay pixel-aligned with the page
	 * image through resizes and letterbox changes.
	 *
	 * Input: captured on a focusable wrapper. Pointer moves are rAF-coalesced
	 * (~60 Hz), wheel and key events preventDefault while focused so page hotkeys
	 * and the app's own shortcuts don't fire underneath the live view.
	 *
	 * Touch: a touch drag is translated into wheel (scroll) events — the page
	 * content follows the finger — and a tap (< slop movement) into a full
	 * move/down/up click sequence, so handheld users can actually browse.
	 *
	 * Inspect mode (`inspecting`): pointer events are NOT forwarded to the page.
	 * Hovering sends throttled `onInspect(x, y, 'hover')` hit-tests and a click
	 * (or tap) sends `onInspect(x, y, 'select')`; wheel still scrolls so deeper
	 * content stays reachable.
	 */
	import { onDestroy } from 'svelte';
	import {
		canvasToPage,
		fitScale,
		letterbox,
		modifiersFromEvent,
		type FrameMetadata,
		type PageRect,
	} from '$lib/browser-panel/coords';

	interface FramePayload {
		data: string;
		metadata: FrameMetadata;
	}

	interface Props {
		/** Latest decoded-and-ready frame, or null before the first frame. */
		frame: FramePayload | null;
		/** Whether input is accepted (true only while the stream is live). */
		interactive?: boolean;
		/** Point-and-prompt mode: hit-test instead of forwarding pointer input. */
		inspecting?: boolean;
		/** Hover-highlight rect in page CSS pixels (inspect mode). */
		hoverRect?: PageRect | null;
		/** Selection-highlight rect in page CSS pixels (inspect mode). */
		selectionRect?: PageRect | null;
		/** Send a client→server input payload over the socket. */
		onInput: (payload: Record<string, unknown>) => void;
		/** Request an element hit-test at a page CSS-pixel point (inspect mode). */
		onInspect?: (x: number, y: number, mode: 'hover' | 'select') => void;
		/** The pointer left the view — clear any hover highlight (inspect mode). */
		onHoverLeave?: () => void;
		/** The user asked to leave inspect mode from inside the view (Escape). */
		onExitInspect?: () => void;
	}

	let {
		frame,
		interactive = true,
		inspecting = false,
		hoverRect = null,
		selectionRect = null,
		onInput,
		onInspect,
		onHoverLeave,
		onExitInspect,
	}: Props = $props();

	let wrapperEl: HTMLDivElement | undefined = $state();
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let focused = $state(false);

	let currentBitmap: ImageBitmap | null = null;
	let currentMeta: FrameMetadata | null = null;

	// ── Frame decode + paint (rAF-coalesced) ──────────────────────────────
	let pendingFrame: FramePayload | null = null;
	let drawScheduled = false;

	function base64ToBlob(b64: string, type = 'image/jpeg'): Blob {
		const bin = atob(b64);
		const bytes = new Uint8Array(bin.length);
		for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
		return new Blob([bytes], { type });
	}

	async function runDraw() {
		drawScheduled = false;
		const f = pendingFrame;
		pendingFrame = null;
		if (!f || !canvasEl) return;
		try {
			const bmp = await createImageBitmap(base64ToBlob(f.data));
			if (!metaIsDrawable(f.metadata)) {
				// Unusable geometry: drop the new bitmap and keep the last good
				// picture on screen rather than swapping in something unpaintable.
				bmp.close?.();
				return;
			}
			currentBitmap?.close?.();
			currentBitmap = bmp;
			currentMeta = f.metadata;
			paint();
		} catch {
			// A malformed frame just skips this paint; the next frame recovers.
		}
	}

	function scheduleDraw(f: FramePayload) {
		pendingFrame = f;
		if (drawScheduled) return;
		drawScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined') requestAnimationFrame(runDraw);
		else runDraw();
	}

	/** Frame metadata is only usable if both device dimensions are finite and positive. */
	function metaIsDrawable(meta: FrameMetadata | null): meta is FrameMetadata {
		return (
			!!meta &&
			Number.isFinite(meta.deviceWidth) &&
			Number.isFinite(meta.deviceHeight) &&
			meta.deviceWidth > 0 &&
			meta.deviceHeight > 0
		);
	}

	/** Resolve a CSS custom property for canvas drawing, with a fixed fallback. */
	function cssColor(name: string, fallback: string): string {
		if (!canvasEl) return fallback;
		const v = getComputedStyle(canvasEl).getPropertyValue(name).trim();
		return v || fallback;
	}

	function drawHighlight(
		ctx: CanvasRenderingContext2D,
		r: PageRect,
		scale: number,
		off: { x: number; y: number },
		kind: 'hover' | 'selection',
	) {
		const x = off.x + r.x * scale;
		const y = off.y + r.y * scale;
		const w = r.width * scale;
		const h = r.height * scale;
		if (!(Number.isFinite(x) && Number.isFinite(y) && w >= 0 && h >= 0)) return;
		const color = cssColor('--primary', '#6366f1');
		ctx.save();
		if (kind === 'hover') {
			ctx.globalAlpha = 0.14;
			ctx.fillStyle = color;
			ctx.fillRect(x, y, w, h);
			ctx.globalAlpha = 0.9;
			ctx.lineWidth = 1.5;
			ctx.strokeStyle = color;
			ctx.strokeRect(x, y, w, h);
		} else {
			ctx.globalAlpha = 0.08;
			ctx.fillStyle = color;
			ctx.fillRect(x, y, w, h);
			ctx.globalAlpha = 1;
			ctx.lineWidth = 2;
			ctx.strokeStyle = color;
			ctx.strokeRect(x, y, w, h);
		}
		ctx.restore();
	}

	function paint() {
		const bitmap = currentBitmap;
		const meta = currentMeta;
		if (!canvasEl || !bitmap || !metaIsDrawable(meta)) return;
		const rect = canvasEl.getBoundingClientRect();
		if (rect.width <= 0 || rect.height <= 0) return;

		// Validate EVERYTHING before touching the canvas. Clearing first and then
		// bailing (or throwing inside drawImage on NaN geometry) blanked the panel
		// for a whole frame — the visible flicker.
		const box = { left: 0, top: 0, width: rect.width, height: rect.height };
		const scale = fitScale(box, meta);
		if (!(scale > 0)) return; // also rejects NaN
		const off = letterbox(box, meta, scale);
		if (!Number.isFinite(off.x) || !Number.isFinite(off.y)) return;

		const ctx = canvasEl.getContext('2d');
		if (!ctx) return;
		const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
		const bw = Math.round(rect.width * dpr);
		const bh = Math.round(rect.height * dpr);
		if (canvasEl.width !== bw) canvasEl.width = bw;
		if (canvasEl.height !== bh) canvasEl.height = bh;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, rect.width, rect.height);
		ctx.imageSmoothingQuality = 'high';
		ctx.drawImage(bitmap, off.x, off.y, meta.deviceWidth * scale, meta.deviceHeight * scale);

		if (inspecting) {
			if (selectionRect) drawHighlight(ctx, selectionRect, scale, off, 'selection');
			if (hoverRect) drawHighlight(ctx, hoverRect, scale, off, 'hover');
		} else if (selectionRect) {
			// Selection stays visible outside inspect mode so the composer's
			// context is still anchored to something on screen.
			drawHighlight(ctx, selectionRect, scale, off, 'selection');
		}
	}

	// Repaint on container resize so the letterbox re-fits. Observe the WRAPPER,
	// never the canvas: paint() resizes the canvas backing store, so observing the
	// canvas can feed its own resize notifications back into an endless loop.
	let resizeObserver: ResizeObserver | null = null;
	$effect(() => {
		if (!wrapperEl || typeof ResizeObserver === 'undefined') return;
		resizeObserver = new ResizeObserver(() => paint());
		resizeObserver.observe(wrapperEl);
		return () => {
			resizeObserver?.disconnect();
			resizeObserver = null;
		};
	});

	// New frame prop → schedule a paint.
	$effect(() => {
		const f = frame;
		if (f) scheduleDraw(f);
	});

	// Highlight rect / mode changes repaint the last frame (no new bitmap).
	$effect(() => {
		void hoverRect;
		void selectionRect;
		void inspecting;
		paint();
	});

	onDestroy(() => {
		currentBitmap?.close?.();
		currentBitmap = null;
	});

	// A native editable is required to summon iOS/Android keyboards. Its value is
	// only a transient IME buffer; committed text goes directly to the remote page.
	let keyboardEl: HTMLTextAreaElement | undefined = $state();
	let keyboardOpen = $state(false);
	let composing = false;
	let compositionCommit: string | null = null;
	let compositionTimer: ReturnType<typeof setTimeout> | undefined;

	function clearBridge() {
		if (keyboardEl) keyboardEl.value = '';
		composing = false;
		compositionCommit = null;
		clearTimeout(compositionTimer);
	}

	function openKeyboard() {
		if (!interactive || inspecting) return;
		// Must stay in the original user gesture, not a promise/effect/rAF.
		keyboardEl?.focus({ preventScroll: true });
	}

	function dismissKeyboard() {
		keyboardEl?.blur();
		keyboardOpen = false;
		clearBridge();
	}

	$effect(() => {
		if (!interactive || inspecting) {
			touch = null;
			dismissKeyboard();
		}
	});
	onDestroy(() => clearTimeout(compositionTimer));

	function commitText(text: string) {
		if (!interactive || inspecting || !keyboardOpen) return;
		// Bound each socket message without separating a UTF-16 surrogate pair.
		const maxTextUnits = 16384;
		for (let start = 0; start < text.length;) {
			let end = Math.min(start + maxTextUnits, text.length);
			const last = text.charCodeAt(end - 1);
			const next = text.charCodeAt(end);
			if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) end--;
			onInput({ type: 'input', kind: 'text', text: text.slice(start, end) });
			start = end;
		}
	}

	function bridgeCompositionEnd(e: CompositionEvent) {
		if (!composing) return;
		composing = false;
		commitText(e.data);
		if (keyboardEl) keyboardEl.value = '';
		// Engines differ on whether a final input follows compositionend. Ignore
		// that trailing commit, but never swallow the next deliberate keystroke.
		compositionCommit = e.data;
		clearTimeout(compositionTimer);
		compositionTimer = setTimeout(() => { compositionCommit = null; }, 0);
	}

	function bridgeInput(event: Event) {
		const e = event as InputEvent;
		e.stopPropagation();
		if (composing || e.isComposing) return;
		const text = keyboardEl?.value || e.data || '';
		if (text !== compositionCommit) commitText(text);
		compositionCommit = null;
		if (keyboardEl) keyboardEl.value = '';
	}

	function bridgeKeyPair(key: string, code: string, keyCode: number) {
		if (!interactive || inspecting || !keyboardOpen) return;
		for (const event of ['down', 'up']) {
			onInput({ type: 'input', kind: 'key', event, key, code, keyCode, text: '', modifiers: 0 });
		}
	}

	function bridgeBeforeInput(e: InputEvent) {
		e.stopPropagation();
		if (!interactive || inspecting || !keyboardOpen) {
			e.preventDefault();
			clearBridge();
			return;
		}
		if (composing || e.isComposing) return;
		// An empty native buffer still emits beforeinput for virtual Backspace.
		if (e.inputType.startsWith('delete')) {
			e.preventDefault();
			const forward = e.inputType.endsWith('Forward');
			bridgeKeyPair(forward ? 'Delete' : 'Backspace', forward ? 'Delete' : 'Backspace', forward ? 46 : 8);
		} else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') {
			e.preventDefault();
			bridgeKeyPair('Enter', 'Enter', 13);
		}
	}

	const bridgeKeys = new Set(['Tab', 'Enter', 'Backspace', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown']);
	function bridgeKey(e: KeyboardEvent, event: 'down' | 'up') {
		e.stopPropagation();
		if (!interactive || inspecting) { e.preventDefault(); return; }
		if (composing || e.isComposing || e.keyCode === 229) return;
		compositionCommit = null;
		if (e.key === 'Escape') {
			e.preventDefault();
			if (event === 'down') dismissKeyboard();
		} else if (bridgeKeys.has(e.key)) {
			// Prevent the native edit (and its beforeinput) when a real key event
			// already handles it. Printable keys are handled ONLY by input/IME.
			e.preventDefault();
			sendKey(event, e);
		}
	}

	// ── Input forwarding ───────────────────────────────────────────────────

	function buttonName(btn: number): 'left' | 'middle' | 'right' {
		return btn === 1 ? 'middle' : btn === 2 ? 'right' : 'left';
	}

	function pagePoint(e: { clientX: number; clientY: number }) {
		if (!canvasEl || !currentMeta) return null;
		return canvasToPage(e.clientX, e.clientY, canvasEl.getBoundingClientRect(), currentMeta);
	}

	function sendMouse(
		event: 'move' | 'down' | 'up' | 'wheel',
		e: PointerEvent | WheelEvent,
		extra: Record<string, unknown> = {},
	) {
		if (!interactive) return;
		const p = pagePoint(e);
		if (!p) return;
		onInput({
			type: 'input',
			kind: 'mouse',
			event,
			x: p.x,
			y: p.y,
			button: buttonName((e as PointerEvent).button ?? 0),
			clickCount: (e as PointerEvent).detail || 1,
			deltaX: 0,
			deltaY: 0,
			modifiers: modifiersFromEvent(e),
			...extra,
		});
	}

	// ── Inspect hit-testing (hover throttled to ~12 Hz, trailing kept) ────
	const HOVER_THROTTLE_MS = 80;
	let lastHoverSent = 0;
	let trailingHover: { clientX: number; clientY: number } | null = null;
	let trailingHoverTimer: ReturnType<typeof setTimeout> | null = null;

	function sendInspect(e: { clientX: number; clientY: number }, mode: 'hover' | 'select') {
		if (!interactive || !onInspect) return;
		if (mode === 'hover') {
			const now = Date.now();
			if (now - lastHoverSent < HOVER_THROTTLE_MS) {
				// Keep the newest position; fire it when the window closes so the
				// highlight settles under a stopped cursor instead of lagging.
				trailingHover = { clientX: e.clientX, clientY: e.clientY };
				if (trailingHoverTimer === null) {
					trailingHoverTimer = setTimeout(() => {
						trailingHoverTimer = null;
						const t = trailingHover;
						trailingHover = null;
						if (t) sendInspect(t, 'hover');
					}, HOVER_THROTTLE_MS - (now - lastHoverSent));
				}
				return;
			}
			lastHoverSent = now;
		}
		const p = pagePoint(e);
		if (!p) return;
		onInspect(p.x, p.y, mode);
	}

	onDestroy(() => {
		if (trailingHoverTimer !== null) clearTimeout(trailingHoverTimer);
	});

	// ── Touch drag→scroll / tap→click synthesis ───────────────────────────
	/** Movement under this (px) still counts as a tap. */
	const TAP_SLOP_PX = 8;
	let touch: {
		id: number;
		lastX: number;
		lastY: number;
		startX: number;
		startY: number;
		moved: boolean;
	} | null = null;

	function onTouchDown(e: PointerEvent) {
		if (touch !== null) return; // ignore secondary fingers
		touch = {
			id: e.pointerId,
			lastX: e.clientX,
			lastY: e.clientY,
			startX: e.clientX,
			startY: e.clientY,
			moved: false,
		};
	}

	function onTouchMove(e: PointerEvent) {
		if (!touch || e.pointerId !== touch.id) return;
		const dx = e.clientX - touch.lastX;
		const dy = e.clientY - touch.lastY;
		touch.lastX = e.clientX;
		touch.lastY = e.clientY;
		if (
			Math.abs(e.clientX - touch.startX) > TAP_SLOP_PX ||
			Math.abs(e.clientY - touch.startY) > TAP_SLOP_PX
		) {
			touch.moved = true;
		}
		// Content follows the finger: dragging up scrolls down.
		if (touch.moved) sendMouse('wheel', e, { deltaX: -dx, deltaY: -dy });
	}

	function onTouchUp(e: PointerEvent) {
		if (!touch || e.pointerId !== touch.id) return;
		const wasTap = !touch.moved;
		touch = null;
		if (!wasTap) return;
		if (inspecting) {
			sendInspect(e, 'select');
			return;
		}
		// Full click sequence at the tap point.
		sendMouse('move', e);
		sendMouse('down', e, { clickCount: 1 });
		sendMouse('up', e, { clickCount: 1 });
		if (pagePoint(e)) openKeyboard();
	}

	// ── Pointer handlers ───────────────────────────────────────────────────

	// Pointer moves coalesced to one send per animation frame (~60 Hz).
	let pendingMove: PointerEvent | null = null;
	let moveScheduled = false;

	function flushMove() {
		moveScheduled = false;
		const m = pendingMove;
		pendingMove = null;
		if (m) sendMouse('move', m);
	}

	function onPointerMove(e: PointerEvent) {
		if (e.target !== wrapperEl && e.target !== canvasEl) return;
		if (!interactive) return;
		if (e.pointerType === 'touch') {
			onTouchMove(e);
			return;
		}
		if (inspecting) {
			sendInspect(e, 'hover');
			return;
		}
		pendingMove = e;
		if (moveScheduled) return;
		moveScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined') requestAnimationFrame(flushMove);
		else flushMove();
	}

	function onPointerDown(e: PointerEvent) {
		if (e.target !== wrapperEl && e.target !== canvasEl) return;
		if (e.pointerType !== 'touch') wrapperEl?.focus();
		if (!interactive) return;
		e.preventDefault();
		if (e.pointerType === 'touch') {
			onTouchDown(e);
			return;
		}
		if (inspecting) return; // select fires on pointerup
		sendMouse('down', e);
	}

	function onPointerUp(e: PointerEvent) {
		if (e.target !== wrapperEl && e.target !== canvasEl) return;
		if (!interactive) return;
		if (e.pointerType === 'touch') {
			onTouchUp(e);
			return;
		}
		if (inspecting) {
			sendInspect(e, 'select');
			return;
		}
		sendMouse('up', e);
	}

	function onPointerCancel(e: PointerEvent) {
		if (touch && e.pointerId === touch.id) touch = null;
	}

	function onPointerLeave() {
		if (!inspecting) return;
		// Drop a queued trailing hover so it can't re-highlight after leaving.
		trailingHover = null;
		onHoverLeave?.();
	}

	function onWheel(e: WheelEvent) {
		if (!interactive) return;
		e.preventDefault();
		// Inspect mode still scrolls — deeper content must stay reachable.
		sendMouse('wheel', e, { deltaX: e.deltaX, deltaY: e.deltaY });
	}

	function isPrintable(key: string): boolean {
		// A single-character key (letter, digit, symbol, space) is text input;
		// named keys ("Enter", "ArrowLeft", …) are longer than one char.
		return key.length === 1;
	}

	function sendKey(event: 'down' | 'up', e: KeyboardEvent) {
		onInput({
			type: 'input',
			kind: 'key',
			event,
			key: e.key,
			code: e.code,
			keyCode: e.keyCode,
			text: event === 'down' && isPrintable(e.key) ? e.key : '',
			modifiers: modifiersFromEvent(e),
		});
	}

	function onKeyDown(e: KeyboardEvent) {
		if (e.target !== wrapperEl || !interactive || !focused) return;
		// Keep the keystroke inside the live view — don't let it trigger app hotkeys.
		e.preventDefault();
		e.stopPropagation();
		if (inspecting) {
			// Keys are not forwarded while inspecting; Escape leaves the mode.
			if (e.key === 'Escape') onExitInspect?.();
			return;
		}
		sendKey('down', e);
	}

	function onKeyUp(e: KeyboardEvent) {
		if (e.target !== wrapperEl || !interactive || !focused) return;
		e.preventDefault();
		e.stopPropagation();
		if (inspecting) return;
		sendKey('up', e);
	}
</script>

<!-- The live view is a genuine interactive surface (role="application"): it must
     be focusable and receive raw pointer/keyboard events to forward to the remote
     page, so the noninteractive-element a11y heuristics don't apply here. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={wrapperEl}
	class="live-wrapper"
	class:focused={focused || keyboardOpen}
	class:non-interactive={!interactive}
	class:inspecting
	tabindex="0"
	role="application"
	aria-label={inspecting
		? 'Live browser view — inspect mode, click an element to select it'
		: 'Live browser view — click to interact, keyboard input is sent to the page'}
	onpointerdown={onPointerDown}
	onpointerup={onPointerUp}
	onpointermove={onPointerMove}
	onpointercancel={onPointerCancel}
	onpointerleave={onPointerLeave}
	onwheel={onWheel}
	onkeydown={onKeyDown}
	onkeyup={onKeyUp}
	onfocus={() => (focused = true)}
	onblur={() => (focused = false)}
	oncontextmenu={(e) => e.preventDefault()}
>
	<canvas bind:this={canvasEl} class="live-canvas"></canvas>
	<textarea
		bind:this={keyboardEl}
		class="keyboard-bridge"
		aria-label="Type in the remote browser"
		tabindex="-1"
		disabled={!interactive || inspecting}
		autocomplete="off"
		{...{ autocorrect: 'off' }}
		autocapitalize="off"
		spellcheck={false}
		rows="1"
		onfocus={() => (keyboardOpen = true)}
		onblur={() => { keyboardOpen = false; clearBridge(); }}
		onbeforeinput={bridgeBeforeInput}
		oninput={bridgeInput}
		oncompositionstart={() => { composing = true; compositionCommit = null; }}
		oncompositionend={bridgeCompositionEnd}
		onkeydown={(e) => bridgeKey(e, 'down')}
		onkeyup={(e) => bridgeKey(e, 'up')}
	></textarea>
	<button
		type="button"
		class="keyboard-toggle"
		disabled={!interactive || inspecting}
		aria-label={keyboardOpen ? 'Dismiss keyboard' : 'Open keyboard'}
		aria-pressed={keyboardOpen}
		onpointerdown={(e) => { e.stopPropagation(); e.preventDefault(); }}
		onclick={() => { if (keyboardOpen) dismissKeyboard(); else openKeyboard(); }}
	>{keyboardOpen ? 'Dismiss keyboard' : 'Keyboard'}</button>

	{#if interactive && inspecting}
		<div class="focus-hint inspect-hint">Click an element to select it</div>
	{:else if interactive && !focused && !keyboardOpen}
		<div class="focus-hint">Click to interact</div>
	{/if}
</div>

<style>
	.keyboard-bridge {
		position: absolute;
		bottom: 8px;
		left: 8px;
		width: 1px;
		height: 1px;
		padding: 0;
		border: 0;
		opacity: 0.01;
		font-size: 16px; /* Avoid iOS focus zoom; not display:none/offscreen. */
		pointer-events: none;
	}

	.keyboard-toggle {
		position: absolute;
		top: 8px;
		right: 8px;
		min-height: 44px;
		padding: 8px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--popover);
		color: var(--foreground);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.keyboard-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.keyboard-toggle:disabled { opacity: 0.5; cursor: default; }

	.live-wrapper {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		outline: none;
		background:
			repeating-conic-gradient(
					color-mix(in oklch, var(--muted) 40%, transparent) 0% 25%,
					transparent 0% 50%
				)
				50% / 20px 20px;
		touch-action: none;
		cursor: default;
		overflow: hidden;
	}

	.live-wrapper.inspecting {
		cursor: crosshair;
	}

	.live-wrapper.non-interactive {
		cursor: default;
	}

	/* Taken out of flow entirely: the backing-store size (set in paint()) can then
	   never influence the wrapper's layout, so resizing it can't retrigger the
	   ResizeObserver. */
	.live-canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
	}

	/* Visible focus affordance: a ring around the whole live surface while it holds
	   keyboard focus, so the user knows their keystrokes are being routed here. */
	.live-wrapper.focused {
		box-shadow: inset 0 0 0 2px color-mix(in oklch, var(--ring) 80%, transparent);
	}

	.live-wrapper:focus-visible {
		box-shadow: inset 0 0 0 2px var(--ring);
	}

	.focus-hint {
		position: absolute;
		bottom: 10px;
		left: 50%;
		transform: translateX(-50%);
		padding: 4px 12px;
		font-size: 0.6875rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--foreground);
		background: color-mix(in oklch, var(--popover) 88%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		border-radius: 999px;
		backdrop-filter: blur(6px);
		pointer-events: none;
		opacity: 0.85;
		white-space: nowrap;
	}

	.inspect-hint {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 40%, transparent);
	}

	@media (prefers-reduced-motion: reduce) {
		.focus-hint {
			transition: none;
		}
	}
</style>
