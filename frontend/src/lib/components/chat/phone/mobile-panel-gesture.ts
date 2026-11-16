import type { ComposerGestureHandle, ComposerOpenOptions } from '$lib/components/chat/composer-shell';
import { FlickTracker, coalescedMoves, gestureClock, type GestureClock } from '$lib/utils/swipe-arm';

export interface GesturePoint { x: number; y: number; time: number; }
export interface PanelGesture {
	start: GesturePoint;
	mode: 'pending' | 'scroll' | 'dismiss';
	offset: number;
	ended: boolean;
	/** The finger's speed along the pull, read at release (#1637). */
	flick: FlickTracker;
}

export function createPanelGesture(point: GesturePoint & { eligible: boolean }): PanelGesture {
	const flick = new FlickTracker();
	flick.addAt(point.y, point.time);
	return { start: { x: point.x, y: point.y, time: point.time }, mode: point.eligible ? 'pending' : 'scroll', offset: 0, ended: false, flick };
}

export function updatePanelGesture(state: PanelGesture, point: GesturePoint) {
	if (state.ended || state.mode === 'scroll') return { mode: state.mode, offset: state.offset };
	state.flick.addAt(point.y, point.time);
	const dx = point.x - state.start.x;
	const dy = point.y - state.start.y;
	if (state.mode === 'pending' && Math.max(Math.abs(dx), Math.abs(dy)) > 8) {
		state.mode = dy > 0 && dy > Math.abs(dx) ? 'dismiss' : 'scroll';
	}
	state.offset = state.mode === 'dismiss' ? Math.max(0, dy) : 0;
	return { mode: state.mode, offset: state.offset };
}

export function finishPanelGesture(state: PanelGesture, point: GesturePoint, panelHeight: number): boolean {
	if (state.ended) return false;
	const wasDismiss = state.mode === 'dismiss';
	// The speed of the last moves, zero if the finger rested before lifting;
	// read before the lift itself counts as a move.
	const velocity = state.flick.velocityAt(point.time);
	updatePanelGesture(state, point);
	state.ended = true;
	return wasDismiss && (state.offset >= Math.max(24, Math.min(72, panelHeight * 0.3)) || (state.offset > 16 && velocity > 0.6));
}

interface ComposerGestureIntent {
	target: EventTarget | null;
	eligible: boolean;
	direction: number;
	open: boolean;
	onOpenChange: (open: boolean) => void;
}

/** Editor eligibility is sampled before pointerdown expands the collapsed form.
 * Other composer surfaces remain available with a draft or expanded editor. */
export function canStartComposerOpen(target: EventTarget | null, composer: HTMLElement, allowEditor: boolean): boolean {
	const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
	if (!element || !composer.contains(element) || element.closest('.mobile-composer-controls')) return false;
	if (element.closest('input, textarea, select, [role="slider"]')) return false;
	const editor = element.closest('[contenteditable]:not([contenteditable="false"]), [role="textbox"]');
	if (editor && (!allowEditor || editor.contains(document.activeElement) || !window.getSelection()?.isCollapsed)) return false;
	for (let current: Element | null = element; current; current = current.parentElement) {
		if (current.scrollHeight > current.clientHeight && current.clientHeight > 0 && /auto|scroll/.test(getComputedStyle(current).overflowY)) return false;
		if (current === composer) return true;
	}
	return false;
}

/** Both opening and closing belong to the composer, never the controls panel.
 * Touch Events arbitrate before native pan; Pointer Events snapshot eligibility,
 * layout and requested state before the collapsed composer can reflow. */
