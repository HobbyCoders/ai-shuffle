export interface GesturePoint { x: number; y: number; time: number; }
export interface PanelGesture {
	start: GesturePoint;
	mode: 'pending' | 'scroll' | 'dismiss';
	offset: number;
	ended: boolean;
}

/** Snapshot the entire touched scroll chain, including the panel itself. */
export function canStartPanelDismiss(target: EventTarget | null, panel: HTMLElement): boolean {
	const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
	if (!element || !panel.contains(element)) return false;
	if (element.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="slider"]')) return false;
	for (let current: Element | null = element; current; current = current.parentElement) {
		// Positive fractional offsets count as scrolled. Safari's negative
		// overscroll is already at the top and must not look like a scrolled list.
		if (current.scrollTop > 0) return false;
		if (current === panel) return true;
	}
	return false;
}

export function createPanelGesture(point: GesturePoint & { eligible: boolean }): PanelGesture {
	return { start: { x: point.x, y: point.y, time: point.time }, mode: point.eligible ? 'pending' : 'scroll', offset: 0, ended: false };
}

export function updatePanelGesture(state: PanelGesture, point: GesturePoint) {
	if (state.ended || state.mode === 'scroll') return { mode: state.mode, offset: state.offset };
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
	updatePanelGesture(state, point);
	state.ended = true;
	const velocity = state.offset / Math.max(1, point.time - state.start.time);
	return wasDismiss && (state.offset >= Math.max(24, Math.min(72, panelHeight * 0.3)) || (state.offset > 16 && velocity > 0.6));
}

export function cancelPanelGesture(state: PanelGesture) {
	state.ended = true;
	state.mode = 'scroll';
	state.offset = 0;
}

interface DismissOptions {
	enabled: () => boolean;
	height: () => number;
	onDrag: (offset: number, dragging: boolean) => void;
	onDismiss: () => void;
}

/** Native scrolling owns every gesture unless an initially eligible downward
 * gesture is claimed before the browser starts scrolling. Touch Events are
 * needed here: pan-y cancels Pointer Events when native scrolling takes over. */
