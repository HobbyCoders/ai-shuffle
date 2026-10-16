<script lang="ts">
	/**
	 * PhoneScreen — paints emulator frames into a letterboxed canvas (same fit
	 * math as the Browser panel) with the screen's rounded corners, and turns
	 * pointer, wheel and keyboard input into device-pixel messages.
	 *
	 *   click → tap · drag → swipe (real duration) · hold → long press
	 *   wheel → one short swipe per burst · keys → text / ENTER / DEL / BACK
	 *
	 * Phones need a focused native editable to raise the soft keyboard, so a
	 * hidden textarea bridges it (see BrowserLiveView for the same idea); the
	 * panel's keyboard button focuses it inside the user's gesture.
	 */
	import { onDestroy } from 'svelte';
	import { fitScale, letterbox } from '$lib/browser-panel/coords';
	import {
		clientToDevice,
		gestureMessage,
		insideScreen,
		keyboardMessage,
		keyMessage,
		textMessages,
		wheelPixels,
		wheelSwipe,
		type ClientMessage,
		type DeviceSize,
		type FrameMessage,
	} from '$lib/emulator/protocol';

	interface Props {
		frame: FrameMessage | null;
		/** Input is accepted (socket open, device running). */
		interactive?: boolean;
		/** The picture on screen is old (socket down). */
		stale?: boolean;
		keyboardOpen?: boolean;
		onSend: (msg: ClientMessage) => void;
	}

	let { frame, interactive = true, stale = false, keyboardOpen = $bindable(false), onSend }: Props = $props();

	let wrapperEl: HTMLDivElement | undefined = $state();
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let keyboardEl: HTMLTextAreaElement | undefined = $state();
	let focused = $state(false);
	let painted = $state(false);
	let size = $state.raw<DeviceSize | null>(null);
	let bitmap: ImageBitmap | null = null;
	let disposed = false;
	let pending: FrameMessage | null = null;
	let drawScheduled = false;

	// ── Decode + paint ─────────────────────────────────────────────────────
	function toBlob(b64: string): Blob {
		const bin = atob(b64);
		const bytes = new Uint8Array(bin.length);
		for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
		return new Blob([bytes], { type: 'image/jpeg' });
	}

	async function runDraw() {
		drawScheduled = false;
		const f = pending;
		pending = null;
		if (!f || disposed || typeof createImageBitmap === 'undefined') return;
		try {
			const bmp = await createImageBitmap(toBlob(f.data));
			if (disposed) { bmp.close?.(); return; }
			bitmap?.close?.();
			bitmap = bmp;
			size = { width: f.width, height: f.height };
			paint();
		} catch {
			// A bad frame skips one paint; the next one recovers.
		}
	}

	$effect(() => {
		const f = frame;
		if (!f) return;
		// Only the newest frame is decoded; frames arriving mid-decode coalesce.
		pending = f;
		if (drawScheduled) return;
		drawScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined') requestAnimationFrame(runDraw);
		else void runDraw();
	});

	function screenBox() {
		if (!canvasEl || !size) return null;
		const rect = canvasEl.getBoundingClientRect();
		const meta = { deviceWidth: size.width, deviceHeight: size.height };
		const scale = fitScale(rect, meta);
		if (!(scale > 0)) return null;
		const off = letterbox(rect, meta, scale);
		return { rect, scale, x: off.x, y: off.y, w: size.width * scale, h: size.height * scale };
	}

	function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
		ctx.beginPath();
		if (typeof ctx.roundRect === 'function') ctx.roundRect(x, y, w, h, r);
		else ctx.rect(x, y, w, h);
	}

	function paint() {
		const box = screenBox();
		if (!canvasEl || !bitmap || !box) return;
		const { rect, x, y, w, h } = box;
		if (rect.width <= 0 || rect.height <= 0) return;
		const ctx = canvasEl.getContext('2d');
		if (!ctx) return;
		const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
		const bw = Math.round(rect.width * dpr), bh = Math.round(rect.height * dpr);
		if (canvasEl.width !== bw) canvasEl.width = bw;
		if (canvasEl.height !== bh) canvasEl.height = bh;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, rect.width, rect.height);
		// Phone screens have rounded corners; so does this one.
		const radius = Math.min(w, h) * 0.045;
		ctx.save();
		roundRect(ctx, x, y, w, h, radius);
		ctx.clip();
		ctx.imageSmoothingQuality = 'high';
		ctx.drawImage(bitmap, x, y, w, h);
		ctx.restore();
		canvasEl.dataset.deviceWidth = String(size?.width ?? 0);
		canvasEl.dataset.deviceHeight = String(size?.height ?? 0);
		painted = true;
	}

	$effect(() => {
		if (!wrapperEl || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => paint());
		ro.observe(wrapperEl);
		return () => ro.disconnect();
	});

	onDestroy(() => {
		disposed = true;
		bitmap?.close?.();
		bitmap = null;
		clearTimeout(wheelTimer);
		keyboardEl?.blur();
	});

	// ── Touch feedback ─────────────────────────────────────────────────────
	type Ripple = { id: number; x: number; y: number; hold: boolean };
	let ripples = $state<Ripple[]>([]);
	let rippleId = 0;
	function ripple(clientX: number, clientY: number, hold = false) {
		if (!wrapperEl) return;
		const r = wrapperEl.getBoundingClientRect();
		const id = ++rippleId;
		ripples = [...ripples.slice(-4), { id, x: clientX - r.left, y: clientY - r.top, hold }];
		setTimeout(() => (ripples = ripples.filter((p) => p.id !== id)), 520);
	}

	// ── Pointer → tap / swipe ──────────────────────────────────────────────
	let press: { id: number; sx: number; sy: number; x: number; y: number; t: number } | null = null;
	let trail = $state<{ x1: number; y1: number; x2: number; y2: number } | null>(null);

	function send(msg: ClientMessage | null) {
		if (msg && interactive && !disposed) onSend(msg);
	}

	function onPointerDown(e: PointerEvent) {
		if (e.target !== canvasEl) return;
		if (e.pointerType === 'mouse' && e.button !== 0) return;
		// Tapping the phone keeps the soft keyboard up while typing; on desktop it
		// takes focus so hardware keys reach the phone.
		if (keyboardOpen && document.activeElement === keyboardEl) e.preventDefault();
		else wrapperEl?.focus({ preventScroll: true });
		if (!interactive || !size || press) return;
		const rect = canvasEl.getBoundingClientRect();
		if (!insideScreen(e.clientX, e.clientY, rect, size)) return;
		e.preventDefault();
		try { canvasEl.setPointerCapture(e.pointerId); } catch { /* jsdom */ }
		press = { id: e.pointerId, sx: e.clientX, sy: e.clientY, x: e.clientX, y: e.clientY, t: performance.now() };
	}

	function onPointerMove(e: PointerEvent) {
		if (!press || e.pointerId !== press.id || !wrapperEl) return;
		press.x = e.clientX;
		press.y = e.clientY;
		const r = wrapperEl.getBoundingClientRect();
		trail = Math.hypot(press.x - press.sx, press.y - press.sy) > 8
			? { x1: press.sx - r.left, y1: press.sy - r.top, x2: press.x - r.left, y2: press.y - r.top }
			: null;
	}

	function onPointerUp(e: PointerEvent) {
		if (!press || e.pointerId !== press.id || !canvasEl || !size) return;
		const p = press;
		press = null;
		trail = null;
		const rect = canvasEl.getBoundingClientRect();
		const start = clientToDevice(p.sx, p.sy, rect, size);
		const end = clientToDevice(e.clientX, e.clientY, rect, size);
		if (!start || !end) return;
		const msg = gestureMessage({
			startClient: { x: p.sx, y: p.sy },
			endClient: { x: e.clientX, y: e.clientY },
			start,
			end,
			durationMs: performance.now() - p.t,
		}, size);
		if (msg.type === 'tap') ripple(p.sx, p.sy);
		else if (msg.type === 'swipe' && msg.x1 === msg.x2 && msg.y1 === msg.y2) ripple(p.sx, p.sy, true);
		send(msg);
	}

	function onPointerCancel(e: PointerEvent) {
		if (press && e.pointerId === press.id) { press = null; trail = null; }
	}

	// ── Wheel → one swipe per burst ────────────────────────────────────────
	let wheelAcc = 0;
	let wheelAt: { x: number; y: number } | null = null;
	let wheelTimer: ReturnType<typeof setTimeout> | undefined;
	function onWheel(e: WheelEvent) {
		if (!canvasEl || !size) return;
		e.preventDefault();
		if (!interactive) return;
		wheelAcc += wheelPixels(e.deltaY, e.deltaMode, canvasEl.clientHeight || 600);
		wheelAt = { x: e.clientX, y: e.clientY };
		clearTimeout(wheelTimer);
		wheelTimer = setTimeout(flushWheel, 90);
	}
	function flushWheel() {
		const box = screenBox();
		const at = wheelAt;
		const delta = wheelAcc;
		wheelAcc = 0;
		wheelAt = null;
		if (!box || !at || !size || !canvasEl) return;
		const p = clientToDevice(at.x, at.y, box.rect, size);
		if (p) send(wheelSwipe(p, delta, box.scale, size));
	}

	// ── Hardware keyboard (desktop) ───────────────────────────────────────
	function onKeyDown(e: KeyboardEvent) {
		if (e.target !== wrapperEl || !interactive) return;
		const msg = keyboardMessage(e);
		if (!msg) return;
		e.preventDefault();
		e.stopPropagation();
		send(msg);
	}
	function onPaste(e: ClipboardEvent) {
		if (!interactive) return;
		const text = e.clipboardData?.getData('text/plain') ?? '';
		if (!text) return;
		e.preventDefault();
		for (const m of textMessages(text)) send(m);
	}

	// ── Soft keyboard bridge (phones) ─────────────────────────────────────
	let composing = false;
	let compositionCommit: string | null = null;

	export function openKeyboard() {
		if (!interactive) return;
		// Must run inside the user's tap: browsers raise the keyboard only then.
		keyboardEl?.focus({ preventScroll: true });
	}
	export function closeKeyboard() {
		keyboardEl?.blur();
	}
	$effect(() => {
		if (!interactive) closeKeyboard();
	});

	function bridgeBeforeInput(e: InputEvent) {
		e.stopPropagation();
		if (!interactive) { e.preventDefault(); return; }
		if (composing || e.isComposing) return;
		if (e.inputType.startsWith('delete')) {
			e.preventDefault();
			send(keyMessage('DEL'));
		} else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') {
			e.preventDefault();
			send(keyMessage('ENTER'));
		}
	}
	function bridgeInput(e: Event) {
		e.stopPropagation();
		if (composing || (e as InputEvent).isComposing) return;
		const text = keyboardEl?.value ?? '';
		if (keyboardEl) keyboardEl.value = '';
		if (text && text !== compositionCommit) for (const m of textMessages(text)) send(m);
		compositionCommit = null;
	}
	function bridgeCompositionEnd(e: CompositionEvent) {
		composing = false;
		if (e.data) for (const m of textMessages(e.data)) send(m);
		if (keyboardEl) keyboardEl.value = '';
		// Some engines fire a trailing input with the same commit; skip it once.
		compositionCommit = e.data;
		setTimeout(() => (compositionCommit = null), 0);
	}
	function bridgeKeyDown(e: KeyboardEvent) {
		e.stopPropagation();
		if (composing || e.isComposing || e.keyCode === 229) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			closeKeyboard();
		} else if (e.key === 'Enter' || e.key === 'Backspace') {
			// A hardware key on a tablet: send it once, skip the native edit.
			e.preventDefault();
			send(keyMessage(e.key === 'Enter' ? 'ENTER' : 'DEL'));
		}
	}
