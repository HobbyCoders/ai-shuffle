<script lang="ts">
	import BrowserNativeEditor from './BrowserNativeEditor.svelte';
	import BrowserFileInput from './BrowserFileInput.svelte';
	import type { BrowserEditor } from '$lib/browser-panel/workspace';
	import { browserTextChunks } from '$lib/browser-panel/text-chunks';
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
	 * (~60 Hz) and a pending move is flushed before a press or release so hover
	 * state lands first. A mouse press captures the pointer, so a drag released
	 * over a field overlay (or outside the panel) still sends its release. Wheel
	 * and key events preventDefault while focused so page hotkeys and the app's
	 * own shortcuts don't fire underneath the live view. Clipboard paste sends
	 * this device's text to the page.
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
		buttonName,
		canvasToPage,
		ClickCounter,
		fitScale,
		heldButton,
		letterbox,
		MOD_CTRL,
		remoteKey,
		remoteModifiers,
		type FrameMetadata,
		type ModifierFlags,
		type PageRect,
	} from '$lib/browser-panel/coords';

	interface FramePayload {
		data: string;
		metadata: FrameMetadata & { runtime_id?: string; tab_id?: string };
	}

	interface Props {
		/** Latest decoded-and-ready frame, or null before the first frame. */
		frame: FramePayload | null;
		editors?: BrowserEditor[];
		onEdit?: (args: Record<string, unknown>) => void;
		onPickFiles?: (field: BrowserEditor) => (files: File[]) => Promise<void>;
		/** Whether input is accepted (true only while the stream is live). */
		interactive?: boolean;
		/** Keyboard ownership is independent of the coordinate/frame gate. */
		keyboardEnabled?: boolean;
		keyboardOpen?: boolean;
		/** Authoritative CSS viewport; JPEG pixel dimensions may be smaller. */
		viewport?: { width: number; height: number };
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
		/** Ctrl/Cmd+C: copy the page's selected text to this device. Must run inside the key gesture. */
		onCopy?: () => void;
	}

	let {
		frame,
		editors = [],
		onEdit,
		onPickFiles,
		interactive: inputEnabled = true,
		keyboardEnabled: keyboardAllowed = inputEnabled,
		keyboardOpen = $bindable(false),
		viewport,
		inspecting = false,
		hoverRect = null,
		selectionRect = null,
		onInput,
		onInspect,
		onHoverLeave,
		onExitInspect,
		onCopy,
	}: Props = $props();

	let wrapperEl: HTMLDivElement | undefined = $state();
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let focused = $state(false);
	let viewWidth = $state(0);
	let viewHeight = $state(0);
	const editorScale = $derived(viewport ? Math.min(viewWidth / viewport.width, viewHeight / viewport.height) : 1);
	const editorLeft = $derived(viewport ? (viewWidth - viewport.width * editorScale) / 2 : 0);
	const editorTop = $derived(viewport ? (viewHeight - viewport.height * editorScale) / 2 : 0);

	let currentBitmap: ImageBitmap | null = null;
	let currentMeta = $state.raw<FramePayload['metadata'] | null>(null);
	// Wait for the matching bitmap to finish decoding too: receiving metadata
	// alone does not mean the picture under the pointer has changed yet.
	const waitingForFrame = $derived(currentMeta?.runtime_id !== frame?.metadata.runtime_id || currentMeta?.tab_id !== frame?.metadata.tab_id || (!!viewport && (
		currentMeta?.deviceWidth !== viewport.width ||
		currentMeta?.deviceHeight !== viewport.height ||
		frame?.metadata.deviceWidth !== viewport.width ||
		frame?.metadata.deviceHeight !== viewport.height
	)));
	const keyboardEnabled = $derived(keyboardAllowed && !waitingForFrame);
	const interactive = $derived(inputEnabled && !waitingForFrame);

	// ── Frame decode + paint (rAF-coalesced) ──────────────────────────────
	let pendingFrame: FramePayload | null = null;
	let drawScheduled = false;
	let disposed = false;
	let drawGeneration = 0;

	function base64ToBlob(b64: string, type = 'image/jpeg'): Blob {
		const bin = atob(b64);
		const bytes = new Uint8Array(bin.length);
		for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
		return new Blob([bytes], { type });
	}

	async function runDraw() {
		drawScheduled = false;
		const f = pendingFrame;
		const generation = drawGeneration;
		pendingFrame = null;
		if (!f || !canvasEl || disposed) return;
		try {
			const bmp = await createImageBitmap(base64ToBlob(f.data));
			if (
				disposed ||
				generation !== drawGeneration ||
				!metaIsDrawable(f.metadata)
			) {
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
		drawGeneration++;
		pendingFrame = f;
		if (drawScheduled) return;
		drawScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined')
			requestAnimationFrame(runDraw);
		else runDraw();
	}

	/**
	 * The page field's fill as drawn in the current frame, so the focused editor
	 * can wear the same colour instead of a theme box. It is the most common
	 * colour inside the field (text, carets and resize grips are a minority).
	 * Text gets black or white for contrast.
	 */
	function fieldSurface(f: BrowserEditor): { background: string; color: string } | null {
		const meta = currentMeta;
		if (!canvasEl || !currentBitmap || !metaIsDrawable(meta)) return null;
		try {
			const ctx = canvasEl.getContext('2d');
			const rect = canvasEl.getBoundingClientRect();
			if (!ctx || rect.width <= 0) return null;
			const box = { left: 0, top: 0, width: rect.width, height: rect.height };
			const scale = fitScale(box, meta);
			const off = letterbox(box, meta, scale);
			const dpr = canvasEl.width / rect.width;
			// Stay clear of the border (and its blur in the scaled JPEG).
			const ix = Math.min(4, f.width / 4);
			const iy = Math.min(4, f.height / 4);
			const x0 = Math.round((off.x + (f.x + ix) * scale) * dpr);
			const y0 = Math.round((off.y + (f.y + iy) * scale) * dpr);
			const w = Math.round((f.width - 2 * ix) * scale * dpr);
			const h = Math.round((f.height - 2 * iy) * scale * dpr);
			if (w < 1 || h < 1) return null;
			const data = ctx.getImageData(x0, y0, w, h).data;
			const step = Math.max(1, Math.floor((w * h) / 600));
			const groups = new Map<number, { n: number; r: number; g: number; b: number }>();
			for (let i = 0; i < w * h; i += step) {
				const o = i * 4;
				if (data[o + 3] < 200) continue; // letterbox / outside the frame
				// JPEG noise: group near-identical colours.
				const key = ((data[o] >> 4) << 8) | ((data[o + 1] >> 4) << 4) | (data[o + 2] >> 4);
				const grp = groups.get(key) ?? { n: 0, r: 0, g: 0, b: 0 };
				grp.n++;
				grp.r += data[o];
				grp.g += data[o + 1];
				grp.b += data[o + 2];
				groups.set(key, grp);
			}
			let best: { n: number; r: number; g: number; b: number } | undefined;
			for (const grp of groups.values()) if (!best || grp.n > best.n) best = grp;
			if (!best) return null;
			const [r, g, b] = [best.r, best.g, best.b].map((c) => Math.round(c / best!.n));
			const lin = (c: number) => ((c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
			const luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
			return { background: `rgb(${r} ${g} ${b})`, color: luminance > 0.18 ? '#141414' : '#f2f2f2' };
		} catch {
			return null; // no readable frame: the editor falls back to theme colours
		}
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
		const dpr =
			typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
		const bw = Math.round(rect.width * dpr);
		const bh = Math.round(rect.height * dpr);
		if (canvasEl.width !== bw) canvasEl.width = bw;
		if (canvasEl.height !== bh) canvasEl.height = bh;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, rect.width, rect.height);
		ctx.imageSmoothingQuality = 'high';
		ctx.drawImage(
			bitmap,
			off.x,
			off.y,
			meta.deviceWidth * scale,
			meta.deviceHeight * scale,
		);
		canvasEl.dataset.viewportWidth = String(meta.deviceWidth);
		canvasEl.dataset.viewportHeight = String(meta.deviceHeight);

		if (inspecting) {
			if (selectionRect)
				drawHighlight(ctx, selectionRect, scale, off, 'selection');
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
		resizeObserver = new ResizeObserver(() => {
			viewWidth = wrapperEl?.clientWidth ?? 0;
			viewHeight = wrapperEl?.clientHeight ?? 0;
			paint();
		});
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
		disposed = true;
		pendingMove = null;
		pendingFrame = null;
		currentBitmap?.close?.();
		currentBitmap = null;
	});

	// A native editable is required to summon iOS/Android keyboards. Its value is
	// only a transient IME buffer; committed text goes directly to the remote page.
	let keyboardEl: HTMLTextAreaElement | undefined = $state();
	// Focus reports bridge activation, not native OS keyboard visibility.
	let composing = false;
	let compositionCommit: string | null = null;
	let compositionTimer: ReturnType<typeof setTimeout> | undefined;

	function clearBridge() {
		if (keyboardEl) keyboardEl.value = '';
		composing = false;
		compositionCommit = null;
		clearTimeout(compositionTimer);
	}

	export function openKeyboard() {
		if (!keyboardEnabled || inspecting) return;
		// Must stay in the original user gesture, not a promise/effect/rAF.
		keyboardEl?.focus({ preventScroll: true });
	}

	export function dismissKeyboard() {
		keyboardEl?.blur();
		keyboardOpen = false;
		clearBridge();
	}

	$effect(() => {
		if (!interactive || inspecting) {
			touch = null;
			pendingMove = null;
			trailingHover = null;
			pressed.clear();
			clicks.reset();
		}
	});
	$effect(() => {
		if (!keyboardAllowed || inspecting) dismissKeyboard();
	});
	onDestroy(dismissKeyboard);

	// The page's command queue holds 128 entries; a giant paste would overflow it
	// and be cut off silently. Cap it, and say so.
	const PASTE_LIMIT = 65536;
	let notice = $state<string | null>(null);
	let noticeTimer: ReturnType<typeof setTimeout> | undefined;
	onDestroy(() => clearTimeout(noticeTimer));

	function sendText(text: string) {
		if (text.length > PASTE_LIMIT) {
			// Never end on half of a surrogate pair.
			const end = /[\uD800-\uDBFF]/.test(text[PASTE_LIMIT - 1]) ? PASTE_LIMIT - 1 : PASTE_LIMIT;
			text = text.slice(0, end);
			notice = `Pasted the first ${PASTE_LIMIT.toLocaleString('en-US')} characters; the rest was too long to send.`;
			clearTimeout(noticeTimer);
			noticeTimer = setTimeout(() => (notice = null), 5000);
		}
		for (const chunk of browserTextChunks(text))
			onInput({ type: 'input', kind: 'text', text: chunk });
	}

	function commitText(text: string) {
		if (!keyboardEnabled || inspecting || !keyboardOpen) return;
		sendText(text);
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
		compositionTimer = setTimeout(() => {
			compositionCommit = null;
		}, 0);
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

	function keyPair(key: string, code: string, keyCode: number, modifiers = 0) {
		for (const event of ['down', 'up']) {
			onInput({
				type: 'input',
				kind: 'key',
				event,
				key,
				code,
				keyCode,
				text: '',
				modifiers,
			});
		}
	}

	function bridgeKeyPair(key: string, code: string, keyCode: number) {
		if (!keyboardEnabled || inspecting || !keyboardOpen) return;
		keyPair(key, code, keyCode);
	}

	/** Arrows/paging/Tab/Escape typed in a field overlay also reach the page. */
	function overlayNavKey(key: string, code: string, keyCode: number, modifiers: number) {
		if (!keyboardEnabled || inspecting) return;
		keyPair(key, code, keyCode, modifiers);
		// Tab moved the page's focus and Escape leaves the field: keep typing
		// going to the page through the live view instead of a stale overlay.
		if (key === 'Tab' || key === 'Escape') wrapperEl?.focus({ preventScroll: true });
	}

	function bridgeBeforeInput(e: InputEvent) {
		e.stopPropagation();
		if (!keyboardEnabled || inspecting || !keyboardOpen) {
			e.preventDefault();
			clearBridge();
			return;
		}
		if (composing || e.isComposing) return;
		// An empty native buffer still emits beforeinput for virtual Backspace.
		if (e.inputType.startsWith('delete')) {
			e.preventDefault();
			const forward = e.inputType.endsWith('Forward');
			bridgeKeyPair(
				forward ? 'Delete' : 'Backspace',
				forward ? 'Delete' : 'Backspace',
				forward ? 46 : 8,
			);
		} else if (
			e.inputType === 'insertLineBreak' ||
			e.inputType === 'insertParagraph'
		) {
			e.preventDefault();
			bridgeKeyPair('Enter', 'Enter', 13);
		}
	}

	const bridgeKeys = new Set([
		'Tab',
		'Enter',
		'Backspace',
		'Delete',
		'ArrowLeft',
		'ArrowRight',
		'ArrowUp',
		'ArrowDown',
		'Home',
		'End',
		'PageUp',
		'PageDown',
	]);
	function bridgeKey(e: KeyboardEvent, event: 'down' | 'up') {
		e.stopPropagation();
		if (!keyboardEnabled || inspecting || !keyboardOpen) {
			e.preventDefault();
			return;
		}
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

	function pagePoint(e: { clientX: number; clientY: number }) {
		if (!canvasEl || !currentMeta) return null;
		return canvasToPage(
			e.clientX,
			e.clientY,
			canvasEl.getBoundingClientRect(),
			currentMeta,
		);
	}

	/** The fields of a pointer/wheel event that a remote mouse event needs. */
	type MouseSample = ModifierFlags & {
		clientX: number;
		clientY: number;
		button: number;
		buttons: number;
	};

	/**
	 * Moves and wheels report the held buttons (`none` while hovering); presses
	 * and releases report the button that changed plus their click count.
	 */
	function sendMouse(
		event: 'move' | 'down' | 'up' | 'wheel',
		e: MouseSample,
		extra: Record<string, unknown> = {},
	) {
		if (!interactive || disposed) return;
		const p = pagePoint(e);
		if (!p) return;
		// Left/right/middle/back/forward only: a pen eraser's 32 is not a mouse button.
		const buttons = (e.buttons ?? 0) & 31;
		onInput({
			type: 'input',
			kind: 'mouse',
			event,
			x: p.x,
			y: p.y,
			button:
				event === 'down' || event === 'up'
					? buttonName(e.button)
					: event === 'move'
						? heldButton(buttons)
						: 'none',
			buttons,
			clickCount: 0,
			deltaX: 0,
			deltaY: 0,
			modifiers: remoteModifiers(e),
			...extra,
		});
	}

	// ── Mouse buttons: click counting + pointer capture ───────────────────
	const clicks = new ClickCounter();
	/** Buttons pressed on the page (DOM button → its click count) awaiting release. */
	const pressed = new Map<number, number>();
	let lastPointer: MouseSample | null = null;

	function sample(e: PointerEvent): MouseSample {
		return {
			clientX: e.clientX,
			clientY: e.clientY,
			button: e.button,
			buttons: e.buttons ?? 0,
			altKey: e.altKey,
			ctrlKey: e.ctrlKey,
			metaKey: e.metaKey,
			shiftKey: e.shiftKey,
		};
	}

	/** DOM `button` → its bit in the DOM `buttons` mask. */
	function buttonBit(button: number) {
		return button === 1 ? 4 : button === 2 ? 2 : 1 << button;
	}

	function press(e: MouseSample) {
		// Only left/middle/right exist on the remote; Back/Forward stay local.
		if (e.button < 0 || e.button > 2) return;
		flushMove(); // hover state lands before the press, not after it
		const count = clicks.press(Date.now(), e.clientX, e.clientY, e.button);
		pressed.set(e.button, count);
		sendMouse('down', e, { clickCount: count });
	}

	function release(e: MouseSample) {
		// Never release a button the page did not see pressed (a press that began
		// on a field overlay, outside the panel, or before control).
		if (!pressed.has(e.button)) return;
		flushMove();
		const count = pressed.get(e.button);
		pressed.delete(e.button);
		sendMouse('up', e, { clickCount: count ?? 1 });
	}

	/** Release every button still held on the page (capture lost, cancel). */
	function releaseHeld() {
		const at = lastPointer;
		if (!at) {
			pressed.clear();
			return;
		}
		let buttons = at.buttons;
		for (const button of [...pressed.keys()]) {
			buttons &= ~buttonBit(button);
			release({ ...at, button, buttons });
		}
	}

	/** Pointer events for this view: on the canvas, or captured by a drag. */
	function ours(e: PointerEvent) {
		return (
			e.target === wrapperEl ||
			e.target === canvasEl ||
			!!wrapperEl?.hasPointerCapture?.(e.pointerId)
		);
	}

	// ── Inspect hit-testing (hover throttled to ~12 Hz, trailing kept) ────
	const HOVER_THROTTLE_MS = 80;
	let lastHoverSent = 0;
	let trailingHover: { clientX: number; clientY: number } | null = null;
	let trailingHoverTimer: ReturnType<typeof setTimeout> | null = null;

	function sendInspect(
		e: { clientX: number; clientY: number },
		mode: 'hover' | 'select',
	) {
		if (!interactive || !onInspect) return;
		if (mode === 'hover') {
			const now = Date.now();
			if (now - lastHoverSent < HOVER_THROTTLE_MS) {
				// Keep the newest position; fire it when the window closes so the
				// highlight settles under a stopped cursor instead of lagging.
				trailingHover = { clientX: e.clientX, clientY: e.clientY };
				if (trailingHoverTimer === null) {
					trailingHoverTimer = setTimeout(
						() => {
							trailingHoverTimer = null;
							const t = trailingHover;
							trailingHover = null;
							if (t) sendInspect(t, 'hover');
						},
						HOVER_THROTTLE_MS - (now - lastHoverSent),
					);
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
	/** A finger lands less precisely than a mouse: double-tap tolerance. */
	const TAP_MULTI_SLOP_PX = 14;
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
		if (touch.moved) sendMouse('wheel', e, { buttons: 0, deltaX: -dx, deltaY: -dy });
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
		// Full click sequence at the tap point; quick repeat taps double-click.
		const clickCount = clicks.press(Date.now(), e.clientX, e.clientY, 0, TAP_MULTI_SLOP_PX);
		const at = { ...sample(e), button: 0, buttons: 0 };
		sendMouse('move', at);
		sendMouse('down', at, { buttons: 1, clickCount });
		sendMouse('up', at, { buttons: 0, clickCount });
		// Remote taps never summon/refocus the native keyboard.
	}

	// ── Pointer handlers ───────────────────────────────────────────────────

	// Pointer moves coalesced to one send per animation frame (~60 Hz).
	let pendingMove: MouseSample | null = null;
	let moveScheduled = false;

	function flushMove() {
		moveScheduled = false;
		const m = pendingMove;
		pendingMove = null;
		if (m) sendMouse('move', m);
	}

	function onPointerMove(e: PointerEvent) {
		if (!ours(e)) return;
		if (!interactive || disposed) return;
		if (e.pointerType === 'touch') {
			onTouchMove(e);
			return;
		}
		if (inspecting) {
			sendInspect(e, 'hover');
			return;
		}
		lastPointer = sample(e);
		// A second button pressed or released mid-drag arrives as a move.
		if (e.button >= 0 && e.button <= 2) {
			if (e.buttons & buttonBit(e.button)) press(lastPointer);
			else if (pressed.has(e.button)) release(lastPointer);
			return;
		}
		pendingMove = lastPointer;
		if (moveScheduled) return;
		moveScheduled = true;
		if (typeof requestAnimationFrame !== 'undefined')
			requestAnimationFrame(flushMove);
		else flushMove();
	}

	function onPointerDown(e: PointerEvent) {
		if (e.target !== wrapperEl && e.target !== canvasEl) return;
		// Tapping outside a native field dismisses its keyboard/selection normally.
		const active = document.activeElement;
		if (active !== keyboardEl && (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) && wrapperEl?.contains(active)) active.blur();
		// Keep the legacy provider-login IME buffer focused across remote clicks.
		// Never refocus here: native IME Hide may leave that textarea focused.
		if (keyboardOpen && document.activeElement === keyboardEl) e.preventDefault();
		else if (interactive && e.pointerType !== 'touch') wrapperEl?.focus();
		if (!interactive || disposed) return;
		e.preventDefault();
		if (e.pointerType === 'touch') {
			onTouchDown(e);
			return;
		}
		if (inspecting) return; // select fires on pointerup
		// Keep the drag's release even if it ends over a field overlay or
		// outside the panel: the page must never be left with a button held.
		try {
			wrapperEl?.setPointerCapture?.(e.pointerId);
		} catch {
			// Pointer already gone (synthetic or released) — nothing to capture.
		}
		lastPointer = sample(e);
		press(lastPointer);
	}

	function onPointerUp(e: PointerEvent) {
		if (!ours(e)) return;
		if (!interactive || disposed) return;
		if (e.pointerType === 'touch') {
			onTouchUp(e);
			return;
		}
		if (inspecting) {
			sendInspect(e, 'select');
			return;
		}
		lastPointer = sample(e);
		release(lastPointer);
	}

	function onPointerCancel(e: PointerEvent) {
		if (touch && e.pointerId === touch.id) touch = null;
		if (e.pointerType !== 'touch') releaseHeld();
	}

	function onPointerLeave() {
		if (!inspecting) return;
		// Drop a queued trailing hover so it can't re-highlight after leaving.
		trailingHover = null;
		onHoverLeave?.();
	}

	function onWheel(e: WheelEvent) {
		if (e.target !== wrapperEl && e.target !== canvasEl) return;
		if (!interactive || disposed) return;
		e.preventDefault();
		// Inspect mode still scrolls — deeper content must stay reachable.
		sendMouse('wheel', e, { deltaX: e.deltaX, deltaY: e.deltaY });
	}

	function sendKey(event: 'down' | 'up', e: KeyboardEvent) {
		onInput({ type: 'input', kind: 'key', event, ...remoteKey(e, event) });
	}

	/** Ctrl/Cmd+V: let the native paste event bring this device's clipboard. */
	function isPasteShortcut(e: KeyboardEvent) {
		return (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'v';
	}

	// Safari raises no paste event on non-editable content, so if none follows
	// the shortcut promptly, the remote pastes from its own clipboard instead.
	let pasteFallback: ReturnType<typeof setTimeout> | undefined;
	onDestroy(() => clearTimeout(pasteFallback));
	function expectPaste() {
		clearTimeout(pasteFallback);
		pasteFallback = setTimeout(() => {
			pasteFallback = undefined;
			if (interactive && !inspecting && !disposed) keyPair('v', 'KeyV', 86, MOD_CTRL);
		}, 100);
	}

	// WebKit only fires paste on non-editable content after a cancelled beforepaste.
	$effect(() => {
		const el = wrapperEl;
		if (!el) return;
		const allow = (e: Event) => {
			if (e.target === el) e.preventDefault();
		};
		el.addEventListener('beforepaste', allow);
		return () => el.removeEventListener('beforepaste', allow);
	});

	function onPaste(e: ClipboardEvent) {
		if (e.target !== wrapperEl || !interactive || !focused || inspecting) return;
		clearTimeout(pasteFallback);
		pasteFallback = undefined;
		e.preventDefault();
		e.stopPropagation();
		const text = e.clipboardData?.getData('text/plain') ?? '';
		// Nothing textual (an image, an empty clipboard): let the remote browser
		// paste from its own clipboard, as Ctrl+V did before.
		if (text) sendText(text);
		else keyPair('v', 'KeyV', 86, MOD_CTRL);
	}

	function onKeyDown(e: KeyboardEvent) {
		if (e.target !== wrapperEl || !interactive || !focused) return;
		if (!inspecting && isPasteShortcut(e)) {
			// No preventDefault: that would cancel the paste event itself.
			e.stopPropagation();
			if (!e.repeat) expectPaste();
			return;
		}
		// Keep the keystroke inside the live view — don't let it trigger app hotkeys.
		e.preventDefault();
		e.stopPropagation();
		if (inspecting) {
			// Keys are not forwarded while inspecting; Escape leaves the mode.
			if (e.key === 'Escape') onExitInspect?.();
			return;
		}
		// The key still reaches the page; the copy itself comes back to this device.
		if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'c' && !e.repeat) onCopy?.();
		sendKey('down', e);
	}

	function onKeyUp(e: KeyboardEvent) {
		if (e.target !== wrapperEl || !interactive || !focused) return;
		if (!inspecting && isPasteShortcut(e)) {
			e.stopPropagation();
			return;
		}
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
	onlostpointercapture={(e) => {
		if (e.pointerType !== 'touch') releaseHeld();
	}}
	onpointerleave={onPointerLeave}
	onwheel={onWheel}
	onkeydown={onKeyDown}
	onkeyup={onKeyUp}
	onpaste={onPaste}
	onfocus={() => (focused = true)}
	onblur={() => (focused = false)}
	oncontextmenu={(e) => e.preventDefault()}
>
	<canvas bind:this={canvasEl} class="live-canvas"></canvas>
	{#if interactive && !inspecting && onEdit}
		{#each editors as field (field.id)}
			{#if field.type === 'file'}
				{#if onPickFiles}
					<BrowserFileInput {field} scale={editorScale} left={editorLeft} top={editorTop} onPick={onPickFiles} onActive={(active) => keyboardOpen = active} />
				{/if}
			{:else}
				<BrowserNativeEditor {field} {viewWidth} {viewHeight} scale={editorScale} left={editorLeft} top={editorTop} {onEdit} onActive={(active) => keyboardOpen = active} onEnter={() => bridgeKeyPair('Enter', 'Enter', 13)} onText={commitText} onKey={(k) => bridgeKeyPair(k, k, k === 'Delete' ? 46 : 8)} onNavKey={overlayNavKey} surface={() => fieldSurface(field)} />
			{/if}
		{/each}
	{/if}
	<textarea
		bind:this={keyboardEl}
		class="keyboard-bridge"
		aria-label="Type in the remote browser"
		tabindex="-1"
		disabled={!keyboardAllowed || inspecting}
		autocomplete="off"
		{...{ autocorrect: 'off' }}
		autocapitalize="off"
		spellcheck={false}
		rows="1"
		onfocus={() => (keyboardOpen = true)}
		onblur={() => {
			keyboardOpen = false;
			clearBridge();
		}}
		onbeforeinput={bridgeBeforeInput}
		oninput={bridgeInput}
		oncompositionstart={() => {
			composing = true;
			compositionCommit = null;
		}}
		oncompositionend={bridgeCompositionEnd}
		onkeydown={(e) => bridgeKey(e, 'down')}
		onkeyup={(e) => bridgeKey(e, 'up')}></textarea>


	{#if notice}
		<div class="focus-hint" role="status">{notice}</div>
	{:else if waitingForFrame}
		<div class="focus-hint" role="status">Waiting for an updated frame · view only</div>
	{:else if interactive && inspecting}
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

	.live-wrapper {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		outline: none;
		background: var(--muted);
		touch-action: auto;
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
		touch-action: none;
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
	}

	/* Visible focus affordance: a ring around the whole live surface while it holds
	   keyboard focus, so the user knows their keystrokes are being routed here. */
	.live-wrapper.focused {
		box-shadow: inset 0 0 0 2px
			color-mix(in oklch, var(--ring) 80%, transparent);
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