export function attachPanelDismiss(panel: HTMLElement, options: DismissOptions) {
	let gesture: PanelGesture | null = null;
	let source: 'touch' | 'pointer' | null = null;
	let identifier = -1;
	let origin: EventTarget | null = null;
	let suppressClickUntil = 0;
	let dragged = false;
	let touchConsumed = false;
	let exceededTapSlop = false;
	const point = (x: number, y: number) => ({ x, y, time: performance.now() });

	function clear() {
		const captured = source === 'pointer' && panel.hasPointerCapture?.(identifier);
		const previousId = identifier;
		gesture = null;
		source = null;
		identifier = -1;
		origin = null;
		dragged = false;
		touchConsumed = false;
		exceededTapSlop = false;
		options.onDrag(0, false);
		if (captured) panel.releasePointerCapture(previousId);
	}
	function cancel() {
		if (dragged) suppressClickUntil = performance.now() + 500;
		if (gesture) cancelPanelGesture(gesture);
		clear();
	}
	function begin(kind: 'touch' | 'pointer', id: number, target: EventTarget | null, x: number, y: number) {
		if (!options.enabled()) return;
		suppressClickUntil = 0;
		source = kind;
		identifier = id;
		origin = target;
		gesture = createPanelGesture({ ...point(x, y), eligible: canStartPanelDismiss(target, panel) });
	}
	function move(event: Event, x: number, y: number) {
		if (!gesture || gesture.mode === 'scroll') return;
		if (!options.enabled() || !event.cancelable || !canStartPanelDismiss(origin, panel)) {
			// May only surrender ownership; never acquire it after scrolling.
			cancel();
			return;
		}
		const next = updatePanelGesture(gesture, point(x, y));
		if (next.mode !== 'dismiss') return;
		event.preventDefault();
		if (source === 'touch') touchConsumed = true;
		if (next.offset <= 8 && !dragged) return;
		dragged = true;
		if (source === 'pointer' && !panel.hasPointerCapture?.(identifier)) panel.setPointerCapture?.(identifier);
		options.onDrag(Math.min(options.height(), next.offset), true);
	}
	function finish(x: number, y: number) {
		if (!gesture) return;
		const claimed = dragged;
		const end = point(x, y);
		const withinSlop = Math.max(Math.abs(x - gesture.start.x), Math.abs(y - gesture.start.y)) <= 8;
		const element = origin instanceof Element ? origin : origin instanceof Node ? origin.parentElement : null;
		// Canceling the first tiny downward move also cancels the browser's
		// eventual click. Restore an ordinary short tap exactly once; never
		// synthesize activation after scrolling, cancellation or a real drag.
		const tapTarget = source === 'touch' && touchConsumed && !claimed && !exceededTapSlop && withinSlop
			&& end.time - gesture.start.time < 500 && options.enabled() && canStartPanelDismiss(origin, panel)
			? element?.closest<HTMLElement>('button, a[href], [role="button"], [role="checkbox"]') : null;
		const dismiss = options.enabled() && finishPanelGesture(gesture, point(x, y), options.height());
		if (claimed || touchConsumed) suppressClickUntil = performance.now() + 500;
		clear();
		if (dismiss) options.onDismiss();
		else if (tapTarget && panel.contains(tapTarget)) tapTarget.click();
	}
	function touchStart(event: TouchEvent) {
		if (event.touches.length !== 1) { cancel(); return; }
		if (gesture) return;
		const touch = event.touches[0];
		begin('touch', touch.identifier, event.target, touch.clientX, touch.clientY);
	}
	function touchMove(event: TouchEvent) {
		if (source !== 'touch') return;
		if (event.touches.length !== 1) { cancel(); return; }
		const touch = Array.from(event.touches).find(touch => touch.identifier === identifier);
		if (!touch) return;
		if (gesture && Math.max(Math.abs(touch.clientX - gesture.start.x), Math.abs(touch.clientY - gesture.start.y)) > 8) exceededTapSlop = true;
		if (gesture?.mode === 'pending') {
			// Touch ownership must be decided before the first native pan. Keep
			// the visual 8px slop, but don't wait for it to cancel a downward pan.
			const dx = touch.clientX - gesture.start.x;
			const dy = touch.clientY - gesture.start.y;
			if (dx || dy) gesture.mode = dy > 0 && dy > Math.abs(dx) ? 'dismiss' : 'scroll';
		}
		move(event, touch.clientX, touch.clientY);
	}
	function touchEnd(event: TouchEvent) {
		if (source !== 'touch') return;
		if (event.touches.length) { cancel(); return; }
		const touch = Array.from(event.changedTouches).find(touch => touch.identifier === identifier);
		if (touch) finish(touch.clientX, touch.clientY);
	}
	function touchCancel() { if (source === 'touch') cancel(); }
	function pointerStart(event: PointerEvent) {
		if (event.pointerType === 'touch') return;
		if (gesture) { cancel(); return; }
		if (event.button !== 0 || event.isPrimary === false) return;
		begin('pointer', event.pointerId, event.target, event.clientX, event.clientY);
	}
	function pointerMove(event: PointerEvent) {
		if (source === 'pointer' && event.pointerId === identifier) move(event, event.clientX, event.clientY);
	}
	function pointerEnd(event: PointerEvent) {
		if (source === 'pointer' && event.pointerId === identifier) finish(event.clientX, event.clientY);
	}
	function pointerCancel(event: PointerEvent) {
		if (source === 'pointer' && event.pointerId === identifier) cancel();
	}
	function click(event: MouseEvent) {
		if (event.detail !== 0 && performance.now() < suppressClickUntil) {
			event.preventDefault();
			event.stopImmediatePropagation();
			suppressClickUntil = 0;
		}
	}
	panel.addEventListener('touchstart', touchStart, { passive: true });
	panel.addEventListener('touchmove', touchMove, { passive: false });
	panel.addEventListener('touchend', touchEnd, { passive: true });
	panel.addEventListener('touchcancel', touchCancel, { passive: true });
	panel.addEventListener('pointerdown', pointerStart);
	window.addEventListener('pointermove', pointerMove);
	window.addEventListener('pointerup', pointerEnd);
	window.addEventListener('pointercancel', pointerCancel);
	panel.addEventListener('lostpointercapture', pointerCancel);
	panel.addEventListener('click', click, true);
	window.addEventListener('blur', cancel);
	return { cancel, destroy() {
		cancel();
		panel.removeEventListener('touchstart', touchStart);
		panel.removeEventListener('touchmove', touchMove);
		panel.removeEventListener('touchend', touchEnd);
		panel.removeEventListener('touchcancel', touchCancel);
		panel.removeEventListener('pointerdown', pointerStart);
		window.removeEventListener('pointermove', pointerMove);
		window.removeEventListener('pointerup', pointerEnd);
		window.removeEventListener('pointercancel', pointerCancel);
		panel.removeEventListener('lostpointercapture', pointerCancel);
		panel.removeEventListener('click', click, true);
		window.removeEventListener('blur', cancel);
	} };
}