export function attachComposerOpen(composer: HTMLElement, options: ComposerOpenOptions): ComposerGestureHandle {
	let snapshot: ComposerGestureIntent | null = null;
	let intent: ComposerGestureIntent | null = null;
	let gesture: PanelGesture | null = null;
	let identifier = -1;
	let origin: EventTarget | null = null;
	let consumed = false;
	let dragged = false;
	let exceededTapSlop = false;
	let suppressClickUntil = 0;
	// Every time in one touch is on the clock its touchstart picked.
	let clock: GestureClock = gestureClock();
	const point = (x: number, y: number, event: Event) => ({ x, y: y * intent!.direction, time: clock(event) });
	function clear() {
		gesture = null; snapshot = null; intent = null; identifier = -1; origin = null;
		consumed = false; dragged = false; exceededTapSlop = false;
	}
	function cancel() {
		if (consumed) suppressClickUntil = performance.now() + 500;
		clear();
	}
	function sample(target: EventTarget | null): ComposerGestureIntent {
		const open = !options.open();
		const openingDirection = options.openDirection() === 'up' ? -1 : 1;
		return {
			target,
			eligible: options.enabled() && canStartComposerOpen(target, composer, options.allowEditor()),
			direction: open ? openingDirection : -openingDirection,
			open,
			onOpenChange: options.onOpenChange
		};
	}
	function pointerDown(event: PointerEvent) {
		if (event.pointerType !== 'touch') return;
		if (gesture || event.isPrimary === false) { cancel(); return; }
		// A fresh touch is intentional, not the previous drag's synthetic click.
		suppressClickUntil = 0;
		snapshot = sample(event.target);
	}
	function start(event: TouchEvent) {
		if (event.touches.length !== 1) { cancel(); return; }
		if (gesture) return;
		const sampled = snapshot?.target === event.target ? snapshot : sample(event.target);
		snapshot = null;
		if (!sampled.eligible || !options.enabled()) return;
		intent = sampled;
		const touch = event.touches[0];
		identifier = touch.identifier; origin = event.target; suppressClickUntil = 0;
		clock = gestureClock(event);
		gesture = createPanelGesture({ ...point(touch.clientX, touch.clientY, event), eligible: true });
	}
	function move(event: TouchEvent) {
		if (!gesture) return;
		if (event.touches.length !== 1 || !options.enabled() || !event.cancelable || !canStartComposerOpen(origin, composer, true)) { cancel(); return; }
		const touch = Array.from(event.touches).find(t => t.identifier === identifier);
		if (!touch) { cancel(); return; }
		const next = point(touch.clientX, touch.clientY, event);
		const dx = next.x - gesture.start.x, dy = next.y - gesture.start.y;
		if (Math.max(Math.abs(dx), Math.abs(dy)) > 8) exceededTapSlop = true;
		// Decide ownership on the first move, before the browser starts a native
		// pan; updatePanelGesture's 8px slop would decide too late.
		if (gesture.mode === 'pending' && (dx || dy)) gesture.mode = dy > 0 && dy > Math.abs(dx) ? 'dismiss' : 'scroll';
		updatePanelGesture(gesture, next);
		if (gesture.mode !== 'dismiss') return;
		event.preventDefault(); consumed = true;
		if (gesture.offset > 8) dragged = true;
	}
	// Touch events carry no coalesced moves; the pointer events for the same
	// finger do, so a busy page's bunched pull still reads its own speed.
	function pointerMove(event: PointerEvent) {
		if (!gesture || !intent || gesture.ended || gesture.mode === 'scroll' || event.pointerType !== 'touch' || event.isPrimary === false) return;
		for (const m of coalescedMoves(event)) gesture.flick.addAt(m.clientY * intent.direction, clock(m));
	}
	function end(event: TouchEvent) {
		if (!gesture) { snapshot = null; return; }
		if (event.touches.length) { cancel(); return; }
		const touch = Array.from(event.changedTouches).find(t => t.identifier === identifier);
		if (!touch) { cancel(); return; }
		const next = point(touch.clientX, touch.clientY, event);
		const withinSlop = Math.max(Math.abs(next.x - gesture.start.x), Math.abs(next.y - gesture.start.y)) <= 8;
		const element = origin instanceof Element ? origin : null;
		const tap = consumed && !dragged && !exceededTapSlop && withinSlop && next.time - gesture.start.time < 500 && options.enabled()
			? element?.closest<HTMLElement>('button, a[href], [role="button"], [role="checkbox"]') : null;
		const change = options.enabled() && dragged && finishPanelGesture(gesture, next, 0) ? intent : null;
		if (consumed) suppressClickUntil = performance.now() + 500;
		clear();
		if (change) change.onOpenChange(change.open);
		else if (tap && composer.contains(tap)) tap.click();
	}
	function click(event: MouseEvent) {
		if (event.detail !== 0 && performance.now() < suppressClickUntil) {
			event.preventDefault(); event.stopImmediatePropagation(); suppressClickUntil = 0;
		}
	}
	// Window capture runs before ChatInput's document/form expansion handlers.
	window.addEventListener('pointerdown', pointerDown, true);
	window.addEventListener('touchstart', start, { capture: true, passive: true });
	window.addEventListener('touchmove', move, { passive: false });
	window.addEventListener('pointermove', pointerMove, { passive: true });
	window.addEventListener('touchend', end);
	window.addEventListener('touchcancel', cancel);
	window.addEventListener('pointercancel', cancel);
	window.addEventListener('blur', cancel);
	composer.addEventListener('click', click, true);
	return { cancel, destroy() {
		cancel();
		window.removeEventListener('pointerdown', pointerDown, true);
		window.removeEventListener('touchstart', start, true);
		window.removeEventListener('touchmove', move);
		window.removeEventListener('pointermove', pointerMove);
		window.removeEventListener('touchend', end);
		window.removeEventListener('touchcancel', cancel);
		window.removeEventListener('pointercancel', cancel);
		window.removeEventListener('blur', cancel);
		composer.removeEventListener('click', click, true);
	} };
}