</script>

<!-- The phone screen is an application surface: it takes raw pointer and key
     input and forwards it, so the non-interactive a11y heuristics don't apply. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={wrapperEl}
	class="screen"
	class:focused
	class:typing={keyboardOpen}
	class:stale
	class:inert-screen={!interactive}
	tabindex="0"
	role="application"
	aria-label="Phone screen. Click to tap, drag to swipe, scroll to swipe, type to enter text. Escape is Back."
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerUp}
	onpointercancel={onPointerCancel}
	onwheel={onWheel}
	onkeydown={onKeyDown}
	onpaste={onPaste}
	onfocus={() => (focused = true)}
	onblur={() => (focused = false)}
	oncontextmenu={(e) => e.preventDefault()}
>
	<canvas bind:this={canvasEl} class="canvas" class:painted data-testid="phone-canvas"></canvas>
	{#if trail}
		<svg class="trail" aria-hidden="true">
			<line x1={trail.x1} y1={trail.y1} x2={trail.x2} y2={trail.y2} />
			<circle cx={trail.x2} cy={trail.y2} r="9" />
		</svg>
	{/if}
	{#each ripples as r (r.id)}
		<span class="ripple" class:hold={r.hold} style:left={`${r.x}px`} style:top={`${r.y}px`} aria-hidden="true"></span>
	{/each}
	<textarea
		bind:this={keyboardEl}
		class="keyboard-bridge"
		aria-label="Type on the phone"
		tabindex="-1"
		disabled={!interactive}
		autocomplete="off"
		{...{ autocorrect: 'off' }}
		autocapitalize="off"
		spellcheck={false}
		rows="1"
		onfocus={() => (keyboardOpen = true)}
		onblur={() => { keyboardOpen = false; composing = false; if (keyboardEl) keyboardEl.value = ''; }}
		onbeforeinput={bridgeBeforeInput}
		oninput={bridgeInput}
		oncompositionstart={() => { composing = true; compositionCommit = null; }}
		oncompositionend={bridgeCompositionEnd}
		onkeydown={bridgeKeyDown}
	></textarea>
</div>

<style>
	.screen {
		position: relative;
		flex: 1;
		min-height: 0;
		min-width: 0;
		outline: none;
		overflow: hidden;
		border-radius: calc(var(--radius) - 2px);
		transition: box-shadow 160ms ease;
	}
	.canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
		touch-action: none;
		cursor: pointer;
		opacity: 0;
		transition: opacity 240ms ease, filter 240ms ease;
	}
	.canvas.painted { opacity: 1; }
	.inert-screen .canvas { cursor: default; }
	/* Old picture while the socket is down: still readable, clearly not live. */
	.stale .canvas { filter: grayscale(0.65) brightness(0.8); }
	.screen.focused,
	.screen.typing { box-shadow: inset 0 0 0 2px color-mix(in oklch, var(--ring) 70%, transparent); }
	.screen:focus-visible { box-shadow: inset 0 0 0 2px var(--ring); }

	.ripple {
		position: absolute;
		width: 34px;
		height: 34px;
		margin: -17px 0 0 -17px;
		border-radius: 50%;
		background: color-mix(in oklch, var(--primary) 30%, transparent);
		border: 1.5px solid color-mix(in oklch, var(--primary) 70%, transparent);
		pointer-events: none;
		animation: tap 480ms cubic-bezier(0.2, 0.7, 0.3, 1) forwards;
	}
	.ripple.hold { animation-duration: 520ms; background: color-mix(in oklch, var(--primary) 45%, transparent); }
	@keyframes tap {
		from { transform: scale(0.4); opacity: 1; }
		to { transform: scale(1.5); opacity: 0; }
	}
	.trail {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		pointer-events: none;
		overflow: visible;
	}
	.trail line {
		stroke: color-mix(in oklch, var(--primary) 65%, transparent);
		stroke-width: 3;
		stroke-linecap: round;
	}
	.trail circle {
		fill: color-mix(in oklch, var(--primary) 30%, transparent);
		stroke: color-mix(in oklch, var(--primary) 75%, transparent);
		stroke-width: 1.5;
	}

	.keyboard-bridge {
		position: absolute;
		bottom: 8px;
		left: 8px;
		width: 1px;
		height: 1px;
		padding: 0;
		border: 0;
		opacity: 0.01;
		font-size: 16px; /* no iOS focus zoom; must not be display:none */
		pointer-events: none;
		resize: none;
	}

	@media (prefers-reduced-motion: reduce) {
		.ripple { animation: none; opacity: 0; }
		.canvas, .screen { transition: none; }
	}
</style>