interface ComposerOpenOptions {
	enabled: () => boolean;
	allowEditor: () => boolean;
	open: () => boolean;
	openDirection: () => 'up' | 'down';
	onOpenChange: (open: boolean) => void;
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
export function attachComposerOpen(composer: HTMLElement, options: ComposerOpenOptions) {
	let snapshot: ComposerGestureIntent | null = null;
	let intent: ComposerGestureIntent | null = null;
	let gesture: PanelGesture | null = null;
	let identifier = -1;
	let origin: EventTarget | null = null;
	let consumed = false;
	let dragged = false;
	let exceededTapSlop = false;
	let suppressClickUntil = 0;
	const point = (x: number, y: number) => ({ x, y: y * intent!.direction, time: performance.now() });
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
		gesture = createPanelGesture({ ...point(touch.clientX, touch.clientY), eligible: true });
	}
	function move(event: TouchEvent) {
		if (!gesture) return;
		if (event.touches.length !== 1 || !options.enabled() || !event.cancelable || !canStartComposerOpen(origin, composer, true)) { cancel(); return; }
		const touch = Array.from(event.touches).find(t => t.identifier === identifier);
		if (!touch) { cancel(); return; }
		const next = point(touch.clientX, touch.clientY);
		const dx = next.x - gesture.start.x, dy = next.y - gesture.start.y;
		if (Math.max(Math.abs(dx), Math.abs(dy)) > 8) exceededTapSlop = true;
		// Match dismissal's first-move arbitration, preserving its visual slop.
		if (gesture.mode === 'pending' && (dx || dy)) gesture.mode = dy > 0 && dy > Math.abs(dx) ? 'dismiss' : 'scroll';
		updatePanelGesture(gesture, next);
		if (gesture.mode !== 'dismiss') return;
		event.preventDefault(); consumed = true;
		if (gesture.offset > 8) dragged = true;
	}
	function end(event: TouchEvent) {
		if (!gesture) { snapshot = null; return; }
		if (event.touches.length) { cancel(); return; }
		const touch = Array.from(event.changedTouches).find(t => t.identifier === identifier);
		if (!touch) { cancel(); return; }
		const next = point(touch.clientX, touch.clientY);
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
		window.removeEventListener('touchend', end);
		window.removeEventListener('touchcancel', cancel);
		window.removeEventListener('pointercancel', cancel);
		window.removeEventListener('blur', cancel);
		composer.removeEventListener('click', click, true);
	} };
}
