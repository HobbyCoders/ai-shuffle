import { describe, expect, it } from 'vitest';
import { canStartDrawerSwipe, completes, dragOffset, lockAxis } from './drawer-gesture';

describe('drawer gesture helpers', () => {
	it('waits out the slop, then takes only horizontal moves in the swipe direction', () => {
		expect(lockAxis(5, 3, 'open')).toBeNull();
		expect(lockAxis(20, 4, 'open')).toBe(true);
		expect(lockAxis(-20, 4, 'open')).toBe(false);
		expect(lockAxis(-20, 4, 'close')).toBe(true);
		expect(lockAxis(12, 30, 'open')).toBe(false);
	});

	it('clamps the drag to the screen and to the swipe direction', () => {
		expect(dragOffset(-30, 400, 'open')).toBe(0);
		expect(dragOffset(900, 400, 'open')).toBe(400);
		expect(dragOffset(30, 400, 'close')).toBe(0);
		expect(dragOffset(-900, 400, 'close')).toBe(-400);
	});

	it('completes past 30% of the width or on a flick', () => {
		expect(completes(121, 0, 400, 'open')).toBe(true);
		expect(completes(60, 0.1, 400, 'open')).toBe(false);
		expect(completes(40, 0.8, 400, 'open')).toBe(true);
		expect(completes(-121, 0, 400, 'close')).toBe(true);
		expect(completes(-40, 0.8, 400, 'close')).toBe(false);
		expect(completes(-40, -0.8, 400, 'close')).toBe(true);
	});

	it('leaves fields, dialogs and session rows their own gestures', () => {
		const host = document.createElement('div');
		host.innerHTML = `
			<input id="f" />
			<div role="dialog"><span id="d">x</span></div>
			<div data-session-card><span id="r">row</span></div>
			<p id="p">text</p>`;
		document.body.append(host);
		const el = (id: string) => host.querySelector(`#${id}`)!;
		expect(canStartDrawerSwipe(el('f'), 'open')).toBe(false);
		expect(canStartDrawerSwipe(el('d'), 'open')).toBe(false);
		expect(canStartDrawerSwipe(el('r'), 'close')).toBe(false);
		expect(canStartDrawerSwipe(el('r'), 'open')).toBe(true);
		expect(canStartDrawerSwipe(el('p'), 'open')).toBe(true);
		expect(canStartDrawerSwipe(null, 'open')).toBe(false);
		host.remove();
	});
});
