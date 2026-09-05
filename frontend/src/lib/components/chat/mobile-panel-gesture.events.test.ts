import { afterEach, describe, expect, it, vi } from 'vitest';
import { attachPanelDismiss } from './mobile-panel-gesture';

const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); document.body.replaceChildren(); });
function setup() {
	const panel = document.createElement('section');
	const list = document.createElement('div');
	const button = document.createElement('button');
	list.append(button); panel.append(list); document.body.append(panel);
	const onDismiss = vi.fn(), onDrag = vi.fn(), onClick = vi.fn();
	button.addEventListener('click', onClick);
	const action = attachPanelDismiss(panel, { enabled: () => true, height: () => 240, onDismiss, onDrag });
	cleanups.push(action.destroy);
	return { panel, list, button, onDismiss, onDrag, onClick, action };
}
function touch(target: Element, type: string, y: number, options: { x?: number; cancelable?: boolean; count?: number } = {}) {
	const item = { identifier: 1, clientX: options.x ?? 100, clientY: y };
	const event = new Event(type, { bubbles: true, cancelable: options.cancelable ?? true });
	Object.assign(event, { touches: type === 'touchend' ? [] : Array.from({ length: options.count ?? 1 }, (_, index) => ({ ...item, identifier: index + 1 })), changedTouches: [item] });
	target.dispatchEvent(event);
	return event;
}

describe('panel native event arbitration', () => {
	it.each(['list', 'panel'] as const)('never takes over a gesture that started with %s scrolled', owner => {
		const h = setup(); h[owner].scrollTop = 100;
		touch(h.button, 'touchstart', 100);
		expect(touch(h.button, 'touchmove', 130).defaultPrevented).toBe(false);
		h[owner].scrollTop = 0;
		expect(touch(h.button, 'touchmove', 240).defaultPrevented).toBe(false);
		touch(h.button, 'touchend', 240);
		expect(h.onDismiss).not.toHaveBeenCalled();
		// Only a new gesture, now starting at top, can dismiss.
		touch(h.button, 'touchstart', 100);
		expect(touch(h.button, 'touchmove', 200).defaultPrevented).toBe(true);
		touch(h.button, 'touchend', 200);
		expect(h.onDismiss).toHaveBeenCalledOnce();
	});

	it('claims downward touch before native pan but does not move the tray for jitter', () => {
		const h = setup(); touch(h.button, 'touchstart', 100);
		expect(touch(h.button, 'touchmove', 102).defaultPrevented).toBe(true);
		expect(h.onDrag).not.toHaveBeenCalled();
		touch(h.button, 'touchend', 102);
		expect(h.onDismiss).not.toHaveBeenCalled();
		expect(h.onClick).toHaveBeenCalledOnce();
		// A browser that still emits a consequential mouse click must not toggle twice.
		h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
		expect(h.onClick).toHaveBeenCalledOnce();
	});

	it.each(['cancel', 'multitouch', 'drag-return'] as const)('does not restore an early-consumed tap after %s', reason => {
		const h = setup(); touch(h.button, 'touchstart', 100); touch(h.button, 'touchmove', 102);
		if (reason === 'cancel') touch(h.button, 'touchcancel', 102);
		else if (reason === 'multitouch') touch(h.button, 'touchmove', 102, { count: 2 });
		else touch(h.button, 'touchmove', 120);
		touch(h.button, 'touchend', 102);
		expect(h.onClick).not.toHaveBeenCalled();
		expect(h.onDismiss).not.toHaveBeenCalled();
	});

	it.each(['uncancelable', 'multitouch', 'cancel'] as const)('surrenders permanently on %s and ignores the late release', reason => {
		const h = setup(); touch(h.button, 'touchstart', 100);
		if (reason === 'cancel') touch(h.button, 'touchcancel', 120);
		else touch(h.button, 'touchmove', 120, reason === 'uncancelable' ? { cancelable: false } : { count: 2 });
		touch(h.button, 'touchmove', 240);
		touch(h.button, 'touchend', 240);
		expect(h.onDismiss).not.toHaveBeenCalled();
	});

	it('keeps horizontal-first touch native even if it bends downward', () => {
		const h = setup(); touch(h.button, 'touchstart', 100);
		expect(touch(h.button, 'touchmove', 101, { x: 103 }).defaultPrevented).toBe(false);
		expect(touch(h.button, 'touchmove', 240).defaultPrevented).toBe(false);
		touch(h.button, 'touchend', 240); expect(h.onDismiss).not.toHaveBeenCalled();
	});

	it('preserves ordinary taps and keyboard clicks, suppressing only the click after a drag', () => {
		const h = setup(); touch(h.button, 'touchstart', 100); touch(h.button, 'touchend', 100);
		h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
		expect(h.onClick).toHaveBeenCalledOnce();
		touch(h.button, 'touchstart', 100); touch(h.button, 'touchmove', 200); touch(h.button, 'touchend', 200);
		h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
		expect(h.onClick).toHaveBeenCalledTimes(2);
		h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
		expect(h.onClick).toHaveBeenCalledTimes(2);
	});

	it('does not intercept editing and removes handlers on destroy', () => {
		const h = setup(), input = document.createElement('input'); h.list.append(input);
		touch(input, 'touchstart', 100);
		expect(touch(input, 'touchmove', 230).defaultPrevented).toBe(false);
		touch(input, 'touchend', 230); expect(h.onDismiss).not.toHaveBeenCalled();
		h.action.destroy();
		touch(h.button, 'touchstart', 100);
		expect(touch(h.button, 'touchmove', 230).defaultPrevented).toBe(false);
		touch(h.button, 'touchend', 230); expect(h.onDismiss).not.toHaveBeenCalled();
	});
});
