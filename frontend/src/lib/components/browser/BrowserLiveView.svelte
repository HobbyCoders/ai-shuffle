<script lang="ts">
	/**
	 * BrowserLiveView — draws the screencast frames into a letterboxed,
	 * aspect-preserving canvas and forwards user input (mouse / wheel / keyboard)
	 * back to the agent's page in PAGE CSS pixels.
	 *
	 * Drawing: each incoming base64 JPEG is decoded to an ImageBitmap and painted
	 * on requestAnimationFrame (intermediate frames are coalesced). The paint uses
	 * the SAME fit-scale + letterbox math as the input mapping (coords.ts), so a
	 * click always lands where the cursor visually is.
	 *
	 * Input: captured on a focusable wrapper. Pointer moves are rAF-coalesced
	 * (~60 Hz), wheel and key events preventDefault while focused so page hotkeys
	 * and the app's own shortcuts don't fire underneath the live view.
	 */
	import { onDestroy } from 'svelte';
	import {
		canvasToPage,
		fitScale,
		letterbox,
		modifiersFromEvent,
		type FrameMetadata,
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
		/** Send a client→server input payload over the socket. */
		onInput: (payload: Record<string, unknown>) => void;
	}

	let { frame, interactive = true, onInput }: Props = $props();

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

	function paint() {
		if (!canvasEl || !currentBitmap || !currentMeta) return;
		const rect = canvasEl.getBoundingClientRect();
		if (rect.width <= 0 || rect.height <= 0) return;
		const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
		const bw = Math.round(rect.width * dpr);
		const bh = Math.round(rect.height * dpr);
		if (canvasEl.width !== bw) canvasEl.width = bw;
		if (canvasEl.height !== bh) canvasEl.height = bh;
		const ctx = canvasEl.getContext('2d');
		if (!ctx) return;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, rect.width, rect.height);
		const box = { left: 0, top: 0, width: rect.width, height: rect.height };
		const scale = fitScale(box, currentMeta);
		if (scale <= 0) return;
		const off = letterbox(box, currentMeta, scale);
		ctx.imageSmoothingQuality = 'high';
		ctx.drawImage(
			currentBitmap,
			off.x,
			off.y,
			currentMeta.deviceWidth * scale,
			currentMeta.deviceHeight * scale,
		);
	}

	// Repaint on container resize so the letterbox re-fits.
	let resizeObserver: ResizeObserver | null = null;
	$effect(() => {
		if (!canvasEl || typeof ResizeObserver === 'undefined') return;
		resizeObserver = new ResizeObserver(() => paint());
		resizeObserver.observe(canvasEl);
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

	onDestroy(() => {
		currentBitmap?.close?.();
		currentBitmap = null;
	});

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
		if (!interactive) return;
		pendingMove = e;
		if (moveScheduled) return;
		moveScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined') requestAnimationFrame(flushMove);
		else flushMove();
	}

	function onPointerDown(e: PointerEvent) {
		wrapperEl?.focus();
		if (!interactive) return;
		e.preventDefault();
		sendMouse('down', e);
	}

	function onPointerUp(e: PointerEvent) {
		if (!interactive) return;
		sendMouse('up', e);
	}

	function onWheel(e: WheelEvent) {
		if (!interactive) return;
		e.preventDefault();
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
		if (!interactive || !focused) return;
		// Keep the keystroke inside the live view — don't let it trigger app hotkeys.
		e.preventDefault();
		e.stopPropagation();
		sendKey('down', e);
	}

	function onKeyUp(e: KeyboardEvent) {
		if (!interactive || !focused) return;
		e.preventDefault();
		e.stopPropagation();
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
	class:focused
	class:non-interactive={!interactive}
	tabindex="0"
	role="application"
	aria-label="Live browser view — click to interact, keyboard input is sent to the page"
	onpointerdown={onPointerDown}
	onpointerup={onPointerUp}
	onpointermove={onPointerMove}
	onwheel={onWheel}
	onkeydown={onKeyDown}
	onkeyup={onKeyUp}
	onfocus={() => (focused = true)}
	onblur={() => (focused = false)}
	oncontextmenu={(e) => e.preventDefault()}
>
	<canvas bind:this={canvasEl} class="live-canvas"></canvas>
	{#if interactive && !focused}
		<div class="focus-hint">Click to interact</div>
	{/if}
</div>

<style>
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
		cursor: crosshair;
	}

	.live-wrapper.non-interactive {
		cursor: default;
	}

	.live-canvas {
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
	}

	@media (prefers-reduced-motion: reduce) {
		.focus-hint {
			transition: none;
		}
	}
</style>
